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

from proxy_hopper.identity.identity import Identity
from proxy_hopper.pool import IdentityQueue


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


class TestReconcileOnStart:
    """Covers the Phase 1/2 reconcile-on-start fix -- see CONFIG_RECONCILER_SCOPE.md §6.

    The ``pool`` fixture already exercises the plain "fresh backend, no
    prior state" happy path (every TestAcquire/TestGetStatus test depends on
    it seeding both configured addresses) so it isn't repeated here.
    """

    def _preexisting_identity(self, address: str, uuid: str) -> dict:
        return Identity(address=address, headers={}, cookies_enabled=False).to_dict()

    async def test_leaves_already_registered_address_untouched(self, backend, target_config):
        existing_uuid = "pre-existing-uuid"
        await backend.identity_write(
            target_config.name, existing_uuid, self._preexisting_identity("1.2.3.4:8080", existing_uuid)
        )
        await backend.ip_set(target_config.name, "1.2.3.4:8080", existing_uuid)
        await backend.push_identity_uuid(target_config.name, existing_uuid)

        queue = IdentityQueue(target_config, backend)
        await queue.start()
        try:
            # Untouched: same UUID as before start(), no churn.
            assert await backend.ip_get(target_config.name, "1.2.3.4:8080") == existing_uuid
            # Missing address gets a freshly created identity.
            new_uuid = await backend.ip_get(target_config.name, "5.6.7.8:8080")
            assert new_uuid is not None
            assert new_uuid != existing_uuid
        finally:
            await queue.stop()

    async def test_address_no_longer_in_config_is_retired(self, backend, target_config):
        # An address the backend knows about but that isn't in this config
        # at all (e.g. it was removed from the target's provider/pool).
        await backend.identity_write(
            target_config.name, "stale-uuid", self._preexisting_identity("9.9.9.9:8080", "stale-uuid")
        )
        await backend.ip_set(target_config.name, "9.9.9.9:8080", "stale-uuid")
        await backend.push_identity_uuid(target_config.name, "stale-uuid")

        queue = IdentityQueue(target_config, backend)
        await queue.start()
        try:
            assert await backend.retire_check(target_config.name, "9.9.9.9:8080") is True
            # Configured addresses were still seeded normally.
            assert await backend.ip_get(target_config.name, "1.2.3.4:8080") is not None
            assert await backend.ip_get(target_config.name, "5.6.7.8:8080") is not None
        finally:
            await queue.stop()

    async def test_no_op_when_backend_already_matches_config(self, backend, target_config):
        uuids = {}
        for address in ("1.2.3.4:8080", "5.6.7.8:8080"):
            uuid = f"uuid-{address}"
            await backend.identity_write(target_config.name, uuid, self._preexisting_identity(address, uuid))
            await backend.ip_set(target_config.name, address, uuid)
            await backend.push_identity_uuid(target_config.name, uuid)
            uuids[address] = uuid

        queue = IdentityQueue(target_config, backend)
        await queue.start()
        try:
            for address, uuid in uuids.items():
                assert await backend.ip_get(target_config.name, address) == uuid
        finally:
            await queue.stop()

    async def test_skips_reconcile_when_lock_already_held(self, backend, target_config):
        # Another instance is mid-reconcile -- simulated by pre-holding the
        # lock before this queue's start() ever gets a chance to compete.
        held = await backend.reconcile_lock_acquire(target_config.name, "rival-instance", 30)
        assert held is True

        queue = IdentityQueue(target_config, backend)
        await queue.start()
        try:
            # Lost the race -> trusts the winner, does not seed anything itself.
            assert await backend.list_addresses(target_config.name) == []
        finally:
            await queue.stop()
            await backend.reconcile_lock_release(target_config.name, "rival-instance")

    async def test_reconciles_once_lock_is_free_again(self, backend, target_config):
        held = await backend.reconcile_lock_acquire(target_config.name, "rival-instance", 30)
        assert held is True
        await backend.reconcile_lock_release(target_config.name, "rival-instance")

        queue = IdentityQueue(target_config, backend)
        await queue.start()
        try:
            assert set(await backend.list_addresses(target_config.name)) == {
                "1.2.3.4:8080",
                "5.6.7.8:8080",
            }
        finally:
            await queue.stop()

    async def test_exception_mid_reconcile_propagates_and_releases_lock(
        self, backend, target_config, monkeypatch
    ):
        queue = IdentityQueue(target_config, backend)

        async def _boom(*args, **kwargs):
            raise RuntimeError("simulated backend blip")

        monkeypatch.setattr(queue, "_create_identity", _boom)

        with pytest.raises(RuntimeError, match="simulated backend blip"):
            await queue.start()

        # Lock must not be left held after the failure.
        reacquired = await backend.reconcile_lock_acquire(target_config.name, "someone-else", 30)
        assert reacquired is True
        await backend.reconcile_lock_release(target_config.name, "someone-else")
