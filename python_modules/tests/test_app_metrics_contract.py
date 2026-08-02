"""AppMetricsStore contract tests.

Every test in this module runs against *each* registered backend type via the
parametrized ``app_metrics`` fixture — see conftest.py. Also exercises
Backend.counter_increment_by (no IPPoolStore wrapper exists for it, so it has
no dedicated place in test_backend_contract.py's "IPPoolStore interface only"
scope).
"""

from __future__ import annotations

import asyncio


class TestAppMetricsStore:
    async def test_get_all_zero_before_any_record(self, app_metrics):
        snap = await app_metrics.get("t")
        assert snap.name == "t"
        assert snap.total_requests == 0
        assert snap.success_requests == 0
        assert snap.failed_requests == 0
        assert snap.avg_latency_ms == 0.0
        assert snap.last_request_at is None

    async def test_record_success_increments_total_and_success(self, app_metrics):
        await app_metrics.record("t", success=True, elapsed_seconds=0.1)
        snap = await app_metrics.get("t")
        assert snap.total_requests == 1
        assert snap.success_requests == 1
        assert snap.failed_requests == 0

    async def test_record_failure_increments_total_and_failed(self, app_metrics):
        await app_metrics.record("t", success=False, elapsed_seconds=0.1)
        snap = await app_metrics.get("t")
        assert snap.total_requests == 1
        assert snap.success_requests == 0
        assert snap.failed_requests == 1

    async def test_record_sets_last_request_at(self, app_metrics):
        assert (await app_metrics.get("t")).last_request_at is None
        await app_metrics.record("t", success=True, elapsed_seconds=0.05)
        snap = await app_metrics.get("t")
        assert snap.last_request_at is not None
        # ISO-8601 UTC — must at least parse.
        from datetime import datetime
        datetime.fromisoformat(snap.last_request_at)

    async def test_avg_latency_ms_averages_across_records(self, app_metrics):
        await app_metrics.record("t", success=True, elapsed_seconds=0.100)  # 100ms
        await app_metrics.record("t", success=True, elapsed_seconds=0.300)  # 300ms
        snap = await app_metrics.get("t")
        assert snap.total_requests == 2
        assert snap.avg_latency_ms == 200.0

    async def test_zero_elapsed_seconds_does_not_break_avg(self, app_metrics):
        await app_metrics.record("t", success=True, elapsed_seconds=0.0)
        snap = await app_metrics.get("t")
        assert snap.total_requests == 1
        assert snap.avg_latency_ms == 0.0

    async def test_independent_targets(self, app_metrics):
        await app_metrics.record("a", success=True, elapsed_seconds=0.1)
        await app_metrics.record("a", success=True, elapsed_seconds=0.1)
        await app_metrics.record("b", success=False, elapsed_seconds=0.1)
        snap_a = await app_metrics.get("a")
        snap_b = await app_metrics.get("b")
        assert snap_a.total_requests == 2
        assert snap_a.success_requests == 2
        assert snap_b.total_requests == 1
        assert snap_b.failed_requests == 1

    async def test_concurrent_records_are_consistent(self, app_metrics):
        """Concurrent increments must not lose updates — this is the reason
        counter_increment_by needs to be atomic per-backend, not a naive
        get-then-set."""
        await asyncio.gather(*[
            app_metrics.record("t", success=True, elapsed_seconds=0.1)
            for _ in range(10)
        ])
        snap = await app_metrics.get("t")
        assert snap.total_requests == 10
        assert snap.success_requests == 10


class TestCounterIncrementBy:
    """Direct coverage of the raw Backend.counter_increment_by primitive."""

    async def test_increments_by_amount(self, pool_store):
        backend = pool_store._backend
        assert await backend.counter_increment_by("k", 5) == 5
        assert await backend.counter_increment_by("k", 3) == 8

    async def test_zero_amount_is_noop_returns_current(self, pool_store):
        backend = pool_store._backend
        await backend.counter_increment_by("k", 7)
        assert await backend.counter_increment_by("k", 0) == 7

    async def test_negative_amount_raises(self, pool_store):
        backend = pool_store._backend
        import pytest
        with pytest.raises(ValueError):
            await backend.counter_increment_by("k", -1)

    async def test_starts_from_zero_when_key_absent(self, pool_store):
        backend = pool_store._backend
        assert await backend.counter_get("fresh-key") == 0
        assert await backend.counter_increment_by("fresh-key", 4) == 4
