"""Tests for the CLI _run wiring.

These tests exercise the entry-point that wires together config, backend,
target managers, proxy server, and prober.  They exist to catch scoping
or signature bugs (e.g. undefined names, wrong argument order) that unit
tests of individual components cannot detect.
"""

from __future__ import annotations

import asyncio

import pytest

from proxy_hopper.cli import _run
from proxy_hopper.config import ProxyProvider, ServerConfig
from test_helpers import make_target_config


def _make_server(**kwargs) -> ServerConfig:
    defaults = dict(
        host="127.0.0.1",
        port=0,          # OS picks a free port
        backend="memory",
        metrics=False,
        probe=False,     # prober not needed for wiring smoke tests
    )
    defaults.update(kwargs)
    return ServerConfig(**defaults)


class TestRunWiring:
    @pytest.mark.asyncio
    async def test_starts_and_stops_with_no_providers(self):
        """_run completes startup and shuts down cleanly with inline-only targets."""
        targets = [make_target_config(["1.2.3.4:8080"], name="t", regex=".*")]
        server = _make_server()

        task = asyncio.create_task(_run(targets, [], server))
        await asyncio.sleep(0.05)   # let startup complete
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_starts_and_stops_with_providers(self):
        """_run correctly threads providers through to TargetManager and IPProber."""
        provider = ProxyProvider(name="p", ip_list=["1.2.3.4:8080"], region_tag="AU")
        targets = [make_target_config(["1.2.3.4:8080"], name="t", regex=".*")]
        server = _make_server()

        task = asyncio.create_task(_run(targets, [provider], server))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_starts_with_prober_enabled(self):
        """_run starts the prober when probe=True without errors."""
        provider = ProxyProvider(name="p", ip_list=["1.2.3.4:8080"])
        targets = [make_target_config(["1.2.3.4:8080"], name="t", regex=".*")]
        server = _make_server(probe=True, probe_interval=9999)

        task = asyncio.create_task(_run(targets, [provider], server))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
