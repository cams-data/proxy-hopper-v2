"""Backend contract tests.

Every test in this module runs against *each* registered backend type via the
parametrized ``backend`` fixture.  A backend implementation passes its
contract when all tests here are green.

These tests deliberately know nothing about backend internals — they only
call the IPPoolBackend interface methods.
"""

from __future__ import annotations

import asyncio
import time

import pytest


class TestInitTarget:
    async def test_returns_true_on_first_call(self, backend):
        assert await backend.init_target("new-target") is True

    async def test_returns_false_on_second_call(self, backend):
        await backend.init_target("dup-target")
        assert await backend.init_target("dup-target") is False

    async def test_independent_targets(self, backend):
        assert await backend.init_target("target-a") is True
        assert await backend.init_target("target-b") is True


class TestPoolQueue:
    async def test_push_then_pop(self, backend):
        await backend.init_target("t")
        await backend.push_ip("t", "1.2.3.4:8080")
        result = await backend.pop_ip("t", timeout=1.0)
        assert result == "1.2.3.4:8080"

    async def test_pop_timeout_returns_none(self, backend):
        await backend.init_target("t")
        result = await backend.pop_ip("t", timeout=0.1)
        assert result is None

    async def test_fifo_ordering(self, backend):
        await backend.init_target("t")
        await backend.push_ip("t", "1.1.1.1:8080")
        await backend.push_ip("t", "2.2.2.2:8080")
        first = await backend.pop_ip("t", timeout=1.0)
        second = await backend.pop_ip("t", timeout=1.0)
        assert first == "1.1.1.1:8080"
        assert second == "2.2.2.2:8080"

    async def test_pool_size(self, backend):
        await backend.init_target("t")
        assert await backend.pool_size("t") == 0
        await backend.push_ip("t", "1.1.1.1:8080")
        await backend.push_ip("t", "2.2.2.2:8080")
        assert await backend.pool_size("t") == 2

    async def test_pool_size_decrements_after_pop(self, backend):
        await backend.init_target("t")
        await backend.push_ip("t", "1.1.1.1:8080")
        await backend.pop_ip("t", timeout=1.0)
        assert await backend.pool_size("t") == 0


class TestFailureCounter:
    async def test_increment_returns_new_count(self, backend):
        await backend.init_target("t")
        assert await backend.increment_failures("t", "1.2.3.4:8080") == 1
        assert await backend.increment_failures("t", "1.2.3.4:8080") == 2

    async def test_get_failures_zero_initially(self, backend):
        await backend.init_target("t")
        assert await backend.get_failures("t", "1.2.3.4:8080") == 0

    async def test_get_failures_reflects_increments(self, backend):
        await backend.init_target("t")
        await backend.increment_failures("t", "1.2.3.4:8080")
        await backend.increment_failures("t", "1.2.3.4:8080")
        assert await backend.get_failures("t", "1.2.3.4:8080") == 2

    async def test_reset_failures(self, backend):
        await backend.init_target("t")
        await backend.increment_failures("t", "1.2.3.4:8080")
        await backend.increment_failures("t", "1.2.3.4:8080")
        await backend.reset_failures("t", "1.2.3.4:8080")
        assert await backend.get_failures("t", "1.2.3.4:8080") == 0

    async def test_independent_addresses(self, backend):
        await backend.init_target("t")
        await backend.increment_failures("t", "1.1.1.1:8080")
        await backend.increment_failures("t", "1.1.1.1:8080")
        await backend.increment_failures("t", "2.2.2.2:8080")
        assert await backend.get_failures("t", "1.1.1.1:8080") == 2
        assert await backend.get_failures("t", "2.2.2.2:8080") == 1

    async def test_concurrent_increments_are_consistent(self, backend):
        """Concurrent increments must not lose updates."""
        await backend.init_target("t")
        await asyncio.gather(
            backend.increment_failures("t", "1.2.3.4:8080"),
            backend.increment_failures("t", "1.2.3.4:8080"),
            backend.increment_failures("t", "1.2.3.4:8080"),
        )
        assert await backend.get_failures("t", "1.2.3.4:8080") == 3


class TestQuarantine:
    async def test_add_and_list(self, backend):
        await backend.init_target("t")
        await backend.quarantine_add("t", "1.2.3.4:8080", time.time() + 9999)
        assert "1.2.3.4:8080" in await backend.quarantine_list("t")

    def _skip_if_fakeredis(self, backend):
        """Skip quarantine_pop tests for the redis backend when using fakeredis.

        quarantine_pop_expired uses a Lua script (EVALSHA) which fakeredis does
        not support.  These tests run correctly against a real Redis instance —
        set REDIS_URL in the environment to enable them.
        """
        if not getattr(backend, "_is_real_redis", True) and hasattr(backend, "_quarantine_pop"):
            pytest.skip("quarantine_pop_expired requires real Redis (fakeredis lacks EVALSHA)")

    async def test_pop_expired_returns_past_entries(self, backend):
        self._skip_if_fakeredis(backend)
        await backend.init_target("t")
        past = time.time() - 1
        await backend.quarantine_add("t", "1.2.3.4:8080", past)
        expired = await backend.quarantine_pop_expired("t", time.time())
        assert "1.2.3.4:8080" in expired

    async def test_pop_expired_does_not_return_future_entries(self, backend):
        self._skip_if_fakeredis(backend)
        await backend.init_target("t")
        future = time.time() + 9999
        await backend.quarantine_add("t", "1.2.3.4:8080", future)
        expired = await backend.quarantine_pop_expired("t", time.time())
        assert "1.2.3.4:8080" not in expired

    async def test_pop_expired_removes_from_list(self, backend):
        self._skip_if_fakeredis(backend)
        await backend.init_target("t")
        await backend.quarantine_add("t", "1.2.3.4:8080", time.time() - 1)
        await backend.quarantine_pop_expired("t", time.time())
        assert "1.2.3.4:8080" not in await backend.quarantine_list("t")

    async def test_pop_expired_atomic_no_double_claim(self, backend):
        """Each expired entry must be claimed by at most one concurrent caller."""
        self._skip_if_fakeredis(backend)
        await backend.init_target("t")
        await backend.quarantine_add("t", "1.2.3.4:8080", time.time() - 1)

        results = await asyncio.gather(
            backend.quarantine_pop_expired("t", time.time()),
            backend.quarantine_pop_expired("t", time.time()),
        )
        total_claims = results[0].count("1.2.3.4:8080") + results[1].count("1.2.3.4:8080")
        assert total_claims == 1

    async def test_multiple_expired_entries(self, backend):
        self._skip_if_fakeredis(backend)
        await backend.init_target("t")
        past = time.time() - 1
        await backend.quarantine_add("t", "1.1.1.1:8080", past)
        await backend.quarantine_add("t", "2.2.2.2:8080", past)
        await backend.quarantine_add("t", "3.3.3.3:8080", time.time() + 9999)  # not expired

        expired = await backend.quarantine_pop_expired("t", time.time())
        assert set(expired) == {"1.1.1.1:8080", "2.2.2.2:8080"}
