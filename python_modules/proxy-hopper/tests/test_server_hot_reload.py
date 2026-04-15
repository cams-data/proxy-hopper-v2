"""Tests for ProxyServer hot-reload via DynamicConfigStore change events."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from proxy_hopper.backend.memory import MemoryBackend
from proxy_hopper.dynamic_config import ConfigChangeEvent, DynamicConfigStore, _build_target
from proxy_hopper.pool_store import IPPoolStore
from proxy_hopper.server import ProxyServer
from proxy_hopper.target_manager import TargetManager

from test_helpers import make_target_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(name="api", ip_list=None):
    return _build_target(
        name=name,
        regex=r".*",
        ip_list=ip_list or ["1.2.3.4:3128"],
    )


async def _make_server_with_dynamic(initial_configs=None):
    """Create a ProxyServer with MemoryBackend + DynamicConfigStore but no TCP listener."""
    raw_backend = MemoryBackend()
    await raw_backend.start()
    pool_store = IPPoolStore(raw_backend)
    dynamic_config = DynamicConfigStore(raw_backend)

    configs = initial_configs or [_make_config("existing")]
    managers = [TargetManager(cfg, pool_store) for cfg in configs]

    server = ProxyServer(
        managers,
        host="127.0.0.1",
        port=0,
        pool_store=pool_store,
        dynamic_config=dynamic_config,
    )
    return server, pool_store, dynamic_config, raw_backend


# ---------------------------------------------------------------------------
# _apply_change — add
# ---------------------------------------------------------------------------

class TestApplyChangeAdd:
    async def test_add_appends_new_manager(self):
        server, pool_store, dynamic_config, raw = await _make_server_with_dynamic([])
        cfg = _make_config("new-target")
        await dynamic_config.add_target(cfg)

        event = ConfigChangeEvent(type="add", name="new-target")
        await server._apply_change(event)

        assert any(m._config.name == "new-target" for m in server._managers)
        for m in server._managers:
            await m.stop()
        await raw.stop()

    async def test_add_starts_manager(self):
        server, pool_store, dynamic_config, raw = await _make_server_with_dynamic([])
        cfg = _make_config("started")
        await dynamic_config.add_target(cfg)

        event = ConfigChangeEvent(type="add", name="started")
        await server._apply_change(event)

        mgr = next(m for m in server._managers if m._config.name == "started")
        assert mgr._running is True
        await mgr.stop()
        await raw.stop()

    async def test_add_missing_from_store_is_noop(self):
        server, pool_store, dynamic_config, raw = await _make_server_with_dynamic([])
        initial_count = len(server._managers)

        event = ConfigChangeEvent(type="add", name="ghost")
        await server._apply_change(event)  # ghost not in store

        assert len(server._managers) == initial_count
        await raw.stop()


# ---------------------------------------------------------------------------
# _apply_change — update
# ---------------------------------------------------------------------------

class TestApplyChangeUpdate:
    async def test_update_replaces_existing_manager(self):
        initial = _make_config("updatable")
        server, pool_store, dynamic_config, raw = await _make_server_with_dynamic([initial])
        for m in server._managers:
            await m.start()

        updated = _build_target("updatable", r".*", ["9.9.9.9:3128"], min_request_interval=5.0)
        await dynamic_config.add_target(updated)

        event = ConfigChangeEvent(type="update", name="updatable")
        await server._apply_change(event)

        mgr = next(m for m in server._managers if m._config.name == "updatable")
        assert mgr._config.min_request_interval == 5.0
        for m in server._managers:
            await m.stop()
        await raw.stop()

    async def test_update_stops_old_manager(self):
        initial = _make_config("stopme")
        server, pool_store, dynamic_config, raw = await _make_server_with_dynamic([initial])
        old_mgr = server._managers[0]
        await old_mgr.start()

        updated = _build_target("stopme", r".*", ["2.2.2.2:3128"])
        await dynamic_config.add_target(updated)

        event = ConfigChangeEvent(type="update", name="stopme")
        await server._apply_change(event)

        assert old_mgr._running is False
        for m in server._managers:
            await m.stop()
        await raw.stop()

    async def test_update_preserves_manager_list_reference(self):
        """In-place mutation must keep the same list object."""
        initial = _make_config("ref-test")
        server, pool_store, dynamic_config, raw = await _make_server_with_dynamic([initial])
        await server._managers[0].start()
        original_list = server._managers  # capture reference

        updated = _build_target("ref-test", r".*", ["3.3.3.3:3128"])
        await dynamic_config.add_target(updated)

        event = ConfigChangeEvent(type="update", name="ref-test")
        await server._apply_change(event)

        assert server._managers is original_list  # same object
        for m in server._managers:
            await m.stop()
        await raw.stop()

    async def test_update_missing_from_store_is_noop(self):
        initial = _make_config("present")
        server, pool_store, dynamic_config, raw = await _make_server_with_dynamic([initial])
        initial_count = len(server._managers)

        event = ConfigChangeEvent(type="update", name="ghost")
        await server._apply_change(event)

        assert len(server._managers) == initial_count
        await raw.stop()


# ---------------------------------------------------------------------------
# _apply_change — remove
# ---------------------------------------------------------------------------

class TestApplyChangeRemove:
    async def test_remove_drops_manager_from_list(self):
        configs = [_make_config("keep"), _make_config("drop")]
        server, pool_store, dynamic_config, raw = await _make_server_with_dynamic(configs)
        for m in server._managers:
            await m.start()

        event = ConfigChangeEvent(type="remove", name="drop")
        await server._apply_change(event)

        names = [m._config.name for m in server._managers]
        assert "drop" not in names
        assert "keep" in names
        for m in server._managers:
            await m.stop()
        await raw.stop()

    async def test_remove_stops_removed_manager(self):
        server, pool_store, dynamic_config, raw = await _make_server_with_dynamic([_make_config("gone")])
        gone_mgr = server._managers[0]
        await gone_mgr.start()

        event = ConfigChangeEvent(type="remove", name="gone")
        await server._apply_change(event)

        assert gone_mgr._running is False
        await raw.stop()

    async def test_remove_nonexistent_is_noop(self):
        server, pool_store, dynamic_config, raw = await _make_server_with_dynamic([_make_config("x")])
        await server._managers[0].start()
        initial_count = len(server._managers)

        event = ConfigChangeEvent(type="remove", name="nobody")
        await server._apply_change(event)

        assert len(server._managers) == initial_count
        await server._managers[0].stop()
        await raw.stop()

    async def test_remove_preserves_list_reference(self):
        configs = [_make_config("a"), _make_config("b")]
        server, pool_store, dynamic_config, raw = await _make_server_with_dynamic(configs)
        for m in server._managers:
            await m.start()
        original_list = server._managers

        event = ConfigChangeEvent(type="remove", name="b")
        await server._apply_change(event)

        assert server._managers is original_list
        for m in server._managers:
            await m.stop()
        await raw.stop()


# ---------------------------------------------------------------------------
# ProxyServer lifecycle with dynamic_config
# ---------------------------------------------------------------------------

class TestServerLifecycleWithDynamicConfig:
    async def test_start_launches_change_listener_task(self):
        raw_backend = MemoryBackend()
        await raw_backend.start()
        pool_store = IPPoolStore(raw_backend)
        dynamic_config = DynamicConfigStore(raw_backend)
        server = ProxyServer(
            [],
            host="127.0.0.1",
            port=0,
            pool_store=pool_store,
            dynamic_config=dynamic_config,
        )
        await server.start()
        assert server._change_listener_task is not None
        assert not server._change_listener_task.done()
        await server.stop()
        await raw_backend.stop()

    async def test_stop_cancels_change_listener_task(self):
        raw_backend = MemoryBackend()
        await raw_backend.start()
        pool_store = IPPoolStore(raw_backend)
        dynamic_config = DynamicConfigStore(raw_backend)
        server = ProxyServer(
            [],
            host="127.0.0.1",
            port=0,
            pool_store=pool_store,
            dynamic_config=dynamic_config,
        )
        await server.start()
        task = server._change_listener_task
        await server.stop()
        assert task.done()
        await raw_backend.stop()

    async def test_no_dynamic_config_no_listener_task(self):
        raw_backend = MemoryBackend()
        await raw_backend.start()
        pool_store = IPPoolStore(raw_backend)
        server = ProxyServer([], host="127.0.0.1", port=0)
        await server.start()
        assert server._change_listener_task is None
        await server.stop()
        await raw_backend.stop()

    async def test_add_via_store_reaches_manager_list(self):
        """End-to-end: add_target on store → event → manager appended."""
        raw_backend = MemoryBackend()
        await raw_backend.start()
        pool_store = IPPoolStore(raw_backend)
        dynamic_config = DynamicConfigStore(raw_backend)
        server = ProxyServer(
            [],
            host="127.0.0.1",
            port=0,
            pool_store=pool_store,
            dynamic_config=dynamic_config,
        )
        await server.start()
        # Yield to event loop so the listener task enters subscribe_changes() before we publish
        await asyncio.sleep(0.05)

        cfg = _build_target("live-add", r".*", ["1.1.1.1:3128"])
        await dynamic_config.add_target(cfg)

        # Give the background listener a moment to process
        for _ in range(20):
            await asyncio.sleep(0.01)
            if any(m._config.name == "live-add" for m in server._managers):
                break

        assert any(m._config.name == "live-add" for m in server._managers)
        await server.stop()
        await raw_backend.stop()
