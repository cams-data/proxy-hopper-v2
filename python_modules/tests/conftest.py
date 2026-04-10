"""Shared fixtures for generic contract tests.

The ``backend`` and ``pool`` fixtures are parametrized over every registered
backend type.  Adding a new backend implementation requires only adding an
entry to ``_BACKEND_FACTORIES`` — every existing contract test then runs
against it automatically.

Redis backend tests
-------------------
When ``REDIS_URL`` is set in the environment (e.g. in CI via a service
container), the redis backend uses a real Redis instance.  Without it, a
fakeredis stub is used instead.  Tests that require real Redis features not
supported by fakeredis (e.g. Lua scripting) are marked ``real_redis`` and
are automatically skipped when running against fakeredis.
"""

from __future__ import annotations

import os
from typing import AsyncIterator

import fakeredis.aioredis as fakeredis
import pytest
import pytest_asyncio

from proxy_hopper.backend.base import IPPoolBackend
from proxy_hopper.backend.memory import MemoryIPPoolBackend
from proxy_hopper.config import TargetConfig
from proxy_hopper.pool import IPPool
from proxy_hopper_redis.backend import RedisIPPoolBackend

_REDIS_URL = os.environ.get("REDIS_URL", "")

# ---------------------------------------------------------------------------
# Backend factory registry
# ---------------------------------------------------------------------------

def _make_memory_backend() -> tuple[MemoryIPPoolBackend, bool]:
    """Returns (backend, is_real_redis)."""
    return MemoryIPPoolBackend(), False


def _make_redis_backend() -> tuple[RedisIPPoolBackend, bool]:
    """Returns (backend, is_real_redis).

    Uses a real Redis when REDIS_URL is set, otherwise fakeredis.
    """
    backend = RedisIPPoolBackend(_REDIS_URL if _REDIS_URL else "redis://localhost:6379/0")
    if not _REDIS_URL:
        fake_server = fakeredis.FakeServer()
        backend._redis = fakeredis.FakeRedis(server=fake_server, decode_responses=True)
        return backend, False
    return backend, True


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
    b, is_real_redis = _BACKEND_FACTORIES[backend_name]()
    # Stash on the backend so tests can inspect it
    b._is_real_redis = is_real_redis  # type: ignore[attr-defined]
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


# ---------------------------------------------------------------------------
# real_redis marker — auto-skip tests that require real Redis (e.g. Lua)
# ---------------------------------------------------------------------------

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_redis: mark test as requiring a real Redis instance (skipped with fakeredis)",
    )


def pytest_collection_modifyitems(config, items):
    """Skip real_redis tests when REDIS_URL is not set (i.e. using fakeredis)."""
    if _REDIS_URL:
        return
    skip = pytest.mark.skip(reason="requires real Redis — set REDIS_URL to enable")
    for item in items:
        if item.get_closest_marker("real_redis"):
            item.add_marker(skip)
