"""TargetManager tests — dispatch, retry, and request lifecycle."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from proxy_hopper.backend.memory import MemoryBackend
from proxy_hopper.config import TargetConfig
from proxy_hopper.models import PendingRequest, ProxyResponse
from proxy_hopper.pool import IdentityQueue
from proxy_hopper.pool_store import IPPoolStore
from proxy_hopper.target_manager import TargetManager

from test_helpers import make_target_config


def make_config(**kw) -> TargetConfig:
    ip_list = kw.pop("ip_list", ["1.2.3.4:8080"])
    defaults = dict(
        name="test",
        regex=r".*example\.com.*",
        min_request_interval=0.0,
        max_queue_wait=2.0,
        num_retries=2,
        ip_failures_until_quarantine=3,
        quarantine_time=0.1,
    )
    defaults.update(kw)
    return make_target_config(ip_list, **defaults)


async def make_pool_store() -> tuple[MemoryBackend, IPPoolStore]:
    backend = MemoryBackend()
    await backend.start()
    return backend, IPPoolStore(backend)


def make_request(url="http://example.com/", max_queue_wait=2.0, num_retries=2) -> PendingRequest:
    return PendingRequest(
        method="GET",
        url=url,
        headers={},
        body=None,
        future=asyncio.get_event_loop().create_future(),
        arrival_time=time.monotonic(),
        max_queue_wait=max_queue_wait,
        num_retries=num_retries,
    )


@pytest.fixture
async def manager_and_backend():
    cfg = make_config()
    raw_backend, pool_store = await make_pool_store()
    mgr = TargetManager(cfg, pool_store)
    await mgr.start()
    yield mgr, pool_store
    await mgr.stop()
    await raw_backend.stop()


class TestMatching:
    def test_matches_url(self):
        cfg = make_config(regex=r".*example\.com.*")
        mgr = TargetManager(cfg, IPPoolStore(MemoryBackend()))
        assert mgr.matches("http://example.com/path")
        assert not mgr.matches("http://other.com/path")

    def test_matches_connect_host(self):
        cfg = make_config(regex=r"example\.com:443")
        mgr = TargetManager(cfg, IPPoolStore(MemoryBackend()))
        assert mgr.matches("example.com:443")
        assert not mgr.matches("other.com:443")


class TestDispatcher:
    async def test_success_resolves_future(self, manager_and_backend):
        mgr, _ = manager_and_backend
        fake_response = ProxyResponse(status=200, headers={}, body=b"OK")

        async def fake_execute(uuid, identity, request):
            request.future.set_result(fake_response)

        with patch.object(mgr, "_execute_request", side_effect=fake_execute):
            req = make_request()
            await mgr.submit(req)
            result = await asyncio.wait_for(req.future, timeout=2.0)
            assert result.status == 200

    async def test_expired_request_returns_503(self):
        cfg = make_config(max_queue_wait=0.01)
        raw_backend, pool_store = await make_pool_store()
        mgr = TargetManager(cfg, pool_store)
        await mgr.start()

        req = PendingRequest(
            method="GET", url="http://example.com/", headers={}, body=None,
            future=asyncio.get_event_loop().create_future(),
            arrival_time=time.monotonic() - 100,
            max_queue_wait=0.01, num_retries=0,
        )
        await mgr.submit(req)

        result = await asyncio.wait_for(req.future, timeout=1.0)
        assert result.status == 503

        await mgr.stop()
        await raw_backend.stop()

    async def test_no_ip_returns_503(self):
        cfg = make_config(max_queue_wait=0.1)
        raw_backend, pool_store = await make_pool_store()
        # Pre-hold the reconcile lock so the queue's start() skips its own
        # reconcile (loses the race, trusts an already-in-progress winner) —
        # queue has no identities.
        await pool_store.reconcile_lock_acquire(cfg.name, "test-holds-lock", ttl_seconds=60)

        mgr = TargetManager(cfg, pool_store)
        # Start just the dispatcher, not the full queue (queue is already set up)
        mgr._running = True
        mgr._tasks = [
            asyncio.create_task(mgr._dispatcher_worker(), name="ph:dispatcher:test")
        ]
        # Queue still needs to be started so its sweep task is running
        await mgr._queue.start()

        req = make_request(max_queue_wait=0.1)
        await mgr.submit(req)

        result = await asyncio.wait_for(req.future, timeout=1.0)
        assert result.status == 503

        await mgr.stop()
        await raw_backend.stop()


class TestExecuteRequest:
    async def test_success_calls_queue_record_success(self, manager_and_backend):
        mgr, backend = manager_and_backend
        result = await mgr._queue.acquire(timeout=1.0)
        assert result is not None
        uuid, identity = result

        record_success = AsyncMock()
        with patch.object(mgr._queue, "record_success", record_success):
            req = make_request()
            from aioresponses import aioresponses
            with aioresponses() as m:
                m.get("http://example.com/", status=200, body=b"hello")
                await mgr._execute_request(uuid, identity, req)

            args, _ = record_success.call_args
            assert args[0] == uuid
            assert args[1] is identity
            assert isinstance(args[2], float)
            assert req.future.result().status == 200

    async def test_rate_limit_calls_queue_record_failure_and_requeues(self):
        cfg = make_config(ip_list=["1.2.3.4:8080"])
        raw_backend, pool_store = await make_pool_store()
        mgr = TargetManager(cfg, pool_store)
        await mgr.start()

        # Pause dispatcher so the re-queued request stays in queue
        dispatcher = next(t for t in mgr._tasks if "dispatcher" in t.get_name())
        dispatcher.cancel()
        await asyncio.gather(dispatcher, return_exceptions=True)

        result = await mgr._queue.acquire(timeout=1.0)
        assert result is not None
        uuid, identity = result
        record_failure = AsyncMock(return_value=False)

        with patch.object(mgr._queue, "record_failure", record_failure):
            req = make_request(num_retries=2)
            from aioresponses import aioresponses
            with aioresponses() as m:
                m.get("http://example.com/", status=429, body=b"rate limited")
                await mgr._execute_request(uuid, identity, req)

            args, _ = record_failure.call_args
            assert args[0] == uuid
            assert args[1] is identity
            assert isinstance(args[2], float)
            assert not req.future.done()
            retry = mgr._request_queue.get_nowait()
            assert retry.failure_count == 1
            assert retry.future is req.future

        await mgr.stop()
        await raw_backend.stop()

    async def test_connection_error_requeues_if_retries_remain(self, manager_and_backend):
        mgr, backend = manager_and_backend
        result = await mgr._queue.acquire(timeout=1.0)
        assert result is not None
        uuid, identity = result

        with patch.object(mgr._queue, "record_failure", AsyncMock(return_value=False)):
            req = make_request(num_retries=2)
            import aiohttp
            from aioresponses import aioresponses
            with aioresponses() as m:
                m.get("http://example.com/", exception=aiohttp.ClientConnectionError("fail"))
                await mgr._execute_request(uuid, identity, req)

            assert not req.future.done()
            assert mgr._request_queue.qsize() == 1


class TestAppMetricsRecording:
    """_execute_request/_execute_pinned_request must record into
    AppMetricsStore, when one is configured, at the same finally-block call
    site as the existing Prometheus instrumentation — same outcome, same
    elapsed time, one record per upstream attempt."""

    @pytest.fixture
    async def manager_with_app_metrics(self):
        from proxy_hopper.app_metrics import AppMetricsStore

        cfg = make_config()
        raw_backend, pool_store = await make_pool_store()
        app_metrics = AppMetricsStore(raw_backend)
        mgr = TargetManager(cfg, pool_store, app_metrics=app_metrics)
        await mgr.start()
        yield mgr, app_metrics
        await mgr.stop()
        await raw_backend.stop()

    async def test_success_is_recorded(self, manager_with_app_metrics):
        mgr, app_metrics = manager_with_app_metrics
        uuid, identity = await mgr._queue.acquire(timeout=1.0)

        req = make_request()
        from aioresponses import aioresponses
        with aioresponses() as m:
            m.get("http://example.com/", status=200, body=b"hello")
            await mgr._execute_request(uuid, identity, req)

        # The record() call is fired via asyncio.create_task (fire-and-forget,
        # matching the event_bus.publish pattern) — give it one loop turn.
        await asyncio.sleep(0)
        snap = await app_metrics.get("test")
        assert snap.total_requests == 1
        assert snap.success_requests == 1
        assert snap.failed_requests == 0

    async def test_failure_is_recorded(self, manager_with_app_metrics):
        mgr, app_metrics = manager_with_app_metrics
        uuid, identity = await mgr._queue.acquire(timeout=1.0)

        with patch.object(mgr._queue, "record_failure", AsyncMock(return_value=False)):
            req = make_request(num_retries=0)
            from aioresponses import aioresponses
            with aioresponses() as m:
                m.get("http://example.com/", status=503, body=b"unavailable")
                await mgr._execute_request(uuid, identity, req)

        await asyncio.sleep(0)
        snap = await app_metrics.get("test")
        assert snap.total_requests == 1
        assert snap.success_requests == 0
        assert snap.failed_requests == 1

    async def test_no_app_metrics_configured_does_not_error(self, manager_and_backend):
        """Default (app_metrics=None) TargetManager must behave exactly as
        before — this is the regression guard for every other test in this
        file that constructs a manager without app_metrics."""
        mgr, _ = manager_and_backend
        assert mgr._app_metrics is None
        uuid, identity = await mgr._queue.acquire(timeout=1.0)

        req = make_request()
        from aioresponses import aioresponses
        with aioresponses() as m:
            m.get("http://example.com/", status=200, body=b"hello")
            await mgr._execute_request(uuid, identity, req)  # must not raise

        assert req.future.result().status == 200


class TestShutdown:
    async def test_queued_requests_get_503_on_shutdown(self):
        cfg = make_config(max_queue_wait=30.0)
        raw_backend, pool_store = await make_pool_store()
        mgr = TargetManager(cfg, pool_store)
        await mgr.start()

        # Drain the queue so requests wait for an identity
        await mgr._queue.acquire(timeout=1.0)

        req = make_request(max_queue_wait=30.0)
        await mgr.submit(req)

        await mgr.stop()
        await raw_backend.stop()

        assert req.future.done()
        result = req.future.result()
        assert result.status == 503

    async def test_inflight_requests_are_awaited_on_shutdown(self):
        cfg = make_config()
        raw_backend, pool_store = await make_pool_store()
        mgr = TargetManager(cfg, pool_store)
        await mgr.start()

        completed = asyncio.Event()

        async def slow_execute(uuid, identity, request):
            await asyncio.sleep(0.1)
            request.future.set_result(ProxyResponse(status=200, headers={}, body=b"ok"))
            completed.set()

        with patch.object(mgr, "_execute_request", side_effect=slow_execute):
            req = make_request()
            await mgr.submit(req)
            await asyncio.sleep(0.05)  # let dispatcher pick it up
            await mgr.stop(drain_timeout=2.0)
            await raw_backend.stop()

        assert completed.is_set()
        assert req.future.result().status == 200

    async def test_inflight_requests_cancelled_after_drain_timeout(self):
        cfg = make_config()
        raw_backend, pool_store = await make_pool_store()
        mgr = TargetManager(cfg, pool_store)
        await mgr.start()

        async def hanging_execute(uuid, identity, request):
            await asyncio.sleep(999)

        with patch.object(mgr, "_execute_request", side_effect=hanging_execute):
            req = make_request()
            await mgr.submit(req)
            await asyncio.sleep(0.05)  # let dispatcher pick it up
            await mgr.stop(drain_timeout=0.1)
            await raw_backend.stop()

        # Shutdown should complete without hanging
        assert len(mgr._inflight) == 0


class TestPendingRequest:
    def test_is_expired(self):
        req = PendingRequest(
            method="GET", url="http://x.com", headers={}, body=None,
            future=asyncio.get_event_loop().create_future(),
            arrival_time=time.monotonic() - 100, max_queue_wait=1.0, num_retries=3,
        )
        assert req.is_expired()

    def test_not_expired(self):
        assert not make_request(max_queue_wait=60.0).is_expired()

    def test_clone_for_retry(self):
        req = make_request(num_retries=3)
        retry = req.clone_for_retry()
        assert retry.failure_count == 1
        assert retry.future is req.future

    def test_can_retry(self):
        req = make_request(num_retries=2)
        assert req.can_retry()
        assert req.clone_for_retry().can_retry()
        assert not req.clone_for_retry().clone_for_retry().can_retry()


# ---------------------------------------------------------------------------
# Phase 2 — X-Proxy-Hopper-Force-IP (pinned IP execution)
# ---------------------------------------------------------------------------

class TestAcquirePinned:
    async def test_returns_identity_for_valid_address(self, manager_and_backend):
        mgr, _ = manager_and_backend
        from proxy_hopper.pool import PinnedAcquireError
        uuid, identity = await mgr._queue.acquire_pinned("1.2.3.4:8080")
        assert identity.address == "1.2.3.4:8080"

    async def test_raises_for_unknown_address(self, manager_and_backend):
        mgr, _ = manager_and_backend
        from proxy_hopper.pool import PinnedAcquireError
        with pytest.raises(PinnedAcquireError, match="not registered"):
            await mgr._queue.acquire_pinned("9.9.9.9:9999")

    async def test_raises_for_retired_address(self, manager_and_backend):
        mgr, pool_store = manager_and_backend
        from proxy_hopper.pool import PinnedAcquireError
        await pool_store.retire_add("test", "1.2.3.4:8080")
        with pytest.raises(PinnedAcquireError, match="retired"):
            await mgr._queue.acquire_pinned("1.2.3.4:8080")

    async def test_raises_for_quarantined_address(self, manager_and_backend):
        mgr, pool_store = manager_and_backend
        from proxy_hopper.pool import PinnedAcquireError
        import time as _time
        await pool_store.quarantine_add("test", "1.2.3.4:8080", _time.time() + 60)
        with pytest.raises(PinnedAcquireError, match="quarantined"):
            await mgr._queue.acquire_pinned("1.2.3.4:8080")

    async def test_does_not_remove_ip_from_pool(self, manager_and_backend):
        mgr, pool_store = manager_and_backend
        before = await pool_store.pool_size("test")
        await mgr._queue.acquire_pinned("1.2.3.4:8080")
        after = await pool_store.pool_size("test")
        assert after == before  # UUID stays in queue


class TestExecutePinnedRequest:
    async def test_success_resolves_future_and_updates_identity(self, manager_and_backend):
        mgr, _ = manager_and_backend
        uuid, identity = await mgr._queue.acquire_pinned("1.2.3.4:8080")
        record_success = AsyncMock()
        with patch.object(mgr._queue, "record_pinned_success", record_success):
            req = make_request()
            from aioresponses import aioresponses
            with aioresponses() as m:
                m.get("http://example.com/", status=200, body=b"ok")
                await mgr._execute_pinned_request(uuid, identity, req)
        assert req.future.result().status == 200
        record_success.assert_awaited_once()

    async def test_retriable_status_calls_pinned_failure(self, manager_and_backend):
        mgr, _ = manager_and_backend
        uuid, identity = await mgr._queue.acquire_pinned("1.2.3.4:8080")
        record_failure = AsyncMock()
        with patch.object(mgr._queue, "record_pinned_failure", record_failure):
            req = make_request(num_retries=2)
            from aioresponses import aioresponses
            with aioresponses() as m:
                m.get("http://example.com/", status=429)
                await mgr._execute_pinned_request(uuid, identity, req)
        assert req.future.result().status == 429
        record_failure.assert_awaited_once()

    async def test_connection_error_resolves_502(self, manager_and_backend):
        mgr, _ = manager_and_backend
        uuid, identity = await mgr._queue.acquire_pinned("1.2.3.4:8080")
        record_failure = AsyncMock()
        with patch.object(mgr._queue, "record_pinned_failure", record_failure):
            req = make_request()
            import aiohttp as _aiohttp
            from aioresponses import aioresponses
            with aioresponses() as m:
                m.get("http://example.com/", exception=_aiohttp.ClientConnectionError("fail"))
                await mgr._execute_pinned_request(uuid, identity, req)
        assert req.future.result().status == 502
        record_failure.assert_awaited_once()


class TestForcedIPDispatch:
    async def test_unknown_force_ip_returns_502(self):
        cfg = make_config()
        raw_backend, pool_store = await make_pool_store()
        mgr = TargetManager(cfg, pool_store)
        await mgr.start()

        req = PendingRequest(
            method="GET", url="http://example.com/", headers={}, body=None,
            future=asyncio.get_event_loop().create_future(),
            arrival_time=time.monotonic(),
            max_queue_wait=2.0, num_retries=0,
            force_ip="9.9.9.9:9999",
        )
        await mgr.submit(req)
        result = await asyncio.wait_for(req.future, timeout=2.0)
        assert result.status == 502
        assert "not registered" in result.body.decode()

        await mgr.stop()
        await raw_backend.stop()

    async def test_valid_force_ip_calls_execute_pinned(self, manager_and_backend):
        mgr, _ = manager_and_backend
        pinned_results: list = []

        async def fake_pinned(uuid, identity, request):
            pinned_results.append(identity.address)
            request.future.set_result(ProxyResponse(200, {}, b"pinned"))

        with patch.object(mgr, "_execute_pinned_request", side_effect=fake_pinned):
            req = PendingRequest(
                method="GET", url="http://example.com/", headers={}, body=None,
                future=asyncio.get_event_loop().create_future(),
                arrival_time=time.monotonic(),
                max_queue_wait=2.0, num_retries=0,
                force_ip="1.2.3.4:8080",
            )
            await mgr.submit(req)
            result = await asyncio.wait_for(req.future, timeout=2.0)

        assert result.status == 200
        assert pinned_results == ["1.2.3.4:8080"]
