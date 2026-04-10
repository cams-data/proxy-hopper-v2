"""IPPool contract tests.

Every test runs against each registered backend via the parametrized
``pool`` and ``backend`` fixtures defined in conftest.py.

These tests exercise the business-logic layer (IPPool) — quarantine
decisions, cooldown scheduling, quarantine sweeping — while remaining
completely agnostic to the underlying storage implementation.
"""

from __future__ import annotations

import asyncio
import time

import pytest


class TestAcquire:
    async def test_returns_address_string(self, pool):
        address = await pool.acquire(timeout=1.0)
        assert address is not None
        assert ":" in address  # "host:port"

    async def test_drains_pool(self, pool):
        a1 = await pool.acquire(timeout=1.0)
        a2 = await pool.acquire(timeout=1.0)
        assert {a1, a2} == {"1.2.3.4:8080", "5.6.7.8:8080"}

    async def test_timeout_when_pool_empty(self, pool):
        await pool.acquire(timeout=1.0)
        await pool.acquire(timeout=1.0)
        result = await pool.acquire(timeout=0.1)
        assert result is None


class TestRecordSuccess:
    async def test_resets_failure_count(self, pool, backend):
        address = await pool.acquire(timeout=1.0)
        # Artificially add failures
        await backend.increment_failures(pool._config.name, address)
        await backend.increment_failures(pool._config.name, address)

        await pool.record_success(address)

        assert await backend.get_failures(pool._config.name, address) == 0

    async def test_returns_ip_to_pool_after_cooldown(self, pool, backend):
        address = await pool.acquire(timeout=1.0)
        size_after_acquire = await backend.pool_size(pool._config.name)

        await pool.record_success(address)
        # min_request_interval=0.0 so IP returns almost immediately
        await asyncio.sleep(0.15)

        assert await backend.pool_size(pool._config.name) == size_after_acquire + 1


class TestRecordFailure:
    async def test_increments_failure_count(self, pool, backend):
        address = await pool.acquire(timeout=1.0)
        await pool.record_failure(address)
        assert await backend.get_failures(pool._config.name, address) == 1

    async def test_below_threshold_returns_ip_to_pool(self, pool, backend):
        # ip_failures_until_quarantine=3, so 1 failure should not quarantine
        address = await pool.acquire(timeout=1.0)
        size_before = await backend.pool_size(pool._config.name)
        await pool.record_failure(address)
        await asyncio.sleep(0.15)
        assert await backend.pool_size(pool._config.name) == size_before + 1

    async def test_at_threshold_quarantines_ip(self, pool, backend):
        address = await pool.acquire(timeout=1.0)
        threshold = pool._config.ip_failures_until_quarantine

        # Pre-load failures up to threshold - 1
        for _ in range(threshold - 1):
            await backend.increment_failures(pool._config.name, address)

        # This failure pushes it to the threshold
        await pool.record_failure(address)
        await asyncio.sleep(0.05)

        quarantined = await backend.quarantine_list(pool._config.name)
        assert address in quarantined

    async def test_at_threshold_ip_not_returned_to_pool(self, pool, backend):
        address = await pool.acquire(timeout=1.0)
        threshold = pool._config.ip_failures_until_quarantine
        for _ in range(threshold - 1):
            await backend.increment_failures(pool._config.name, address)

        size_before = await backend.pool_size(pool._config.name)
        await pool.record_failure(address)
        await asyncio.sleep(0.15)

        # IP should remain out of pool (quarantined)
        assert await backend.pool_size(pool._config.name) == size_before

    async def test_consecutive_failures_accumulate_across_calls(self, pool, backend):
        address = await pool.acquire(timeout=1.0)
        await pool.record_failure(address)
        await asyncio.sleep(0.1)
        # Re-acquire (IP returned after 1 failure, below threshold)
        addr2 = await pool.acquire(timeout=1.0)
        if addr2 == address:
            await pool.record_failure(address)
            assert await backend.get_failures(pool._config.name, address) == 2


class TestQuarantineSweep:
    def _skip_if_fakeredis(self, backend):
        if not getattr(backend, "_is_real_redis", True) and hasattr(backend, "_quarantine_pop"):
            pytest.skip("quarantine sweep requires real Redis (fakeredis lacks EVALSHA)")

    async def test_sweep_releases_expired_ip(self, pool, backend):
        self._skip_if_fakeredis(backend)
        address = await pool.acquire(timeout=1.0)
        # Manually quarantine with a release time in the past
        await backend.quarantine_add(pool._config.name, address, time.time() - 1)

        size_before = await backend.pool_size(pool._config.name)
        await pool._sweep_quarantine()

        assert await backend.pool_size(pool._config.name) == size_before + 1
        assert address not in await backend.quarantine_list(pool._config.name)

    async def test_sweep_leaves_unexpired_ip(self, pool, backend):
        self._skip_if_fakeredis(backend)
        address = await pool.acquire(timeout=1.0)
        await backend.quarantine_add(pool._config.name, address, time.time() + 9999)

        await pool._sweep_quarantine()

        assert address in await backend.quarantine_list(pool._config.name)

    async def test_sweep_is_safe_when_quarantine_empty(self, pool, backend):
        self._skip_if_fakeredis(backend)
        # Should not raise
        await pool._sweep_quarantine()


class TestGetStatus:
    async def test_available_count(self, pool, backend):
        status = await pool.get_status()
        assert status["available_ips"] == 2

    async def test_available_decrements_after_acquire(self, pool):
        await pool.acquire(timeout=1.0)
        status = await pool.get_status()
        assert status["available_ips"] == 1

    async def test_quarantined_listed(self, pool, backend):
        address = await pool.acquire(timeout=1.0)
        await backend.quarantine_add(pool._config.name, address, time.time() + 9999)
        status = await pool.get_status()
        assert address in status["quarantined_ips"]
