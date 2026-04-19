"""Redis-backend–specific tests.

These verify Redis-specific behaviour and implementation details not covered
by the generic contract tests in python_modules/tests/test_backend_contract.py.
Generic contract compliance is verified there (parametrized against both backends).
"""

from __future__ import annotations

import time

import pytest

from proxy_hopper.pool_store import _pool_key, _failures_key, _quarantine_key, _init_key
from proxy_hopper_redis.backend import RedisBackend


class TestRedisKeySchema:
    """Verify the Redis key schema produced by pool_store helpers."""

    def test_pool_key(self):
        assert _pool_key("google") == "ph:google:pool"

    def test_failures_key(self):
        assert _failures_key("g", "1.2.3.4:8080") == "ph:g:failures:1.2.3.4:8080"

    def test_quarantine_key(self):
        assert _quarantine_key("g") == "ph:g:quarantine"

    def test_init_key(self):
        assert _init_key("g") == "ph:g:init"


class TestClaimInit:
    async def test_sets_init_key_in_redis(self, redis_backend, target_config):
        raw, store = redis_backend
        # The fixture already called claim_init; verify the key exists
        key = _init_key(target_config.name)
        assert await raw._redis.exists(key)

    async def test_second_call_returns_false(self, redis_backend, target_config):
        raw, store = redis_backend
        # claim_init was already called in the fixture
        result = await store.claim_init(target_config.name)
        assert result is False

    async def test_init_key_has_ttl(self, redis_backend, target_config):
        raw, store = redis_backend
        key = _init_key(target_config.name)
        ttl = await raw._redis.ttl(key)
        assert ttl > 0


class TestRedisPoolQueue:
    async def test_uuid_stored_in_redis_list(self, redis_backend, target_config):
        raw, store = redis_backend
        # The fixture seeds 2 UUIDs
        pool_k = _pool_key(target_config.name)
        size = await raw._redis.llen(pool_k)
        assert size == 2

    async def test_pop_uses_blpop(self, redis_backend, target_config):
        raw, store = redis_backend
        uuid = await store.pop_identity_uuid(target_config.name, timeout=1.0)
        assert uuid is not None
        pool_k = _pool_key(target_config.name)
        assert await raw._redis.llen(pool_k) == 1


class TestRedisFailureCounter:
    async def test_uses_separate_key_per_ip(self, redis_backend, target_config):
        raw, store = redis_backend
        await store.increment_failures(target_config.name, "1.2.3.4:8080")
        await store.increment_failures(target_config.name, "5.6.7.8:8080")
        await store.increment_failures(target_config.name, "5.6.7.8:8080")

        k1 = _failures_key(target_config.name, "1.2.3.4:8080")
        k2 = _failures_key(target_config.name, "5.6.7.8:8080")
        assert await raw._redis.get(k1) == "1"
        assert await raw._redis.get(k2) == "2"

    async def test_reset_sets_key_to_zero(self, redis_backend, target_config):
        raw, store = redis_backend
        await store.increment_failures(target_config.name, "1.2.3.4:8080")
        await store.reset_failures(target_config.name, "1.2.3.4:8080")
        key = _failures_key(target_config.name, "1.2.3.4:8080")
        assert await raw._redis.get(key) == "0"

    async def test_get_failures_returns_zero_for_unknown_ip(self, redis_backend, target_config):
        raw, store = redis_backend
        result = await store.get_failures(target_config.name, "99.99.99.99:8080")
        assert result == 0


class TestRedisQuarantine:
    async def test_stored_as_zset_with_score(self, redis_backend, target_config):
        raw, store = redis_backend
        release_at = time.time() + 100
        await store.quarantine_add(target_config.name, "1.2.3.4:8080", release_at)
        score = await raw._redis.zscore(
            _quarantine_key(target_config.name), "1.2.3.4:8080"
        )
        assert score == pytest.approx(release_at, abs=0.001)

    @pytest.mark.skip(reason="fakeredis does not support EVALSHA — requires a real Redis instance")
    async def test_quarantine_pop_expired_returns_and_removes_expired(self, redis_backend, target_config):
        raw, store = redis_backend
        past = time.time() - 1
        await store.quarantine_add(target_config.name, "1.2.3.4:8080", past)

        claimed = await store.quarantine_pop_expired(target_config.name, time.time())

        assert "1.2.3.4:8080" in claimed
        # Subsequent call must return empty — entry already removed
        claimed_again = await store.quarantine_pop_expired(target_config.name, time.time())
        assert "1.2.3.4:8080" not in claimed_again

    async def test_quarantine_list_uses_zrange(self, redis_backend, target_config):
        raw, store = redis_backend
        await store.quarantine_add(target_config.name, "1.2.3.4:8080", time.time() + 9999)
        listed = await store.quarantine_list(target_config.name)
        assert "1.2.3.4:8080" in listed


class TestStartStop:
    async def test_start_verifies_connection(self):
        import fakeredis.aioredis as fakeredis
        raw = RedisBackend()
        raw._redis = fakeredis.FakeRedis(decode_responses=True)
        await raw.start()  # should not raise
        await raw.stop()
