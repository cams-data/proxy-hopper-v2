"""Tests for the background IP health prober (IPProber).

Verifies:
- IP deduplication across targets
- Probe success/failure recorded to metrics only (no pool side effects)
- round-robin URL rotation
- Lifecycle (start / stop)
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from proxy_hopper.config import TargetConfig
from proxy_hopper.prober import IPProber


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_target(name: str, ips: list[str]) -> TargetConfig:
    return TargetConfig(
        name=name,
        regex=".*",
        ip_list=ips,
        min_request_interval=0.0,
        max_queue_wait=5.0,
        num_retries=0,
        ip_failures_until_quarantine=3,
        quarantine_time=60.0,
    )


# ---------------------------------------------------------------------------
# IP deduplication
# ---------------------------------------------------------------------------

class TestIPDeduplication:
    def test_deduplicates_same_ip_across_targets(self):
        shared_ip = "1.2.3.4:8080"
        t1 = _make_target("t1", [shared_ip, "5.6.7.8:8080"])
        t2 = _make_target("t2", [shared_ip, "9.10.11.12:8080"])

        prober = IPProber([t1, t2], probe_urls=["http://example.com"])

        assert prober._addresses.count(shared_ip) == 1
        assert len(prober._addresses) == 3

    def test_no_targets_yields_empty_address_list(self):
        prober = IPProber([], probe_urls=["http://example.com"])
        assert prober._addresses == []

    def test_single_target_all_ips_included(self):
        t = _make_target("t", ["1.1.1.1:80", "2.2.2.2:80", "3.3.3.3:80"])
        prober = IPProber([t], probe_urls=["http://example.com"])
        assert set(prober._addresses) == {"1.1.1.1:80", "2.2.2.2:80", "3.3.3.3:80"}


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_task(self):
        t = _make_target("t", ["1.1.1.1:80"])
        prober = IPProber([t], probe_urls=["http://example.com"], interval=9999)

        await prober.start()
        try:
            assert prober._task is not None
            assert not prober._task.done()
        finally:
            await prober.stop()

    @pytest.mark.asyncio
    async def test_start_noop_when_no_addresses(self):
        prober = IPProber([], probe_urls=["http://example.com"])
        await prober.start()
        assert prober._task is None

    @pytest.mark.asyncio
    async def test_start_noop_when_no_probe_urls(self):
        t = _make_target("t", ["1.1.1.1:80"])
        prober = IPProber([t], probe_urls=[])
        await prober.start()
        assert prober._task is None

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self):
        t = _make_target("t", ["1.1.1.1:80"])
        prober = IPProber([t], probe_urls=["http://example.com"], interval=9999)

        await prober.start()
        await prober.stop()

        assert prober._task is None or prober._task.cancelled() or prober._task.done()


# ---------------------------------------------------------------------------
# Probe results go to metrics only
# ---------------------------------------------------------------------------

class TestProbeMetrics:
    @pytest.mark.asyncio
    async def test_success_recorded_to_metrics_not_pool(self):
        """A successful probe must call record_probe_success, nothing else."""
        t = _make_target("t", ["1.1.1.1:80"])
        prober = IPProber([t], probe_urls=["https://1.1.1.1"], interval=9999, timeout=5.0)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        recorded_successes: list[tuple[str, float]] = []
        recorded_failures: list[Any] = []

        mock_metrics = MagicMock()
        mock_metrics.record_probe_success = lambda addr, dur: recorded_successes.append((addr, dur))
        mock_metrics.record_probe_failure = lambda *a: recorded_failures.append(a)

        with (
            patch("proxy_hopper.prober.aiohttp.ClientSession", return_value=mock_session),
            patch("proxy_hopper.prober.get_metrics", return_value=mock_metrics),
        ):
            await prober._probe_address("1.1.1.1:80")

        assert len(recorded_successes) == 1
        assert recorded_successes[0][0] == "1.1.1.1:80"
        assert recorded_failures == []

    @pytest.mark.asyncio
    async def test_timeout_recorded_as_failure(self):
        t = _make_target("t", ["1.1.1.1:80"])
        prober = IPProber([t], probe_urls=["https://1.1.1.1"], interval=9999, timeout=5.0)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=asyncio.TimeoutError)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        recorded_failures: list[tuple] = []
        mock_metrics = MagicMock()
        mock_metrics.record_probe_success = MagicMock()
        mock_metrics.record_probe_failure = lambda addr, reason, dur: recorded_failures.append((addr, reason))

        with (
            patch("proxy_hopper.prober.aiohttp.ClientSession", return_value=mock_session),
            patch("proxy_hopper.prober.get_metrics", return_value=mock_metrics),
        ):
            await prober._probe_address("1.1.1.1:80")

        assert len(recorded_failures) == 1
        assert recorded_failures[0] == ("1.1.1.1:80", "timeout")
        mock_metrics.record_probe_success.assert_not_called()

    @pytest.mark.asyncio
    async def test_5xx_recorded_as_failure(self):
        t = _make_target("t", ["1.1.1.1:80"])
        prober = IPProber([t], probe_urls=["https://1.1.1.1"], interval=9999, timeout=5.0)

        mock_resp = AsyncMock()
        mock_resp.status = 502
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        recorded_failures: list[tuple] = []
        mock_metrics = MagicMock()
        mock_metrics.record_probe_success = MagicMock()
        mock_metrics.record_probe_failure = lambda addr, reason, dur: recorded_failures.append((addr, reason))

        with (
            patch("proxy_hopper.prober.aiohttp.ClientSession", return_value=mock_session),
            patch("proxy_hopper.prober.get_metrics", return_value=mock_metrics),
        ):
            await prober._probe_address("1.1.1.1:80")

        assert len(recorded_failures) == 1
        assert recorded_failures[0] == ("1.1.1.1:80", "http_error")
        mock_metrics.record_probe_success.assert_not_called()

    @pytest.mark.asyncio
    async def test_4xx_treated_as_success(self):
        """A 404 from the target still means the proxy IP is forwarding traffic."""
        t = _make_target("t", ["1.1.1.1:80"])
        prober = IPProber([t], probe_urls=["https://1.1.1.1"], interval=9999, timeout=5.0)

        mock_resp = AsyncMock()
        mock_resp.status = 404
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        recorded_successes: list[Any] = []
        mock_metrics = MagicMock()
        mock_metrics.record_probe_success = lambda addr, dur: recorded_successes.append(addr)
        mock_metrics.record_probe_failure = MagicMock()

        with (
            patch("proxy_hopper.prober.aiohttp.ClientSession", return_value=mock_session),
            patch("proxy_hopper.prober.get_metrics", return_value=mock_metrics),
        ):
            await prober._probe_address("1.1.1.1:80")

        assert "1.1.1.1:80" in recorded_successes
        mock_metrics.record_probe_failure.assert_not_called()


# ---------------------------------------------------------------------------
# URL rotation
# ---------------------------------------------------------------------------

class TestUrlRotation:
    def test_urls_rotate_round_robin(self):
        t = _make_target("t", ["1.1.1.1:80"])
        urls = ["http://a.example", "http://b.example", "http://c.example"]
        prober = IPProber([t], probe_urls=urls)

        cycle = prober._url_cycles["1.1.1.1:80"]
        seen = [next(cycle) for _ in range(6)]
        assert seen == urls + urls

    def test_each_address_has_independent_cycle(self):
        t = _make_target("t", ["1.1.1.1:80", "2.2.2.2:80"])
        urls = ["http://a.example", "http://b.example"]
        prober = IPProber([t], probe_urls=urls)

        # Advance first address's cycle once
        next(prober._url_cycles["1.1.1.1:80"])

        # Second address cycle should still be at the first URL
        assert next(prober._url_cycles["2.2.2.2:80"]) == urls[0]
