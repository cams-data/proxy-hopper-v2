"""IPPoolStore contract tests.

Every test in this module runs against *each* registered backend type via the
parametrized ``pool_store`` fixture.  An IPPoolStore+Backend pair passes the
contract when all tests here are green.

These tests deliberately know nothing about backend internals — they only
call the IPPoolStore interface methods.
"""

from __future__ import annotations

import asyncio
import time

import pytest


class TestClaimInit:
    async def test_returns_true_on_first_call(self, pool_store):
        assert await pool_store.claim_init("new-target") is True

    async def test_returns_false_on_second_call(self, pool_store):
        await pool_store.claim_init("dup-target")
        assert await pool_store.claim_init("dup-target") is False

    async def test_independent_targets(self, pool_store):
        assert await pool_store.claim_init("target-a") is True
        assert await pool_store.claim_init("target-b") is True


class TestPoolQueue:
    async def test_push_then_pop(self, pool_store):
        await pool_store.push_ip("t", "1.2.3.4:8080")
        result = await pool_store.pop_ip("t", timeout=1.0)
        assert result == "1.2.3.4:8080"

    async def test_pop_timeout_returns_none(self, pool_store):
        result = await pool_store.pop_ip("t", timeout=0.1)
        assert result is None

    async def test_fifo_ordering(self, pool_store):
        await pool_store.push_ip("t", "1.1.1.1:8080")
        await pool_store.push_ip("t", "2.2.2.2:8080")
        first = await pool_store.pop_ip("t", timeout=1.0)
        second = await pool_store.pop_ip("t", timeout=1.0)
        assert first == "1.1.1.1:8080"
        assert second == "2.2.2.2:8080"

    async def test_pool_size(self, pool_store):
        assert await pool_store.pool_size("t") == 0
        await pool_store.push_ip("t", "1.1.1.1:8080")
        await pool_store.push_ip("t", "2.2.2.2:8080")
        assert await pool_store.pool_size("t") == 2

    async def test_pool_size_decrements_after_pop(self, pool_store):
        await pool_store.push_ip("t", "1.1.1.1:8080")
        await pool_store.pop_ip("t", timeout=1.0)
        assert await pool_store.pool_size("t") == 0


class TestFailureCounter:
    async def test_increment_returns_new_count(self, pool_store):
        assert await pool_store.increment_failures("t", "1.2.3.4:8080") == 1
        assert await pool_store.increment_failures("t", "1.2.3.4:8080") == 2

    async def test_get_failures_zero_initially(self, pool_store):
        assert await pool_store.get_failures("t", "1.2.3.4:8080") == 0

    async def test_get_failures_reflects_increments(self, pool_store):
        await pool_store.increment_failures("t", "1.2.3.4:8080")
        await pool_store.increment_failures("t", "1.2.3.4:8080")
        assert await pool_store.get_failures("t", "1.2.3.4:8080") == 2

    async def test_reset_failures(self, pool_store):
        await pool_store.increment_failures("t", "1.2.3.4:8080")
        await pool_store.increment_failures("t", "1.2.3.4:8080")
        await pool_store.reset_failures("t", "1.2.3.4:8080")
        assert await pool_store.get_failures("t", "1.2.3.4:8080") == 0

    async def test_independent_addresses(self, pool_store):
        await pool_store.increment_failures("t", "1.1.1.1:8080")
        await pool_store.increment_failures("t", "1.1.1.1:8080")
        await pool_store.increment_failures("t", "2.2.2.2:8080")
        assert await pool_store.get_failures("t", "1.1.1.1:8080") == 2
        assert await pool_store.get_failures("t", "2.2.2.2:8080") == 1

    async def test_concurrent_increments_are_consistent(self, pool_store):
        """Concurrent increments must not lose updates."""
        await asyncio.gather(
            pool_store.increment_failures("t", "1.2.3.4:8080"),
            pool_store.increment_failures("t", "1.2.3.4:8080"),
            pool_store.increment_failures("t", "1.2.3.4:8080"),
        )
        assert await pool_store.get_failures("t", "1.2.3.4:8080") == 3


class TestQuarantine:
    async def test_add_and_list(self, pool_store):
        await pool_store.quarantine_add("t", "1.2.3.4:8080", time.time() + 9999)
        assert "1.2.3.4:8080" in await pool_store.quarantine_list("t")

    def _skip_if_fakeredis(self, pool_store):
        """Skip quarantine_pop tests for the redis backend when using fakeredis.

        quarantine_pop_expired uses a Lua script (EVALSHA) which fakeredis does
        not support.  These tests run correctly against a real Redis instance —
        set REDIS_URL in the environment to enable them.
        """
        if not getattr(pool_store, "_is_real_redis", True) and hasattr(pool_store._backend, "_sorted_set_pop"):
            pytest.skip("quarantine_pop_expired requires real Redis (fakeredis lacks EVALSHA)")

    async def test_pop_expired_returns_past_entries(self, pool_store):
        self._skip_if_fakeredis(pool_store)
        past = time.time() - 1
        await pool_store.quarantine_add("t", "1.2.3.4:8080", past)
        expired = await pool_store.quarantine_pop_expired("t", time.time())
        assert "1.2.3.4:8080" in expired

    async def test_pop_expired_does_not_return_future_entries(self, pool_store):
        self._skip_if_fakeredis(pool_store)
        future = time.time() + 9999
        await pool_store.quarantine_add("t", "1.2.3.4:8080", future)
        expired = await pool_store.quarantine_pop_expired("t", time.time())
        assert "1.2.3.4:8080" not in expired

    async def test_pop_expired_removes_from_list(self, pool_store):
        self._skip_if_fakeredis(pool_store)
        await pool_store.quarantine_add("t", "1.2.3.4:8080", time.time() - 1)
        await pool_store.quarantine_pop_expired("t", time.time())
        assert "1.2.3.4:8080" not in await pool_store.quarantine_list("t")

    async def test_pop_expired_atomic_no_double_claim(self, pool_store):
        """Each expired entry must be claimed by at most one concurrent caller."""
        self._skip_if_fakeredis(pool_store)
        await pool_store.quarantine_add("t", "1.2.3.4:8080", time.time() - 1)

        results = await asyncio.gather(
            pool_store.quarantine_pop_expired("t", time.time()),
            pool_store.quarantine_pop_expired("t", time.time()),
        )
        total_claims = results[0].count("1.2.3.4:8080") + results[1].count("1.2.3.4:8080")
        assert total_claims == 1

    async def test_multiple_expired_entries(self, pool_store):
        self._skip_if_fakeredis(pool_store)
        past = time.time() - 1
        await pool_store.quarantine_add("t", "1.1.1.1:8080", past)
        await pool_store.quarantine_add("t", "2.2.2.2:8080", past)
        await pool_store.quarantine_add("t", "3.3.3.3:8080", time.time() + 9999)  # not expired

        expired = await pool_store.quarantine_pop_expired("t", time.time())
        assert set(expired) == {"1.1.1.1:8080", "2.2.2.2:8080"}
