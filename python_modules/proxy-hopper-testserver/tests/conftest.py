"""Shared fixtures for proxy-hopper integration tests."""

from __future__ import annotations

import asyncio
import os
from typing import AsyncIterator

import pytest
import pytest_asyncio

from proxy_hopper.backend.base import IPPoolBackend
from proxy_hopper.backend.memory import MemoryIPPoolBackend
from proxy_hopper.config import ResolvedIP, TargetConfig
from proxy_hopper.target_manager import TargetManager
from proxy_hopper_testserver import MockProxyPool, UpstreamServer

_REDIS_URL = os.environ.get("REDIS_URL", "")

# ---------------------------------------------------------------------------
# Backend factory registry — same pattern as python_modules/tests/conftest.py
# ---------------------------------------------------------------------------

def _make_memory_backend() -> tuple[MemoryIPPoolBackend, bool]:
    return MemoryIPPoolBackend(), False


def _make_redis_backend():
    from proxy_hopper_redis.backend import RedisIPPoolBackend
    import fakeredis.aioredis as fakeredis

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
# Markers
# ---------------------------------------------------------------------------

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_redis: mark test as requiring a real Redis instance (skipped with fakeredis)",
    )


def pytest_collection_modifyitems(config, items):
    if _REDIS_URL:
        return
    skip = pytest.mark.skip(reason="requires real Redis — set REDIS_URL to enable")
    for item in items:
        if item.get_closest_marker("real_redis"):
            item.add_marker(skip)

# ---------------------------------------------------------------------------
# Core fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(params=list(_BACKEND_FACTORIES))
def backend_name(request) -> str:
    return request.param


@pytest_asyncio.fixture
async def backend(backend_name) -> AsyncIterator[IPPoolBackend]:
    """A started backend of each registered type."""
    b, _ = _BACKEND_FACTORIES[backend_name]()
    await b.start()
    yield b
    await b.stop()


@pytest_asyncio.fixture
async def upstream() -> AsyncIterator[UpstreamServer]:
    """A fresh upstream test server, reset to normal mode before each test."""
    async with UpstreamServer() as server:
        yield server
        server.reset()


@pytest_asyncio.fixture
async def proxies() -> AsyncIterator[MockProxyPool]:
    """Three mock proxy IPs, all in forward mode."""
    async with MockProxyPool(count=3) as pool:
        yield pool
        pool.reset_all()


def _make_resolved(ip_list: list[str]) -> list[ResolvedIP]:
    result = []
    for entry in ip_list:
        host, _, port_str = entry.rpartition(":")
        result.append(ResolvedIP(host=host, port=int(port_str)))
    return result


def make_target_config(ip_list: list[str], **kw) -> TargetConfig:
    defaults = dict(
        name="integration-test",
        regex=r".*",
        min_request_interval=0.0,
        max_queue_wait=5.0,
        num_retries=2,
        ip_failures_until_quarantine=3,
        quarantine_time=60.0,
    )
    defaults.update(kw)
    return TargetConfig(resolved_ips=_make_resolved(ip_list), **defaults)


@pytest_asyncio.fixture
async def manager(proxies: MockProxyPool, upstream: UpstreamServer, backend: IPPoolBackend) -> AsyncIterator[TargetManager]:
    """A started TargetManager pointing at the mock proxy pool."""
    cfg = make_target_config(ip_list=proxies.ip_list)
    mgr = TargetManager(cfg, backend)
    await mgr.start()
    yield mgr
    await mgr.stop()
