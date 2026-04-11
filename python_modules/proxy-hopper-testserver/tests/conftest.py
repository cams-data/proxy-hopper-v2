"""Shared fixtures for proxy-hopper integration tests."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
import pytest_asyncio

from proxy_hopper.backend.memory import MemoryIPPoolBackend
from proxy_hopper.config import TargetConfig
from proxy_hopper.pool import IPPool
from proxy_hopper.target_manager import TargetManager
from proxy_hopper_testserver import MockProxyPool, UpstreamServer


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


def make_target_config(ip_list: list[str], **kw) -> TargetConfig:
    defaults = dict(
        name="integration-test",
        regex=r".*",
        ip_list=ip_list,
        min_request_interval=0.0,
        max_queue_wait=5.0,
        num_retries=2,
        ip_failures_until_quarantine=3,
        quarantine_time=60.0,
    )
    defaults.update(kw)
    return TargetConfig(**defaults)


@pytest_asyncio.fixture
async def manager(proxies: MockProxyPool, upstream: UpstreamServer) -> AsyncIterator[TargetManager]:
    """A started TargetManager pointing at the mock proxy pool."""
    cfg = make_target_config(ip_list=proxies.ip_list)
    backend = MemoryIPPoolBackend()
    await backend.start()
    mgr = TargetManager(cfg, backend)
    await mgr.start()
    yield mgr
    await mgr.stop()
    await backend.stop()
