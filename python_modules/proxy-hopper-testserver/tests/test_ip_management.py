"""Integration tests for runtime IP pool management.

These tests verify the observable behaviour when IPs are added, removed, or
swapped in a running TargetManager.  Management operations go through the
TargetManager public API (add_address / retire_address), which is the same
path ProxyServer uses when it applies hot-reload config change events.

Pool drain helper
-----------------
Some tests need to exhaust the pool to simulate "all IPs busy / removed".
Since the pool queue now stores UUID strings (not address strings), the helper
pops UUIDs until the queue is empty, discarding them.  This is equivalent to
temporarily removing every identity from service — the manager cannot dispatch
new requests until UUIDs are returned or new ones are pushed.

Test matrix
-----------
Remove-IP:
  - removing the only IP causes subsequent requests to fail with 503
  - removing one of many IPs reduces available concurrency
  - an in-flight request via a removed IP still completes before the pool drains

Add-IP:
  - adding an IP to an exhausted pool allows requests to succeed
  - adding IPs increases concurrent throughput

Swap-IP:
  - old broken proxy retired; new healthy proxy added — requests recover
  - new IP receives traffic after old IP is retired
  - swap with a working old proxy and a broken new proxy makes requests fail

Pool exhaustion:
  - all IPs removed → every request fails with 503 / no_ip_available
  - restoring one IP → service recovers
  - queue-wait timeout gives 503 when pool stays empty
"""

from __future__ import annotations

import asyncio
import time

import pytest

from proxy_hopper.models import PendingRequest
from proxy_hopper.target_manager import TargetManager
from proxy_hopper_testserver import MockProxyPool, UpstreamServer

from conftest import make_target_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(
    url: str,
    max_queue_wait: float = 5.0,
    num_retries: int = 0,
) -> PendingRequest:
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


async def _submit(
    manager: TargetManager,
    url: str,
    *,
    num_retries: int = 0,
    timeout: float = 5.0,
    max_queue_wait: float | None = None,
):
    req = _make_request(
        url,
        num_retries=num_retries,
        max_queue_wait=max_queue_wait if max_queue_wait is not None else timeout,
    )
    await manager.submit(req)
    return await asyncio.wait_for(req.future, timeout=timeout)


async def _drain_pool(backend, target_name: str) -> int:
    """Pop every UUID from the pool queue and return the count drained.

    UUIDs are discarded — this leaves the pool empty so that subsequent
    acquire() calls will block until new identities are created and pushed.
    """
    count = 0
    while True:
        uuid = await backend.pop_identity_uuid(target_name, timeout=0.05)
        if uuid is None:
            break
        count += 1
    return count


# ---------------------------------------------------------------------------
# Remove-IP scenarios
# ---------------------------------------------------------------------------

