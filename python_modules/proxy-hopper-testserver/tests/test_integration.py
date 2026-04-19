"""End-to-end integration tests for proxy-hopper.

These tests verify observable system state after controlled upstream and
proxy-layer failures — things that unit tests cannot cover because they
require a real request lifecycle through the full stack.

Each test runs twice: once against MemoryBackend and once against
RedisBackend (using fakeredis when REDIS_URL is not set, real Redis
in CI). A failure against one backend but not the other indicates a backend
contract violation.

Test matrix
-----------
Upstream failures (proxy forwards correctly, upstream returns error):
  - 500 responses → IP failure counter increments, quarantine after threshold
  - 429 responses → same quarantine path, treated as rate limit
  - hang (no response) → sock_read timeout fires, treated as connection error
  - slow responses → requests complete, latency doesn't cause false failures

Proxy-layer failures (the proxy IP itself is broken):
  - connection refused → aiohttp ClientError, IP marked as failure
  - connection hang → timeout, IP marked as failure
  - connection close → ServerDisconnectedError, IP marked as failure
  - error_response (502/503 from proxy) → treated as server error, IP marked

Cross-cutting:
  - successful requests reset failure counter
  - retry logic uses a different IP on each attempt
  - quarantine expires and IPs return to the pool
  - graceful shutdown drains in-flight requests
"""

from __future__ import annotations

import asyncio
import time

import pytest

from proxy_hopper.config import TargetConfig
from proxy_hopper.models import PendingRequest
from proxy_hopper.target_manager import TargetManager
from proxy_hopper_testserver import MockProxyPool, UpstreamServer

from conftest import make_target_config


def make_request(url: str, max_queue_wait: float = 5.0, num_retries: int = 0) -> PendingRequest:
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


async def submit_and_wait(
    manager: TargetManager,
    url: str,
    num_retries: int = 0,
    timeout: float = 5.0,
):
    req = make_request(url, num_retries=num_retries)
    await manager.submit(req)
    return await asyncio.wait_for(req.future, timeout=timeout)


# ---------------------------------------------------------------------------
# Upstream HTTP error responses
# ---------------------------------------------------------------------------

