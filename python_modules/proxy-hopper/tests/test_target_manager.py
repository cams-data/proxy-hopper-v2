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
        # Pre-claim init so the queue's start() skips seeding — queue has no identities.
        await pool_store.claim_init(cfg.name)

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
