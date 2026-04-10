"""Fixtures for proxy-hopper-redis package tests."""

from __future__ import annotations

import asyncio

import fakeredis.aioredis as fakeredis
import pytest
import pytest_asyncio

from proxy_hopper.config import TargetConfig
from proxy_hopper_redis.backend import RedisIPPoolBackend


@pytest.fixture
def target_config() -> TargetConfig:
    return TargetConfig(
        name="test-target",
        regex=r".*example\.com.*",
        ip_list=["1.2.3.4:8080", "5.6.7.8:8080"],
        min_request_interval=0.0,
        max_queue_wait=2.0,
        num_retries=2,
        ip_failures_until_quarantine=3,
        quarantine_time=0.3,
    )


@pytest_asyncio.fixture
async def redis_backend(target_config) -> RedisIPPoolBackend:
    fake_server = fakeredis.FakeServer()
    backend = RedisIPPoolBackend()
    backend._redis = fakeredis.FakeRedis(server=fake_server, decode_responses=True)
    from proxy_hopper_redis.backend import _QUARANTINE_POP_SCRIPT
    backend._quarantine_pop = backend._redis.register_script(_QUARANTINE_POP_SCRIPT)

    await backend.init_target(target_config.name)
    for host, port in target_config.resolved_ip_list():
        await backend.push_ip(target_config.name, f"{host}:{port}")

    yield backend

    await backend._redis.aclose()
