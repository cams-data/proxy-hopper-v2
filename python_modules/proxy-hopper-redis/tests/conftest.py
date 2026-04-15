"""Fixtures for proxy-hopper-redis package tests."""

from __future__ import annotations

import asyncio

import fakeredis.aioredis as fakeredis
import pytest
import pytest_asyncio

from proxy_hopper.config import ResolvedIP, TargetConfig
from proxy_hopper.pool_store import IPPoolStore
from proxy_hopper_redis.backend import RedisBackend


def _make_resolved(ip_list: list[str]) -> list[ResolvedIP]:
    result = []
    for entry in ip_list:
        host, _, port_str = entry.rpartition(":")
        result.append(ResolvedIP(host=host, port=int(port_str)))
    return result


@pytest.fixture
def target_config() -> TargetConfig:
    return TargetConfig(
        name="test-target",
        regex=r".*example\.com.*",
        resolved_ips=_make_resolved(["1.2.3.4:8080", "5.6.7.8:8080"]),
        min_request_interval=0.0,
        max_queue_wait=2.0,
        num_retries=2,
        ip_failures_until_quarantine=3,
        quarantine_time=0.3,
    )


@pytest_asyncio.fixture
async def redis_backend(target_config):
    """Returns (raw_backend, pool_store) pair backed by fakeredis."""
    fake_server = fakeredis.FakeServer()
    raw = RedisBackend()
    raw._redis = fakeredis.FakeRedis(server=fake_server, decode_responses=True)
    from proxy_hopper_redis.backend import _SORTED_SET_POP_SCRIPT
    raw._sorted_set_pop = raw._redis.register_script(_SORTED_SET_POP_SCRIPT)

    store = IPPoolStore(raw)
    await store.claim_init(target_config.name)
    for ip in target_config.resolved_ips:
        await store.push_ip(target_config.name, ip.address)

    yield raw, store

    await raw._redis.aclose()
