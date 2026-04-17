"""IdentityQueue contract tests.

Every test runs against each registered backend via the parametrized
``pool`` and ``backend`` fixtures defined in conftest.py.

These tests exercise the business-logic layer (IdentityQueue) — quarantine
decisions, cooldown scheduling, quarantine sweeping — while remaining
completely agnostic to the underlying storage implementation.
"""

from __future__ import annotations

import asyncio
import time

import pytest


class TestAcquire:
    async def test_returns_uuid_and_identity(self, pool):
        result = await pool.acquire(timeout=1.0)
        assert result is not None
        uuid, identity = result
        assert isinstance(uuid, str)
        assert ":" in identity.address  # "host:port"

    async def test_drains_pool(self, pool):
        r1 = await pool.acquire(timeout=1.0)
        r2 = await pool.acquire(timeout=1.0)
        assert r1 is not None and r2 is not None
        addresses = {r1[1].address, r2[1].address}
        assert addresses == {"1.2.3.4:8080", "5.6.7.8:8080"}

    async def test_timeout_when_pool_empty(self, pool):
        await pool.acquire(timeout=1.0)
        await pool.acquire(timeout=1.0)
        result = await pool.acquire(timeout=0.1)
        assert result is None


class TestRecordSuccess:
    async def test_resets_failure_count(self, pool, backend):
        result = await pool.acquire(timeout=1.0)
        assert result is not None
        uuid, identity = result
        # Artificially add failures
        await backend.increment_failures(pool._config.name, identity.address)
        await backend.increment_failures(pool._config.name, identity.address)

        await pool.record_success(uuid, identity, 0.5)

        assert await backend.get_failures(pool._config.name, identity.address) == 0

    async def test_returns_identity_to_pool_after_cooldown(self, pool, backend):
        result = await pool.acquire(timeout=1.0)
        assert result is not None
        uuid, identity = result
        size_after_acquire = await backend.pool_size(pool._config.name)

        await pool.record_success(uuid, identity, 0.5)
        # min_request_interval=0.0 so identity returns almost immediately
        await asyncio.sleep(0.15)

        assert await backend.pool_size(pool._config.name) == size_after_acquire + 1


class TestRecordFailure:
    async def test_increments_failure_count(self, pool, backend):
        result = await pool.acquire(timeout=1.0)
        assert result is not None
        uuid, identity = result
        await pool.record_failure(uuid, identity, 0.5)
        assert await backend.get_failures(pool._config.name, identity.address) == 1

    async def test_below_threshold_returns_identity_to_pool(self, pool, backend):
        # ip_failures_until_quarantine=3, so 1 failure should not quarantine
        result = await pool.acquire(timeout=1.0)
        assert result is not None
        uuid, identity = result
        size_before = await backend.pool_size(pool._config.name)
        await pool.record_failure(uuid, identity, 0.5)
        await asyncio.sleep(0.15)
        assert await backend.pool_size(pool._config.name) == size_before + 1

    async def test_at_threshold_quarantines_ip(self, pool, backend):
        result = await pool.acquire(timeout=1.0)
        assert result is not None
        uuid, identity = result
        threshold = pool._config.ip_failures_until_quarantine

        # Pre-load failures up to threshold - 1
        for _ in range(threshold - 1):
            await backend.increment_failures(pool._config.name, identity.address)

        # This failure pushes it to the threshold
        was_quarantined = await pool.record_failure(uuid, identity, 0.5)
        await asyncio.sleep(0.05)

        quarantined = await backend.quarantine_list(pool._config.name)
        assert identity.address in quarantined
        assert was_quarantined is True

    async def test_at_threshold_identity_not_returned_to_pool(self, pool, backend):
        result = await pool.acquire(timeout=1.0)
        assert result is not None
        uuid, identity = result
        threshold = pool._config.ip_failures_until_quarantine
        for _ in range(threshold - 1):
            await backend.increment_failures(pool._config.name, identity.address)

        size_before = await backend.pool_size(pool._config.name)
        await pool.record_failure(uuid, identity, 0.5)
        await asyncio.sleep(0.15)

        # Identity should remain out of pool (quarantined)
        assert await backend.pool_size(pool._config.name) == size_before

    async def test_consecutive_failures_accumulate_across_calls(self, pool, backend):
        r1 = await pool.acquire(timeout=1.0)
        assert r1 is not None
        uuid1, identity1 = r1
        await pool.record_failure(uuid1, identity1, 0.5)
        await asyncio.sleep(0.1)
        # Re-acquire (identity returned after 1 failure, below threshold)
        r2 = await pool.acquire(timeout=1.0)
        if r2 is not None and r2[1].address == identity1.address:
            uuid2, identity2 = r2
            await pool.record_failure(uuid2, identity2, 0.5)
            assert await backend.get_failures(pool._config.name, identity2.address) == 2


class TestQuarantineSweep:
    def _skip_if_fakeredis(self, backend):
        if not getattr(backend, "_is_real_redis", True) and hasattr(getattr(backend, "_backend", None), "_sorted_set_pop"):
            pytest.skip("quarantine sweep requires real Redis (fakeredis lacks EVALSHA)")

    async def test_sweep_releases_expired_ip(self, pool, backend):
        self._skip_if_fakeredis(backend)
        result = await pool.acquire(timeout=1.0)
        assert result is not None
        uuid, identity = result
        # Manually quarantine with a release time in the past
        await backend.quarantine_add(pool._config.name, identity.address, time.time() - 1)

        size_before = await backend.pool_size(pool._config.name)
        await pool._sweep_quarantine()

        assert await backend.pool_size(pool._config.name) == size_before + 1
        assert identity.address not in await backend.quarantine_list(pool._config.name)

    async def test_sweep_leaves_unexpired_ip(self, pool, backend):
        self._skip_if_fakeredis(backend)
        result = await pool.acquire(timeout=1.0)
        assert result is not None
        uuid, identity = result
        await backend.quarantine_add(pool._config.name, identity.address, time.time() + 9999)

        await pool._sweep_quarantine()

        assert identity.address in await backend.quarantine_list(pool._config.name)

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
        result = await pool.acquire(timeout=1.0)
        assert result is not None
        uuid, identity = result
        await backend.quarantine_add(pool._config.name, identity.address, time.time() + 9999)
        status = await pool.get_status()
        assert identity.address in status["quarantined_ips"]
