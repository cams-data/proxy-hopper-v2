"""Shared fixtures for generic contract tests.

The ``backend`` and ``pool`` fixtures are parametrized over every registered
backend type.  Adding a new backend implementation requires only adding an
entry to ``_BACKEND_FACTORIES`` — every existing contract test then runs
against it automatically.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import fakeredis.aioredis as fakeredis
import pytest
import pytest_asyncio

from proxy_hopper.backend.base import IPPoolBackend
from proxy_hopper.backend.memory import MemoryIPPoolBackend
from proxy_hopper.config import TargetConfig
from proxy_hopper.pool import IPPool
from proxy_hopper_redis.backend import RedisIPPoolBackend

# ---------------------------------------------------------------------------
# Backend factory registry
# ---------------------------------------------------------------------------
# To register a new backend, add an entry here.  All contract tests will
# run against it automatically.

def _make_memory_backend() -> MemoryIPPoolBackend:
    return MemoryIPPoolBackend()


def _make_redis_backend() -> RedisIPPoolBackend:
    fake_server = fakeredis.FakeServer()
    backend = RedisIPPoolBackend()
    # Inject a fakeredis client so tests run without a real Redis server
    backend._redis = fakeredis.FakeRedis(server=fake_server, decode_responses=True)
    return backend


_BACKEND_FACTORIES = {
    "memory": _make_memory_backend,
    "redis": _make_redis_backend,
}

# ---------------------------------------------------------------------------
# Parametrized fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(params=list(_BACKEND_FACTORIES))
def backend_name(request) -> str:
    return request.param


@pytest_asyncio.fixture
async def backend(backend_name) -> AsyncIterator[IPPoolBackend]:
    """A fully started backend of each registered type."""
    b = _BACKEND_FACTORIES[backend_name]()
    await b.start()
    yield b
    await b.stop()


@pytest.fixture
def target_config() -> TargetConfig:
    return TargetConfig(
        name="contract-target",
        regex=r".*example\.com.*",
        ip_list=["1.2.3.4:8080", "5.6.7.8:8080"],
        min_request_interval=0.0,
        max_queue_wait=2.0,
        num_retries=2,
        ip_failures_until_quarantine=3,
        quarantine_time=0.3,
    )


@pytest_asyncio.fixture
async def pool(backend, target_config) -> AsyncIterator[IPPool]:
    """A started IPPool backed by each registered backend type."""
    p = IPPool(target_config, backend)
    await p.start()
    yield p
    await p.stop()
