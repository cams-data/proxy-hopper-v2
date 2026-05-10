"""Fixtures for proxy-hopper-webserver tests."""

from __future__ import annotations

import pytest
import pytest_asyncio

from proxy_hopper.backend.memory import MemoryBackend
from proxy_hopper.repository import ProxyRepository


@pytest_asyncio.fixture
async def backend():
    b = MemoryBackend()
    await b.start()
    yield b
    await b.stop()


@pytest_asyncio.fixture
async def repo(backend):
    return ProxyRepository(backend)
