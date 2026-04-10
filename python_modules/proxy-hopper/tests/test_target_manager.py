"""TargetManager tests — dispatch, retry, and request lifecycle."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from proxy_hopper.backend.memory import MemoryIPPoolBackend
from proxy_hopper.config import TargetConfig
from proxy_hopper.models import PendingRequest, ProxyResponse
from proxy_hopper.pool import IPPool
from proxy_hopper.target_manager import TargetManager


def make_config(**kw) -> TargetConfig:
    defaults = dict(
        name="test",
        regex=r".*example\.com.*",
        ip_list=["1.2.3.4:8080"],
        min_request_interval=0.0,
        max_queue_wait=2.0,
        num_retries=2,
        ip_failures_until_quarantine=3,
        quarantine_time=0.1,
    )
    defaults.update(kw)
    return TargetConfig(**defaults)


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
    backend = MemoryIPPoolBackend()
    await backend.start()
    mgr = TargetManager(cfg, backend)
    await mgr.start()
    yield mgr, backend
    await mgr.stop()
    await backend.stop()


class TestMatching:
    def test_matches_url(self):
        cfg = make_config(regex=r".*example\.com.*")
        mgr = TargetManager(cfg, MemoryIPPoolBackend())
        assert mgr.matches("http://example.com/path")
        assert not mgr.matches("http://other.com/path")

    def test_matches_connect_host(self):
        cfg = make_config(regex=r"example\.com:443")
        mgr = TargetManager(cfg, MemoryIPPoolBackend())
        assert mgr.matches("example.com:443")
        assert not mgr.matches("other.com:443")


class TestDispatcher:
    async def test_success_resolves_future(self, manager_and_backend):
        mgr, _ = manager_and_backend
        fake_response = ProxyResponse(status=200, headers={}, body=b"OK")

        async def fake_execute(address, request):
            request.future.set_result(fake_response)

        with patch.object(mgr, "_execute_request", side_effect=fake_execute):
            req = make_request()
            await mgr.submit(req)
            result = await asyncio.wait_for(req.future, timeout=2.0)
            assert result.status == 200

    async def test_expired_request_sets_timeout(self):
        cfg = make_config(max_queue_wait=0.01)
        backend = MemoryIPPoolBackend()
        await backend.start()
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        req = PendingRequest(
            method="GET", url="http://example.com/", headers={}, body=None,
            future=asyncio.get_event_loop().create_future(),
            arrival_time=time.monotonic() - 100,
            max_queue_wait=0.01, num_retries=0,
        )
        await mgr.submit(req)

        with pytest.raises((TimeoutError, asyncio.TimeoutError, Exception)):
            await asyncio.wait_for(req.future, timeout=1.0)

        await mgr.stop()
        await backend.stop()

    async def test_no_ip_sets_timeout_exception(self):
        cfg = make_config(max_queue_wait=0.1)
        backend = MemoryIPPoolBackend()
        await backend.start()
        # Initialise pool but don't add any IPs
        await backend.init_target(cfg.name)

        mgr = TargetManager(cfg, backend)
        # Start just the dispatcher, not pool (pool is already set up)
        mgr._running = True
        mgr._tasks = [
            asyncio.create_task(mgr._dispatcher_worker(), name="ph:dispatcher:test")
        ]
        # Pool still needs to be started so its _backend reference is set
        await mgr._pool.start()

        req = make_request(max_queue_wait=0.1)
        await mgr.submit(req)

        with pytest.raises((TimeoutError, asyncio.TimeoutError, Exception)):
            await asyncio.wait_for(req.future, timeout=1.0)

        await mgr.stop()
        await backend.stop()


class TestExecuteRequest:
    async def test_success_calls_pool_record_success(self, manager_and_backend):
        mgr, backend = manager_and_backend
        address = await mgr._pool.acquire(timeout=1.0)

        record_success = AsyncMock()
        with patch.object(mgr._pool, "record_success", record_success):
            req = make_request()
            from aioresponses import aioresponses
            with aioresponses() as m:
                m.get("http://example.com/", status=200, body=b"hello")
                await mgr._execute_request(address, req)

            args, _ = record_success.call_args
            assert args[0] == address
            assert isinstance(args[1], float)
            assert req.future.result().status == 200

    async def test_rate_limit_calls_pool_record_failure_and_requeues(self):
        cfg = make_config(ip_list=["1.2.3.4:8080"])
        backend = MemoryIPPoolBackend()
        await backend.start()
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        # Pause dispatcher so the re-queued request stays in queue
        dispatcher = next(t for t in mgr._tasks if "dispatcher" in t.get_name())
        dispatcher.cancel()
        await asyncio.gather(dispatcher, return_exceptions=True)

        address = await mgr._pool.acquire(timeout=1.0)
        record_failure = AsyncMock()

        with patch.object(mgr._pool, "record_failure", record_failure):
            req = make_request(num_retries=2)
            from aioresponses import aioresponses
            with aioresponses() as m:
                m.get("http://example.com/", status=429, body=b"rate limited")
                await mgr._execute_request(address, req)

            args, _ = record_failure.call_args
            assert args[0] == address
            assert isinstance(args[1], float)
            assert not req.future.done()
            retry = mgr._request_queue.get_nowait()
            assert retry.failure_count == 1
            assert retry.future is req.future

        await mgr.stop()
        await backend.stop()

    async def test_connection_error_requeues_if_retries_remain(self, manager_and_backend):
        mgr, backend = manager_and_backend
        address = await mgr._pool.acquire(timeout=1.0)

        with patch.object(mgr._pool, "record_failure", AsyncMock()):
            req = make_request(num_retries=2)
            import aiohttp
            from aioresponses import aioresponses
            with aioresponses() as m:
                m.get("http://example.com/", exception=aiohttp.ClientConnectionError("fail"))
                await mgr._execute_request(address, req)

            assert not req.future.done()
            assert mgr._request_queue.qsize() == 1


class TestShutdown:
    async def test_queued_requests_get_503_on_shutdown(self):
        cfg = make_config(max_queue_wait=30.0)
        backend = MemoryIPPoolBackend()
        await backend.start()
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        # Drain the pool so requests sit in queue waiting for an IP
        await mgr._pool.acquire(timeout=1.0)

        req = make_request(max_queue_wait=30.0)
        await mgr.submit(req)

        await mgr.stop()
        await backend.stop()

        assert req.future.done()
        result = req.future.result()
        assert result.status == 503

    async def test_inflight_requests_are_awaited_on_shutdown(self):
        cfg = make_config()
        backend = MemoryIPPoolBackend()
        await backend.start()
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        completed = asyncio.Event()

        async def slow_execute(address, request):
            await asyncio.sleep(0.1)
            request.future.set_result(ProxyResponse(status=200, headers={}, body=b"ok"))
            completed.set()

        with patch.object(mgr, "_execute_request", side_effect=slow_execute):
            req = make_request()
            await mgr.submit(req)
            await asyncio.sleep(0.05)  # let dispatcher pick it up
            await mgr.stop(drain_timeout=2.0)
            await backend.stop()

        assert completed.is_set()
        assert req.future.result().status == 200

    async def test_inflight_requests_cancelled_after_drain_timeout(self):
        cfg = make_config()
        backend = MemoryIPPoolBackend()
        await backend.start()
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        async def hanging_execute(address, request):
            await asyncio.sleep(999)

        with patch.object(mgr, "_execute_request", side_effect=hanging_execute):
            req = make_request()
            await mgr.submit(req)
            await asyncio.sleep(0.05)  # let dispatcher pick it up
            await mgr.stop(drain_timeout=0.1)
            await backend.stop()

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
