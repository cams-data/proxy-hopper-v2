"""Tests for the CLI _run wiring.

These tests exercise the entry-point that wires together config, backend,
target managers, proxy server, and prober.  They exist to catch scoping
or signature bugs (e.g. undefined names, wrong argument order) that unit
tests of individual components cannot detect.
"""

from __future__ import annotations

import asyncio
import sys

import aiohttp
import pytest

from proxy_hopper.cli import _run
from proxy_hopper.config import ProxyProvider, ServerConfig
from test_helpers import make_target_config

# Warm up the (optional, dev-only) admin-server import chain at collection
# time. `proxy_hopper_webserver.app` and `proxy_hopper_webserver.graphql`
# (the latter only pulled in lazily, inside create_admin_app's body, so
# importing .app alone doesn't warm it) each drag in FastAPI/strawberry-
# graphql/graphql-core for the first time, which can take upwards of ten
# seconds each on a slow filesystem — pure import cost, not compute. Pay
# that here so individual tests' readiness-polling timeouts measure actual
# startup latency instead of one-time import cost.
try:
    import proxy_hopper_webserver.app  # noqa: F401
    import proxy_hopper_webserver.graphql  # noqa: F401
except ImportError:
    pass


async def _wait_for_port(host: str, port: int, timeout: float = 30.0) -> None:
    """Poll until a TCP connection succeeds, or raise on timeout.

    30s default is generous on purpose: even after warming up the admin
    server's import chain at collection time (see the module-level warm-up
    above), cold-import time for FastAPI/strawberry-graphql/graphql-core has
    been observed to vary wildly (roughly 2s-25s) on this project's dev
    sandbox (WSL2 with a cross-filesystem /mnt/d mount) depending on disk
    cache/IO contention at the moment pytest collects this module. Actual
    server startup once imports are warm is under 2s. A real failure (the
    server never starting at all) will still time out here, just slower
    than it would on a normal filesystem — this isn't hiding a bug, it's
    absorbing this one environment's IO variance.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    last_exc: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError as exc:
            last_exc = exc
            await asyncio.sleep(0.02)
    raise TimeoutError(f"{host}:{port} never became reachable") from last_exc


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


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestEmbeddedAdmin:
    """Tests for `_run`'s optional embedded admin server (server.admin=True).

    This is the mode that lets the admin API see *live* state with the
    memory backend: a separately-run `proxy-hopper admin` process gets its
    own private MemoryBackend instance that the proxy process can never
    write to, so these two must share one `repo`/`event_bus` in one process
    to have any live data at all.
    """

    @pytest.mark.asyncio
    async def test_embedded_admin_health_endpoint_reachable(self):
        admin_port = _free_port()
        targets = [make_target_config(["1.2.3.4:8080"], name="t", regex=".*")]
        server = _make_server(admin=True, admin_host="127.0.0.1", admin_port=admin_port)

        task = asyncio.create_task(_run(targets, [], server))
        try:
            await _wait_for_port("127.0.0.1", admin_port)
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{admin_port}/health") as resp:
                    assert resp.status == 200
                    assert await resp.json() == {"status": "ok"}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_embedded_admin_shares_live_repo(self):
        """The admin GraphQL API must see the same seeded target the proxy
        is routing with — not a disconnected, independently-seeded copy."""
        admin_port = _free_port()
        targets = [make_target_config(["1.2.3.4:8080"], name="shared-target", regex=".*")]
        server = _make_server(admin=True, admin_host="127.0.0.1", admin_port=admin_port)

        task = asyncio.create_task(_run(targets, [], server))
        try:
            await _wait_for_port("127.0.0.1", admin_port)
            query = {"query": "{ targets { name } }"}
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{admin_port}/graphql", json=query
                ) as resp:
                    assert resp.status == 200
                    data = await resp.json()
            names = [t["name"] for t in data["data"]["targets"]]
            assert names == ["shared-target"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_admin_not_started_when_disabled(self):
        """Default (admin=False) must not open the admin port at all."""
        admin_port = _free_port()
        targets = [make_target_config(["1.2.3.4:8080"], name="t", regex=".*")]
        server = _make_server(admin=False, admin_host="127.0.0.1", admin_port=admin_port)

        task = asyncio.create_task(_run(targets, [], server))
        try:
            await asyncio.sleep(0.1)
            with pytest.raises((TimeoutError, OSError)):
                await _wait_for_port("127.0.0.1", admin_port, timeout=0.2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_missing_webserver_dependency_aborts_cleanly(self, monkeypatch):
        """If proxy-hopper-webserver isn't installed, admin=True must abort
        _run() with a clear error rather than hang or crash uglily — the
        same fail-fast pattern already used for backend=redis without
        proxy-hopper-redis installed."""
        monkeypatch.setitem(sys.modules, "proxy_hopper_webserver", None)
        targets = [make_target_config(["1.2.3.4:8080"], name="t", regex=".*")]
        server = _make_server(admin=True, admin_port=_free_port())

        # Must return promptly on its own — no cancellation needed — since
        # the ImportError path returns before starting the proxy.
        await asyncio.wait_for(_run(targets, [], server), timeout=2.0)
