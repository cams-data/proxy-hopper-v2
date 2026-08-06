"""IpHealthStore contract tests.

Every test in this module runs against *each* registered backend type via
the parametrized ``ip_health`` fixture — see conftest.py.
"""

from __future__ import annotations

import asyncio
from datetime import datetime


class TestIpHealthStore:
    async def test_unknown_before_any_record(self, ip_health):
        result = await ip_health.get_many(["1.2.3.4:8080"])
        snap = result["1.2.3.4:8080"]
        assert snap.address == "1.2.3.4:8080"
        assert snap.status is None
        assert snap.provider is None
        assert snap.last_check_at is None
        assert snap.reason is None

    async def test_record_success_sets_status_up(self, ip_health):
        await ip_health.record("1.2.3.4:8080", success=True, provider="p1")
        snap = (await ip_health.get_many(["1.2.3.4:8080"]))["1.2.3.4:8080"]
        assert snap.status == "up"
        assert snap.provider == "p1"
        assert snap.reason is None
        assert snap.last_check_at is not None
        datetime.fromisoformat(snap.last_check_at)  # must parse

    async def test_record_failure_sets_status_down_with_reason(self, ip_health):
        await ip_health.record("1.2.3.4:8080", success=False, provider="p1", reason="timeout")
        snap = (await ip_health.get_many(["1.2.3.4:8080"]))["1.2.3.4:8080"]
        assert snap.status == "down"
        assert snap.reason == "timeout"

    async def test_later_record_overwrites_earlier(self, ip_health):
        await ip_health.record("1.2.3.4:8080", success=False, provider="p1", reason="timeout")
        await ip_health.record("1.2.3.4:8080", success=True, provider="p1")
        snap = (await ip_health.get_many(["1.2.3.4:8080"]))["1.2.3.4:8080"]
        assert snap.status == "up"
        assert snap.reason is None

    async def test_get_many_covers_every_requested_address(self, ip_health):
        await ip_health.record("1.1.1.1:8080", success=True, provider="p1")
        result = await ip_health.get_many(["1.1.1.1:8080", "2.2.2.2:8080"])
        assert set(result) == {"1.1.1.1:8080", "2.2.2.2:8080"}
        assert result["1.1.1.1:8080"].status == "up"
        assert result["2.2.2.2:8080"].status is None

    async def test_get_many_ignores_addresses_not_requested(self, ip_health):
        await ip_health.record("1.1.1.1:8080", success=True, provider="p1")
        await ip_health.record("2.2.2.2:8080", success=False, provider="p1", reason="timeout")
        result = await ip_health.get_many(["1.1.1.1:8080"])
        assert set(result) == {"1.1.1.1:8080"}

    async def test_independent_addresses(self, ip_health):
        await ip_health.record("1.1.1.1:8080", success=True, provider="p1")
        await ip_health.record("2.2.2.2:8080", success=False, provider="p2", reason="connection_error")
        result = await ip_health.get_many(["1.1.1.1:8080", "2.2.2.2:8080"])
        assert result["1.1.1.1:8080"].status == "up"
        assert result["1.1.1.1:8080"].provider == "p1"
        assert result["2.2.2.2:8080"].status == "down"
        assert result["2.2.2.2:8080"].provider == "p2"

    async def test_concurrent_records_do_not_corrupt_entries(self, ip_health):
        await asyncio.gather(*[
            ip_health.record(f"{i}.1.1.1:8080", success=True, provider="p1")
            for i in range(10)
        ])
        result = await ip_health.get_many([f"{i}.1.1.1:8080" for i in range(10)])
        assert all(snap.status == "up" for snap in result.values())
