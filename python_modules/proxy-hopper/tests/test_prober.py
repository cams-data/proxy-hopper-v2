"""Tests for the background IP health prober (IPProber).

Verifies:
- IP deduplication across providers and targets
- Probe success/failure recorded to metrics only (no pool side effects)
- round-robin URL rotation
- Lifecycle (start / stop)
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from proxy_hopper.config import ProxyProvider, ResolvedIP
from proxy_hopper.prober import IPProber, _ProbeEntry

from test_helpers import make_target_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_provider(name: str, ips: list[str], region: str = "") -> ProxyProvider:
    return ProxyProvider(
        name=name,
        ip_list=ips,
        region_tag=region or None,
    )


# ---------------------------------------------------------------------------
# IP deduplication
# ---------------------------------------------------------------------------

class TestIPDeduplication:
    def test_deduplicates_same_ip_across_providers(self):
        shared_ip = "1.2.3.4:8080"
        p1 = _make_provider("p1", [shared_ip, "5.6.7.8:8080"])
        p2 = _make_provider("p2", [shared_ip, "9.10.11.12:8080"])

        prober = IPProber(providers=[p1, p2], probe_urls=["http://example.com"])

        addresses = [e.address for e in prober._entries]
        assert addresses.count(shared_ip) == 1
        assert len(addresses) == 3

    def test_no_providers_or_targets_yields_empty(self):
        prober = IPProber(probe_urls=["http://example.com"])
        assert prober._entries == []

    def test_single_provider_all_ips_included(self):
        p = _make_provider("p", ["1.1.1.1:80", "2.2.2.2:80", "3.3.3.3:80"])
        prober = IPProber(providers=[p], probe_urls=["http://example.com"])
        addresses = {e.address for e in prober._entries}
        assert addresses == {"1.1.1.1:80", "2.2.2.2:80", "3.3.3.3:80"}

    def test_provider_metadata_carried_through(self):
        p = _make_provider("my-provider", ["1.1.1.1:8080"], region="Australia")
        prober = IPProber(providers=[p], probe_urls=["http://example.com"])
        entry = prober._entries[0]
        assert entry.provider == "my-provider"
        assert entry.region == "Australia"

    def test_inline_target_fallback(self):
        t = make_target_config(["1.2.3.4:8080"], name="t")
        prober = IPProber(targets=[t], probe_urls=["http://example.com"])
        assert len(prober._entries) == 1
        entry = prober._entries[0]
        assert entry.address == "1.2.3.4:8080"
        assert entry.provider == ""

    def test_target_ips_not_duplicated_with_provider_ips(self):
        p = _make_provider("p", ["1.2.3.4:8080"])
        t = make_target_config(["1.2.3.4:8080", "5.6.7.8:8080"], name="t")
        prober = IPProber(providers=[p], targets=[t], probe_urls=["http://example.com"])
        addresses = [e.address for e in prober._entries]
        # 1.2.3.4 from provider, 5.6.7.8 from target fallback (not duplicated)
        assert addresses.count("1.2.3.4:8080") == 1
        assert "5.6.7.8:8080" in addresses


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_task(self):
        p = _make_provider("p", ["1.1.1.1:80"])
        prober = IPProber(providers=[p], probe_urls=["http://example.com"], interval=9999)

        await prober.start()
        try:
            assert prober._task is not None
            assert not prober._task.done()
        finally:
            await prober.stop()

    @pytest.mark.asyncio
    async def test_start_noop_when_no_addresses(self):
        prober = IPProber(probe_urls=["http://example.com"])
        await prober.start()
        assert prober._task is None

    @pytest.mark.asyncio
    async def test_start_noop_when_no_probe_urls(self):
        p = _make_provider("p", ["1.1.1.1:80"])
        prober = IPProber(providers=[p], probe_urls=[])
        await prober.start()
        assert prober._task is None

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self):
        p = _make_provider("p", ["1.1.1.1:80"])
        prober = IPProber(providers=[p], probe_urls=["http://example.com"], interval=9999)

        await prober.start()
        await prober.stop()

        assert prober._task is None or prober._task.cancelled() or prober._task.done()


# ---------------------------------------------------------------------------
# Probe results go to metrics only
# ---------------------------------------------------------------------------

class TestProbeMetrics:
    def _make_entry(self, address: str, provider: str = "p", region: str = "AU") -> _ProbeEntry:
        return _ProbeEntry(address=address, provider=provider, region=region)

    @pytest.mark.asyncio
    async def test_success_recorded_to_metrics_not_pool(self):
        """A successful probe must call record_probe_success with provider/region."""
        p = _make_provider("p", ["1.1.1.1:80"])
        prober = IPProber(providers=[p], probe_urls=["https://1.1.1.1"], interval=9999, timeout=5.0)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        recorded_successes: list[tuple] = []
        recorded_failures: list[Any] = []

        mock_metrics = MagicMock()
        mock_metrics.record_probe_success = lambda addr, dur, **kw: recorded_successes.append((addr, kw))
        mock_metrics.record_probe_failure = lambda *a, **kw: recorded_failures.append(a)

        prober._session = mock_session
        entry = self._make_entry("1.1.1.1:80")
        with patch("proxy_hopper.prober.get_metrics", return_value=mock_metrics):
            await prober._probe_address(entry)

        assert len(recorded_successes) == 1
        assert recorded_successes[0][0] == "1.1.1.1:80"
        assert recorded_successes[0][1]["provider"] == "p"
        assert recorded_successes[0][1]["region"] == "AU"
        assert recorded_failures == []

    @pytest.mark.asyncio
    async def test_timeout_recorded_as_failure(self):
        p = _make_provider("p", ["1.1.1.1:80"])
        prober = IPProber(providers=[p], probe_urls=["https://1.1.1.1"], interval=9999, timeout=5.0)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=asyncio.TimeoutError)

        recorded_failures: list[tuple] = []
        mock_metrics = MagicMock()
        mock_metrics.record_probe_success = MagicMock()
        mock_metrics.record_probe_failure = lambda addr, reason, dur, **kw: recorded_failures.append((addr, reason))

        prober._session = mock_session
        entry = self._make_entry("1.1.1.1:80")
        with patch("proxy_hopper.prober.get_metrics", return_value=mock_metrics):
            await prober._probe_address(entry)

        assert len(recorded_failures) == 1
        assert recorded_failures[0] == ("1.1.1.1:80", "timeout")
        mock_metrics.record_probe_success.assert_not_called()

    @pytest.mark.asyncio
    async def test_5xx_recorded_as_failure(self):
        p = _make_provider("p", ["1.1.1.1:80"])
        prober = IPProber(providers=[p], probe_urls=["https://1.1.1.1"], interval=9999, timeout=5.0)

        mock_resp = AsyncMock()
        mock_resp.status = 502
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        recorded_failures: list[tuple] = []
        mock_metrics = MagicMock()
        mock_metrics.record_probe_success = MagicMock()
        mock_metrics.record_probe_failure = lambda addr, reason, dur, **kw: recorded_failures.append((addr, reason))

        prober._session = mock_session
        entry = self._make_entry("1.1.1.1:80")
        with patch("proxy_hopper.prober.get_metrics", return_value=mock_metrics):
            await prober._probe_address(entry)

        assert len(recorded_failures) == 1
        assert recorded_failures[0] == ("1.1.1.1:80", "http_error")
        mock_metrics.record_probe_success.assert_not_called()

    @pytest.mark.asyncio
    async def test_4xx_treated_as_success(self):
        """A 404 from the target still means the proxy IP is forwarding traffic."""
        p = _make_provider("p", ["1.1.1.1:80"])
        prober = IPProber(providers=[p], probe_urls=["https://1.1.1.1"], interval=9999, timeout=5.0)

        mock_resp = AsyncMock()
        mock_resp.status = 404
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        recorded_successes: list[Any] = []
        mock_metrics = MagicMock()
        mock_metrics.record_probe_success = lambda addr, dur, **kw: recorded_successes.append(addr)
        mock_metrics.record_probe_failure = MagicMock()

        prober._session = mock_session
        entry = self._make_entry("1.1.1.1:80")
        with patch("proxy_hopper.prober.get_metrics", return_value=mock_metrics):
            await prober._probe_address(entry)

        assert "1.1.1.1:80" in recorded_successes
        mock_metrics.record_probe_failure.assert_not_called()


# ---------------------------------------------------------------------------
# URL rotation
# ---------------------------------------------------------------------------

class TestUrlRotation:
    def test_urls_rotate_round_robin(self):
        p = _make_provider("p", ["1.1.1.1:80"])
        urls = ["http://a.example", "http://b.example", "http://c.example"]
        prober = IPProber(providers=[p], probe_urls=urls)

        cycle = prober._url_cycles["1.1.1.1:80"]
        seen = [next(cycle) for _ in range(6)]
        assert seen == urls + urls

    def test_each_address_has_independent_cycle(self):
        p = _make_provider("p", ["1.1.1.1:80", "2.2.2.2:80"])
        urls = ["http://a.example", "http://b.example"]
        prober = IPProber(providers=[p], probe_urls=urls)

        # Advance first address's cycle once
        next(prober._url_cycles["1.1.1.1:80"])

        # Second address cycle should still be at the first URL
        assert next(prober._url_cycles["2.2.2.2:80"]) == urls[0]