class TestUpstreamErrors:
    async def test_503_increments_failure_counter(self, backend, proxies, upstream):
        upstream.set_mode("http_error", status=503)
        cfg = make_target_config(
            ip_list=[proxies[0].address],
            num_retries=0,
            ip_failures_until_quarantine=99,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        await submit_and_wait(mgr, upstream.url + "/test")
        await asyncio.sleep(0.1)

        failures = await backend.get_failures(cfg.name, proxies[0].address)
        assert failures == 1

        await mgr.stop()

    async def test_503s_quarantine_ip_at_threshold(self, backend, proxies, upstream):
        upstream.set_mode("http_error", status=503)
        threshold = 3
        cfg = make_target_config(
            ip_list=[proxies[0].address],
            num_retries=0,
            ip_failures_until_quarantine=threshold,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        for _ in range(threshold):
            await submit_and_wait(mgr, upstream.url + "/test", num_retries=0)
            await asyncio.sleep(0.05)

        await asyncio.sleep(0.1)
        quarantined = await backend.quarantine_list(cfg.name)
        assert proxies[0].address in quarantined

        await mgr.stop()

    async def test_429_quarantines_ip_at_threshold(self, backend, proxies, upstream):
        upstream.set_mode("http_error", status=429)
        threshold = 3
        cfg = make_target_config(
            ip_list=[proxies[0].address],
            num_retries=0,
            ip_failures_until_quarantine=threshold,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        for _ in range(threshold):
            await submit_and_wait(mgr, upstream.url + "/test", num_retries=0)
            await asyncio.sleep(0.05)

        await asyncio.sleep(0.1)
        quarantined = await backend.quarantine_list(cfg.name)
        assert proxies[0].address in quarantined

        await mgr.stop()

    async def test_successful_request_resets_failure_count(self, backend, proxies, upstream):
        cfg = make_target_config(
            ip_list=[proxies[0].address],
            num_retries=0,
            ip_failures_until_quarantine=99,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        # Accumulate a failure using 503 (in _RETRIABLE_STATUSES)
        upstream.set_mode("http_error", status=503)
        await submit_and_wait(mgr, upstream.url + "/test", num_retries=0)
        await asyncio.sleep(0.1)

        assert await backend.get_failures(cfg.name, proxies[0].address) == 1

        # Succeed — failure count must reset to 0
        upstream.set_mode("normal")
        await asyncio.sleep(0.15)  # wait for IP to return to pool after cooldown
        await submit_and_wait(mgr, upstream.url + "/test", num_retries=0)
        await asyncio.sleep(0.05)

        assert await backend.get_failures(cfg.name, proxies[0].address) == 0

        await mgr.stop()

    async def test_500_is_passed_through_not_recorded_as_failure(self, backend, proxies, upstream):
        # 500 is not in _RETRIABLE_STATUSES — proxy-hopper passes it straight
        # through to the client and does NOT count it as a proxy IP failure.
        upstream.set_mode("http_error", status=500)
        cfg = make_target_config(
            ip_list=[proxies[0].address],
            num_retries=0,
            ip_failures_until_quarantine=99,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        result = await submit_and_wait(mgr, upstream.url + "/test")
        await asyncio.sleep(0.1)

        assert result.status == 500
        assert await backend.get_failures(cfg.name, proxies[0].address) == 0

        await mgr.stop()

    async def test_successful_request_returns_200(self, manager, proxies, upstream):
        upstream.set_mode("normal")
        result = await submit_and_wait(manager, upstream.url + "/hello")
        assert result.status == 200

    async def test_error_response_body_is_structured_json_on_exhaustion(
        self, backend, proxies, upstream
    ):
        upstream.set_mode("http_error", status=503)
        cfg = make_target_config(
            ip_list=proxies.ip_list,
            num_retries=0,
            ip_failures_until_quarantine=99,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        result = await submit_and_wait(mgr, upstream.url + "/fail", num_retries=0)
        await mgr.stop()

        import json
        body = json.loads(result.body)
        assert "error" in body
        assert "retries_attempted" in body
        assert "retries_allowed" in body


# ---------------------------------------------------------------------------
# Proxy-layer failures
# ---------------------------------------------------------------------------

class TestProxyLayerFailures:
    async def test_refused_connection_increments_failure(self, backend, proxies, upstream):
        proxies[0].set_mode("refuse")

        cfg = make_target_config(
            ip_list=[proxies[0].address],
            num_retries=0,
            ip_failures_until_quarantine=99,
            max_queue_wait=3.0,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        await submit_and_wait(mgr, upstream.url + "/test", timeout=3.0)
        await asyncio.sleep(0.1)

        failures = await backend.get_failures(cfg.name, proxies[0].address)
        assert failures == 1

        await mgr.stop()

    async def test_hang_proxy_triggers_timeout_and_failure(self, backend, proxies, upstream):
        proxies[0].set_mode("hang")

        cfg = make_target_config(
            ip_list=[proxies[0].address],
            num_retries=0,
            ip_failures_until_quarantine=99,
            max_queue_wait=3.0,
        )
        mgr = TargetManager(cfg, backend, proxy_read_timeout=0.5)
        await mgr.start()

        start = time.monotonic()
        await submit_and_wait(mgr, upstream.url + "/test", timeout=3.0)
        elapsed = time.monotonic() - start

        assert elapsed < 2.5

        await asyncio.sleep(0.1)
        failures = await backend.get_failures(cfg.name, proxies[0].address)
        assert failures >= 1

        await mgr.stop()

    async def test_close_proxy_increments_failure(self, backend, proxies, upstream):
        proxies[0].set_mode("close")

        cfg = make_target_config(
            ip_list=[proxies[0].address],
            num_retries=0,
            ip_failures_until_quarantine=99,
            max_queue_wait=3.0,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        await submit_and_wait(mgr, upstream.url + "/test", timeout=3.0)
        await asyncio.sleep(0.1)

        failures = await backend.get_failures(cfg.name, proxies[0].address)
        assert failures >= 1

        await mgr.stop()

    async def test_proxy_error_response_increments_failure(self, backend, proxies, upstream):
        proxies[0].set_mode("error_response", status=502)

        cfg = make_target_config(
            ip_list=[proxies[0].address],
            num_retries=0,
            ip_failures_until_quarantine=99,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        await submit_and_wait(mgr, upstream.url + "/test")
        await asyncio.sleep(0.1)

        failures = await backend.get_failures(cfg.name, proxies[0].address)
        assert failures >= 1

        await mgr.stop()

    async def test_all_proxies_failing_exhausts_retries(self, backend, proxies, upstream):
        proxies.set_all_mode("refuse")

        cfg = make_target_config(
            ip_list=proxies.ip_list,
            num_retries=2,
            ip_failures_until_quarantine=99,
            max_queue_wait=5.0,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        result = await submit_and_wait(mgr, upstream.url + "/test", num_retries=2, timeout=5.0)

        import json
        body = json.loads(result.body)
        assert result.status == 502
        assert body["retries_attempted"] == 2

        await mgr.stop()


# ---------------------------------------------------------------------------
# Retry behaviour
# ---------------------------------------------------------------------------

class TestRetryBehaviour:
    async def test_retry_uses_different_ip(self, backend, proxies, upstream):
        """After a failure on proxies[0], the retry uses proxies[1] (FIFO order).

        FIFO: proxies[0] is acquired first. After the failure it goes back
        to the pool with a cooldown. proxies[1] is next in the queue, so
        the retry uses it.
        """
        upstream.set_mode("http_error", status=503)

        cfg = make_target_config(
            ip_list=proxies.ip_list,
            num_retries=1,
            ip_failures_until_quarantine=99,
            min_request_interval=0.0,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        await submit_and_wait(mgr, upstream.url + "/test", num_retries=1)
        await asyncio.sleep(0.1)

        failures_0 = await backend.get_failures(cfg.name, proxies[0].address)
        failures_1 = await backend.get_failures(cfg.name, proxies[1].address)
        assert failures_0 == 1
        assert failures_1 == 1

        await mgr.stop()

    async def test_retry_succeeds_after_one_failure(self, backend, proxies, upstream):
        """If first proxy fails but second succeeds, request resolves 200."""
        proxies[0].set_mode("refuse")
        proxies[1].set_mode("forward")
        proxies[2].set_mode("forward")
        upstream.set_mode("normal")

        cfg = make_target_config(
            ip_list=proxies.ip_list,
            num_retries=2,
            ip_failures_until_quarantine=99,
            max_queue_wait=5.0,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        result = await submit_and_wait(mgr, upstream.url + "/ok", num_retries=2, timeout=5.0)
        assert result.status == 200

        await mgr.stop()


# ---------------------------------------------------------------------------
# Quarantine lifecycle
# ---------------------------------------------------------------------------

class TestQuarantineLifecycle:
    @pytest.mark.real_redis
    async def test_quarantined_ip_returns_to_pool_after_expiry(self, backend, proxies, upstream):
        """An IP that hits the failure threshold is quarantined, then released
        back into the pool once quarantine_time elapses.

        Uses a short quarantine_time (0.3s) and a fast sweep_interval (0.1s)
        so the test completes quickly without waiting for the default 5s tick.
        """
        upstream.set_mode("http_error", status=503)
        threshold = 2
        quarantine_time = 0.3

        cfg = make_target_config(
            ip_list=[proxies[0].address],
            num_retries=0,
            ip_failures_until_quarantine=threshold,
            quarantine_time=quarantine_time,
        )
        mgr = TargetManager(cfg, backend, quarantine_sweep_interval=0.1)
        await mgr.start()

        # Drive the IP into quarantine
        for _ in range(threshold):
            await submit_and_wait(mgr, upstream.url + "/test", num_retries=0)
            await asyncio.sleep(0.05)

        await asyncio.sleep(0.1)
        assert proxies[0].address in await backend.quarantine_list(cfg.name)

        # Wait for quarantine to expire and the pool sweeper to release it
        await asyncio.sleep(quarantine_time + 0.5)

        quarantined = await backend.quarantine_list(cfg.name)
        assert proxies[0].address not in quarantined

        # Confirm the IP is usable again — switch upstream to normal and send a request
        upstream.set_mode("normal")
        result = await submit_and_wait(mgr, upstream.url + "/test", timeout=3.0)
        assert result.status == 200

        await mgr.stop()


# ---------------------------------------------------------------------------
# Latency simulation
# ---------------------------------------------------------------------------

class TestLatency:
    async def test_slow_proxy_completes_without_failure(self, backend, proxies, upstream):
        """Slow but responding proxy should not increment failure count."""
        proxies[0].set_latency(0.2)
        upstream.set_mode("normal")

        cfg = make_target_config(
            ip_list=[proxies[0].address],
            num_retries=0,
            ip_failures_until_quarantine=99,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        result = await submit_and_wait(mgr, upstream.url + "/test", timeout=5.0)
        assert result.status == 200

        await asyncio.sleep(0.1)
        failures = await backend.get_failures(cfg.name, proxies[0].address)
        assert failures == 0

        await mgr.stop()

    async def test_slow_upstream_completes_without_failure(self, backend, proxies, upstream):
        """Slow upstream (not proxy) should not mark the proxy IP as failed."""
        upstream.set_mode("slow", delay=0.3)

        cfg = make_target_config(
            ip_list=[proxies[0].address],
            num_retries=0,
            ip_failures_until_quarantine=99,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        result = await submit_and_wait(mgr, upstream.url + "/slow", timeout=5.0)
        assert result.status == 200

        await asyncio.sleep(0.1)
        failures = await backend.get_failures(cfg.name, proxies[0].address)
        assert failures == 0

        await mgr.stop()


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

class TestGracefulShutdown:
    async def test_queued_requests_receive_503_on_shutdown(self, backend, proxies, upstream):
        upstream.set_mode("normal")

        cfg = make_target_config(
            ip_list=[proxies[0].address],
            max_queue_wait=30.0,
            num_retries=0,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        # Drain the pool so requests queue up waiting for an identity
        await backend.pop_identity_uuid(cfg.name, timeout=0.1)

        req = make_request(upstream.url + "/queued", max_queue_wait=30.0)
        await mgr.submit(req)

        await mgr.stop()

        assert req.future.done()
        result = req.future.result()
        assert result.status == 503
