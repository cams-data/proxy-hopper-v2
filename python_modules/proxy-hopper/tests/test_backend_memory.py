"""Memory-backend–specific tests.

These cover implementation details of MemoryIPPoolBackend that are not part
of the generic contract — e.g. internal data structure state.  Generic
contract compliance is verified in python_modules/tests/test_backend_contract.py.
"""

from __future__ import annotations

import asyncio

import pytest

from proxy_hopper.backend.memory import MemoryIPPoolBackend


@pytest.fixture
async def backend():
    b = MemoryIPPoolBackend()
    await b.start()
    await b.init_target("t")
    yield b
    await b.stop()


class TestInternalState:
    async def test_init_target_creates_internal_structures(self):
        b = MemoryIPPoolBackend()
        await b.start()
        await b.init_target("x")
        assert "x" in b._pools
        assert "x" in b._failures
        assert "x" in b._quarantine
        await b.stop()

    async def test_push_increases_queue_size(self, backend):
        await backend.push_ip("t", "1.1.1.1:8080")
        assert backend._pools["t"].qsize() == 1

    async def test_pop_removes_from_queue(self, backend):
        await backend.push_ip("t", "1.1.1.1:8080")
        await backend.pop_ip("t", timeout=1.0)
        assert backend._pools["t"].qsize() == 0

    async def test_failures_stored_in_dict(self, backend):
        await backend.increment_failures("t", "1.1.1.1:8080")
        assert backend._failures["t"]["1.1.1.1:8080"] == 1

    async def test_quarantine_stored_in_dict(self, backend):
        await backend.quarantine_add("t", "1.1.1.1:8080", 9999.0)
        assert backend._quarantine["t"]["1.1.1.1:8080"] == 9999.0

    async def test_quarantine_pop_removes_from_dict(self, backend):
        import time
        await backend.quarantine_add("t", "1.1.1.1:8080", time.time() - 1)
        await backend.quarantine_pop_expired("t", time.time())
        assert "1.1.1.1:8080" not in backend._quarantine["t"]

    async def test_multiple_targets_isolated(self, backend):
        await backend.init_target("t2")
        await backend.push_ip("t", "1.1.1.1:8080")
        assert backend._pools["t2"].qsize() == 0
        assert backend._pools["t"].qsize() == 1
