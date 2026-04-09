"""Redis-backend–specific tests.

These verify Redis-specific behaviour and implementation details not covered
by the generic contract tests in python_modules/tests/test_backend_contract.py.
Generic contract compliance is verified there (parametrized against both backends).
"""

from __future__ import annotations

import time

import pytest

from proxy_hopper_redis.backend import RedisIPPoolBackend


class TestRedisKeys:
    """Verify the Redis key schema is correct."""

    def test_pool_key(self):
        assert RedisIPPoolBackend._pool_key("google") == "ph:google:pool"

    def test_failures_key(self):
        assert RedisIPPoolBackend._failures_key("g", "1.2.3.4:8080") == "ph:g:failures:1.2.3.4:8080"

    def test_quarantine_key(self):
        assert RedisIPPoolBackend._quarantine_key("g") == "ph:g:quarantine"

    def test_init_key(self):
        assert RedisIPPoolBackend._init_key("g") == "ph:g:initialized"


class TestInitTarget:
    async def test_sets_init_key_in_redis(self, redis_backend, target_config):
        # The fixture already called init_target; verify the key exists
        key = redis_backend._init_key(target_config.name)
        assert await redis_backend._redis.exists(key)

    async def test_second_call_returns_false(self, redis_backend, target_config):
        # init_target was already called in the fixture
        result = await redis_backend.init_target(target_config.name)
        assert result is False

    async def test_init_key_has_ttl(self, redis_backend, target_config):
        key = redis_backend._init_key(target_config.name)
        ttl = await redis_backend._redis.ttl(key)
        assert ttl > 0


class TestRedisPoolQueue:
    async def test_ip_stored_in_redis_list(self, redis_backend, target_config):
        # The fixture seeds 2 IPs
        pool_key = redis_backend._pool_key(target_config.name)
        size = await redis_backend._redis.llen(pool_key)
        assert size == 2

    async def test_pop_uses_blpop(self, redis_backend, target_config):
        address = await redis_backend.pop_ip(target_config.name, timeout=1.0)
        assert address is not None
        pool_key = redis_backend._pool_key(target_config.name)
        assert await redis_backend._redis.llen(pool_key) == 1


class TestRedisFailureCounter:
    async def test_uses_separate_key_per_ip(self, redis_backend, target_config):
        await redis_backend.increment_failures(target_config.name, "1.2.3.4:8080")
        await redis_backend.increment_failures(target_config.name, "5.6.7.8:8080")
        await redis_backend.increment_failures(target_config.name, "5.6.7.8:8080")

        k1 = redis_backend._failures_key(target_config.name, "1.2.3.4:8080")
        k2 = redis_backend._failures_key(target_config.name, "5.6.7.8:8080")
        assert await redis_backend._redis.get(k1) == "1"
        assert await redis_backend._redis.get(k2) == "2"

    async def test_reset_sets_key_to_zero(self, redis_backend, target_config):
        await redis_backend.increment_failures(target_config.name, "1.2.3.4:8080")
        await redis_backend.reset_failures(target_config.name, "1.2.3.4:8080")
        key = redis_backend._failures_key(target_config.name, "1.2.3.4:8080")
        assert await redis_backend._redis.get(key) == "0"

    async def test_get_failures_returns_zero_for_unknown_ip(self, redis_backend, target_config):
        result = await redis_backend.get_failures(target_config.name, "99.99.99.99:8080")
        assert result == 0


class TestRedisQuarantine:
    async def test_stored_as_zset_with_score(self, redis_backend, target_config):
        release_at = time.time() + 100
        await redis_backend.quarantine_add(target_config.name, "1.2.3.4:8080", release_at)
        score = await redis_backend._redis.zscore(
            redis_backend._quarantine_key(target_config.name), "1.2.3.4:8080"
        )
        assert score == pytest.approx(release_at, abs=0.001)

    async def test_zrem_atomicity_prevents_double_claim(self, redis_backend, target_config):
        past = time.time() - 1
        await redis_backend.quarantine_add(target_config.name, "1.2.3.4:8080", past)

        import asyncio
        results = await asyncio.gather(
            redis_backend.quarantine_pop_expired(target_config.name, time.time()),
            redis_backend.quarantine_pop_expired(target_config.name, time.time()),
        )
        claims = results[0].count("1.2.3.4:8080") + results[1].count("1.2.3.4:8080")
        assert claims == 1

    async def test_quarantine_list_uses_zrange(self, redis_backend, target_config):
        await redis_backend.quarantine_add(target_config.name, "1.2.3.4:8080", time.time() + 9999)
        listed = await redis_backend.quarantine_list(target_config.name)
        assert "1.2.3.4:8080" in listed


class TestStartStop:
    async def test_start_verifies_connection(self):
        import fakeredis.aioredis as fakeredis
        backend = RedisIPPoolBackend()
        backend._redis = fakeredis.FakeRedis(decode_responses=True)
        await backend.start()  # should not raise
        await backend.stop()