class TestRemoveIp:

    async def test_removing_only_ip_causes_requests_to_fail(
        self, backend, proxies, upstream
    ):
        """After the pool's only IP is removed, requests get 503 no_ip_available."""
        upstream.set_mode("normal")
        cfg = make_target_config(
            ip_list=[proxies[0].address],
            max_queue_wait=1.0,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        # Retire the only identity — acquire() will discard it and find nothing.
        await mgr.retire_address(proxies[0].address)

        result = await _submit(mgr, upstream.url + "/test", timeout=2.0, max_queue_wait=1.0)
        assert result.status == 503

        import json
        body = json.loads(result.body)
        assert body["error"] in ("no_ip_available", "queue_timeout")

        await mgr.stop()

    async def test_removing_one_of_many_ips_reduces_pool_size(
        self, backend, proxies, upstream
    ):
        """After removing one IP the pool size shrinks by one."""
        upstream.set_mode("normal")
        cfg = make_target_config(ip_list=proxies.ip_list)
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        size_before = await backend.pool_size(cfg.name)

        # Pop one UUID — simulates draining one identity from the pool.
        removed = await backend.pop_identity_uuid(cfg.name, timeout=0.1)
        assert removed is not None

        size_after = await backend.pool_size(cfg.name)
        assert size_after == size_before - 1

        # Remaining IPs still serve requests.
        result = await _submit(mgr, upstream.url + "/ok")
        assert result.status == 200

        await mgr.stop()

    async def test_inflight_request_completes_before_pool_drains(
        self, backend, proxies, upstream
    ):
        """A request that's already dispatched (in-flight) completes even if
        the pool is drained concurrently — pool drain only starves new requests."""
        upstream.set_mode("slow", delay=0.3)
        cfg = make_target_config(
            ip_list=[proxies[0].address],
            min_request_interval=0.0,
            max_queue_wait=5.0,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        # Submit the request — it dispatches immediately (identity available).
        req = _make_request(upstream.url + "/slow", max_queue_wait=5.0)
        await mgr.submit(req)

        # While the request is in-flight (upstream taking 0.3s), drain the pool.
        # This simulates a concurrent remove_ip arriving mid-request.
        await asyncio.sleep(0.05)
        await _drain_pool(backend, cfg.name)

        # The in-flight request should still complete successfully.
        result = await asyncio.wait_for(req.future, timeout=5.0)
        assert result.status == 200

        await mgr.stop()

    async def test_removed_proxy_no_longer_receives_traffic(
        self, backend, proxies, upstream
    ):
        """After retiring a proxy, it no longer handles requests."""
        upstream.set_mode("normal")
        cfg = make_target_config(
            ip_list=[proxies[0].address, proxies[1].address],
            min_request_interval=0.0,
            max_queue_wait=5.0,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        # Retire proxy[0] — acquire() will discard its UUID and use proxy[1].
        await mgr.retire_address(proxies[0].address)

        # proxy[0] in refuse mode — if it somehow received a request, it would fail.
        proxies[0].set_mode("refuse")
        proxies[1].set_mode("forward")

        result = await _submit(mgr, upstream.url + "/test")
        assert result.status == 200

        await mgr.stop()


# ---------------------------------------------------------------------------
# Add-IP scenarios
# ---------------------------------------------------------------------------

class TestAddIp:

    async def test_add_ip_to_exhausted_pool_allows_requests(
        self, backend, proxies, upstream
    ):
        """Adding an IP into an empty pool restores service."""
        upstream.set_mode("normal")
        proxies[0].set_mode("forward")

        cfg = make_target_config(
            ip_list=[proxies[0].address],
            max_queue_wait=2.0,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        # Exhaust the pool.
        await _drain_pool(backend, cfg.name)

        # Immediately confirm requests fail.
        result = await _submit(
            mgr, upstream.url + "/fail", timeout=2.0, max_queue_wait=0.5
        )
        assert result.status == 503

        # Add the IP back — creates a fresh identity and pushes its UUID.
        await mgr.add_address(proxies[0].address)

        # Now requests should succeed.
        result = await _submit(mgr, upstream.url + "/ok", timeout=5.0)
        assert result.status == 200

        await mgr.stop()

    async def test_add_second_ip_allows_concurrent_requests(
        self, backend, proxies, upstream
    ):
        """Adding a second IP lets two requests dispatch simultaneously."""
        upstream.set_mode("slow", delay=0.2)
        proxies[0].set_mode("forward")
        proxies[1].set_mode("forward")

        # Start with only one IP.
        cfg = make_target_config(
            ip_list=[proxies[0].address],
            min_request_interval=0.0,
            max_queue_wait=5.0,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        # Add a second IP — creates an identity and pushes its UUID to the queue.
        await mgr.add_address(proxies[1].address)

        start = time.monotonic()
        r1, r2 = await asyncio.gather(
            _submit(mgr, upstream.url + "/a", timeout=5.0),
            _submit(mgr, upstream.url + "/b", timeout=5.0),
        )
        elapsed = time.monotonic() - start

        assert r1.status == 200
        assert r2.status == 200
        # Two concurrent requests with a 0.2s upstream each — serial would be
        # ~0.4s; concurrent should finish well under 0.35s.
        assert elapsed < 0.35, f"Expected concurrent dispatch, took {elapsed:.3f}s"

        await mgr.stop()

    async def test_add_ip_with_broken_proxy_new_ip_handles_traffic(
        self, backend, proxies, upstream
    ):
        """Old proxy breaks, new IP added — requests succeed again via the new IP."""
        upstream.set_mode("normal")

        cfg = make_target_config(
            ip_list=[proxies[0].address],
            max_queue_wait=5.0,
            num_retries=1,
            ip_failures_until_quarantine=99,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        # Break proxy[0] — it will start returning 502.
        proxies[0].set_mode("error_response", status=502)

        # Add a healthy proxy[1] as replacement.
        proxies[1].set_mode("forward")
        await mgr.add_address(proxies[1].address)

        # With 1 retry, the request should fail on proxy[0] then succeed on proxy[1].
        result = await _submit(mgr, upstream.url + "/test", num_retries=1, timeout=5.0)
        assert result.status == 200

        await mgr.stop()


# ---------------------------------------------------------------------------
# Swap-IP scenarios
# ---------------------------------------------------------------------------

class TestSwapIp:

    async def test_swap_broken_proxy_for_healthy_one(
        self, backend, proxies, upstream
    ):
        """Retire the broken proxy, add a healthy one — requests recover."""
        upstream.set_mode("normal")
        proxies[0].set_mode("refuse")
        proxies[1].set_mode("forward")

        cfg = make_target_config(
            ip_list=[proxies[0].address],
            max_queue_wait=5.0,
            num_retries=0,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        # Confirm currently broken.
        result = await _submit(mgr, upstream.url + "/test", timeout=3.0)
        assert result.status == 502

        # Swap: retire old broken IP, add healthy new IP.
        await mgr.retire_address(proxies[0].address)
        await mgr.add_address(proxies[1].address)

        result = await _submit(mgr, upstream.url + "/test", timeout=5.0)
        assert result.status == 200

        await mgr.stop()

    async def test_swap_healthy_proxy_for_broken_one_makes_requests_fail(
        self, backend, proxies, upstream
    ):
        """Retire the working proxy, add a broken one — requests start failing."""
        upstream.set_mode("normal")
        proxies[0].set_mode("forward")
        proxies[1].set_mode("refuse")

        cfg = make_target_config(
            ip_list=[proxies[0].address],
            max_queue_wait=5.0,
            num_retries=0,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        # Working before the swap.
        result = await _submit(mgr, upstream.url + "/test", timeout=3.0)
        assert result.status == 200

        # Swap: drain proxy[0]'s UUID from queue (it returned after the success),
        # retire it, and add broken proxy[1].
        await _drain_pool(backend, cfg.name)
        await mgr.retire_address(proxies[0].address)
        await mgr.add_address(proxies[1].address)

        result = await _submit(mgr, upstream.url + "/test", timeout=3.0)
        assert result.status in (502, 503)

        await mgr.stop()

    async def test_swap_ip_old_drains_naturally(
        self, backend, proxies, upstream
    ):
        """After swap, old and new IPs each serve requests — both succeed."""
        upstream.set_mode("normal")
        proxies[0].set_mode("forward")  # original IP
        proxies[1].set_mode("forward")  # new IP

        cfg = make_target_config(
            ip_list=[proxies[0].address],
            min_request_interval=0.0,
            max_queue_wait=5.0,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        # Simulate swap: add new IP alongside old one.
        await mgr.add_address(proxies[1].address)

        # Both requests succeed — one per identity.
        r1 = await _submit(mgr, upstream.url + "/a", timeout=5.0)
        r2 = await _submit(mgr, upstream.url + "/b", timeout=5.0)
        assert r1.status == 200
        assert r2.status == 200

        await mgr.stop()


# ---------------------------------------------------------------------------
# Pool exhaustion and recovery
# ---------------------------------------------------------------------------

class TestPoolExhaustion:

    async def test_all_ips_removed_all_requests_fail(
        self, backend, proxies, upstream
    ):
        """When the pool is completely drained every request fails with 503."""
        upstream.set_mode("normal")
        cfg = make_target_config(
            ip_list=proxies.ip_list,
            max_queue_wait=0.5,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        # Drain every UUID.
        await _drain_pool(backend, cfg.name)

        results = await asyncio.gather(*[
            _submit(mgr, upstream.url + "/test", timeout=2.0, max_queue_wait=0.5)
            for _ in range(3)
        ])
        for r in results:
            assert r.status == 503

        await mgr.stop()

    async def test_restore_ip_after_exhaustion_resumes_service(
        self, backend, proxies, upstream
    ):
        """After full pool exhaustion, adding one IP back resumes service."""
        upstream.set_mode("normal")
        proxies[0].set_mode("forward")

        cfg = make_target_config(
            ip_list=[proxies[0].address],
            max_queue_wait=5.0,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        # Exhaust pool.
        await _drain_pool(backend, cfg.name)

        # Fail fast to confirm exhaustion.
        fail_result = await _submit(
            mgr, upstream.url + "/test", timeout=1.0, max_queue_wait=0.3
        )
        assert fail_result.status == 503

        # Restore IP — creates a fresh identity and pushes its UUID.
        await mgr.add_address(proxies[0].address)

        # Wait for the next dispatcher iteration, then verify recovery.
        await asyncio.sleep(0.05)
        result = await _submit(mgr, upstream.url + "/ok", timeout=5.0)
        assert result.status == 200

        await mgr.stop()

    async def test_queue_wait_timeout_gives_503_while_pool_empty(
        self, backend, proxies, upstream
    ):
        """A request that waits in queue past max_queue_wait gets a 503."""
        upstream.set_mode("normal")
        cfg = make_target_config(
            ip_list=[proxies[0].address],
            max_queue_wait=0.3,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        # Drain pool so the request must wait.
        await _drain_pool(backend, cfg.name)

        start = time.monotonic()
        result = await _submit(
            mgr, upstream.url + "/test", timeout=2.0, max_queue_wait=0.3
        )
        elapsed = time.monotonic() - start

        assert result.status == 503
        # Should have timed out near max_queue_wait, not hung until our timeout.
        assert elapsed < 1.5

        await mgr.stop()

    async def test_partial_pool_loss_does_not_stop_service(
        self, backend, proxies, upstream
    ):
        """Losing half the pool still allows remaining IPs to handle traffic."""
        upstream.set_mode("normal")
        cfg = make_target_config(
            ip_list=proxies.ip_list,  # 3 IPs
            min_request_interval=0.0,
            max_queue_wait=5.0,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        # Drain two of the three UUIDs.
        for _ in range(2):
            uuid = await backend.pop_identity_uuid(cfg.name, timeout=0.1)
            assert uuid is not None

        size = await backend.pool_size(cfg.name)
        assert size == 1

        # One remaining identity should still serve requests.
        result = await _submit(mgr, upstream.url + "/test", timeout=5.0)
        assert result.status == 200

        await mgr.stop()

    async def test_new_ip_added_while_requests_queued_unblocks_them(
        self, backend, proxies, upstream
    ):
        """Requests queued behind an empty pool unblock when a new IP is added."""
        upstream.set_mode("normal")
        proxies[0].set_mode("forward")

        cfg = make_target_config(
            ip_list=[proxies[0].address],
            min_request_interval=0.0,
            max_queue_wait=5.0,
        )
        mgr = TargetManager(cfg, backend)
        await mgr.start()

        # Drain the pool — requests will queue.
        await _drain_pool(backend, cfg.name)

        # Submit two requests that will wait in the queue.
        req1 = _make_request(upstream.url + "/q1", max_queue_wait=5.0)
        req2 = _make_request(upstream.url + "/q2", max_queue_wait=5.0)
        await mgr.submit(req1)
        await mgr.submit(req2)

        # After a short delay, add an IP — creates identity and pushes UUID,
        # unblocking the dispatcher.  With min_request_interval=0.0, the UUID
        # returns to the queue immediately after the first request, serving both.
        await asyncio.sleep(0.1)
        await mgr.add_address(proxies[0].address)

        # Both should eventually resolve (sequentially via the one IP).
        r1 = await asyncio.wait_for(req1.future, timeout=5.0)
        r2 = await asyncio.wait_for(req2.future, timeout=5.0)
        assert r1.status == 200
        assert r2.status == 200

        await mgr.stop()
