"""Tests for DynamicConfigStore — CRUD, IP helpers, pub/sub, serialisation."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from proxy_hopper.backend.memory import MemoryBackend
from proxy_hopper.config import ResolvedIP, TargetConfig
from proxy_hopper.dynamic_config import (
    ConfigChangeEvent,
    DynamicConfigStore,
    _KV_PREFIX,
    _build_target,
    _dict_to_target,
    _target_to_dict,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def backend():
    b = MemoryBackend()
    await b.start()
    yield b
    await b.stop()


@pytest_asyncio.fixture
async def store(backend):
    return DynamicConfigStore(backend)


def _make_config(name="api", ip_list=None, **kw) -> TargetConfig:
    ips = [
        ResolvedIP(host="1.2.3.4", port=3128),
        *([] if ip_list is None else [ResolvedIP(host=h, port=p) for h, p in
            [entry.rsplit(":", 1) for entry in ip_list] if True
        ]),
    ]
    # simpler: use _build_target helper
    return _build_target(
        name=name,
        regex=r".*",
        ip_list=ip_list or ["1.2.3.4:3128"],
        **kw,
    )


# ---------------------------------------------------------------------------
# Serialisation round-trip
# ---------------------------------------------------------------------------

class TestSerialisation:
    def test_round_trip_basic(self):
        cfg = _build_target("t", r".*", ["1.2.3.4:3128"])
        restored = _dict_to_target(_target_to_dict(cfg))
        assert restored.name == cfg.name
        assert restored.regex == cfg.regex
        assert restored.resolved_ips == cfg.resolved_ips
        assert restored.min_request_interval == cfg.min_request_interval

    def test_round_trip_preserves_all_fields(self):
        cfg = _build_target(
            "complex", r"api\.example\.com",
            ["10.0.0.1:3128", "10.0.0.2:3128"],
            min_request_interval=5.0,
            max_queue_wait=60.0,
            num_retries=1,
            ip_failures_until_quarantine=2,
            quarantine_time=300.0,
            default_proxy_port=3128,
            mutable=True,
        )
        restored = _dict_to_target(_target_to_dict(cfg))
        assert restored.min_request_interval == 5.0
        assert restored.max_queue_wait == 60.0
        assert restored.num_retries == 1
        assert restored.ip_failures_until_quarantine == 2
        assert restored.quarantine_time == 300.0
        assert restored.mutable is True
        assert len(restored.resolved_ips) == 2

    def test_round_trip_with_identity(self):
        from proxy_hopper.config import IdentityConfig, WarmupConfig
        cfg = _build_target(
            "id-test", r".*",
            ["1.2.3.4:3128"],
            identity=IdentityConfig(
                enabled=True,
                cookies=True,
                rotate_after_requests=50,
                rotate_on_429=True,
                warmup=WarmupConfig(enabled=True, path="/warmup"),
            ),
        )
        restored = _dict_to_target(_target_to_dict(cfg))
        assert restored.identity.enabled is True
        assert restored.identity.rotate_after_requests == 50
        assert restored.identity.warmup is not None
        assert restored.identity.warmup.path == "/warmup"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

class TestAddTarget:
    async def test_add_persists_to_kv(self, store, backend):
        cfg = _make_config("new-target")
        await store.add_target(cfg)
        raw = await backend.kv_get(f"{_KV_PREFIX}new-target")
        assert raw is not None

    async def test_add_then_get_round_trips(self, store):
        cfg = _make_config("roundtrip")
        await store.add_target(cfg)
        got = await store.get_target("roundtrip")
        assert got is not None
        assert got.name == "roundtrip"
        assert got.resolved_ips == cfg.resolved_ips

    async def test_add_duplicate_raises(self, store):
        cfg = _make_config("dup")
        await store.add_target(cfg)
        with pytest.raises(ValueError, match="already exists"):
            await store.add_target(cfg)

    async def test_add_publishes_event(self, store, backend):
        events = []

        async def collect():
            async with store.subscribe_changes() as evts:
                async for e in evts:
                    events.append(e)
                    return

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        await store.add_target(_make_config("pub-add"))
        await asyncio.wait_for(task, timeout=1.0)

        assert len(events) == 1
        assert events[0].type == "add"
        assert events[0].name == "pub-add"
        assert events[0].data is not None


class TestUpdateTarget:
    async def test_update_overwrites_stored_value(self, store):
        cfg = _make_config("upd", min_request_interval=1.0)
        await store.add_target(cfg)
        updated = cfg.model_copy(update={"min_request_interval": 10.0})
        await store.update_target(updated)
        got = await store.get_target("upd")
        assert got.min_request_interval == 10.0

    async def test_update_nonexistent_raises(self, store):
        cfg = _make_config("ghost")
        with pytest.raises(ValueError, match="does not exist"):
            await store.update_target(cfg)

    async def test_update_immutable_target_raises(self, store):
        cfg = _make_config("frozen", mutable=False)
        await store.add_target(cfg)
        updated = cfg.model_copy(update={"min_request_interval": 99.0})
        with pytest.raises(ValueError, match="not mutable"):
            await store.update_target(updated)

    async def test_update_publishes_event(self, store, backend):
        await store.add_target(_make_config("pub-upd"))
        events = []

        async def collect():
            async with store.subscribe_changes() as evts:
                async for e in evts:
                    events.append(e)
                    return

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        updated = _make_config("pub-upd", min_request_interval=99.0)
        await store.update_target(updated)
        await asyncio.wait_for(task, timeout=1.0)

        assert events[0].type == "update"
        assert events[0].name == "pub-upd"


class TestRemoveTarget:
    async def test_remove_deletes_from_kv(self, store, backend):
        await store.add_target(_make_config("to-remove"))
        await store.remove_target("to-remove")
        assert await backend.kv_get(f"{_KV_PREFIX}to-remove") is None

    async def test_remove_nonexistent_is_noop(self, store):
        await store.remove_target("does-not-exist")  # must not raise

    async def test_remove_publishes_event(self, store):
        await store.add_target(_make_config("pub-rm"))
        events = []

        async def collect():
            async with store.subscribe_changes() as evts:
                async for e in evts:
                    events.append(e)
                    return

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        await store.remove_target("pub-rm")
        await asyncio.wait_for(task, timeout=1.0)

        assert events[0].type == "remove"
        assert events[0].name == "pub-rm"
        assert events[0].data is None

    async def test_get_after_remove_returns_none(self, store):
        await store.add_target(_make_config("del-me"))
        await store.remove_target("del-me")
        assert await store.get_target("del-me") is None


class TestGetTarget:
    async def test_get_missing_returns_none(self, store):
        assert await store.get_target("nope") is None

    async def test_get_returns_correct_target(self, store):
        await store.add_target(_make_config("find-me"))
        got = await store.get_target("find-me")
        assert got is not None
        assert got.name == "find-me"


class TestListTargets:
    async def test_empty_store_returns_empty(self, store):
        assert await store.list_targets() == []

    async def test_lists_all_added_targets(self, store):
        await store.add_target(_make_config("a"))
        await store.add_target(_make_config("b"))
        targets = await store.list_targets()
        assert {t.name for t in targets} == {"a", "b"}

    async def test_removed_targets_not_listed(self, store):
        await store.add_target(_make_config("keep"))
        await store.add_target(_make_config("drop"))
        await store.remove_target("drop")
        targets = await store.list_targets()
        assert [t.name for t in targets] == ["keep"]

    async def test_only_lists_own_prefix(self, store, backend):
        # Write a KV entry with a different prefix — must not appear in list
        await backend.kv_set("ph:other:target:noise", '{"name": "noise"}')
        await store.add_target(_make_config("real"))
        targets = await store.list_targets()
        assert len(targets) == 1
        assert targets[0].name == "real"


# ---------------------------------------------------------------------------
# IP helpers
# ---------------------------------------------------------------------------

class TestAddIp:
    async def test_add_ip_appends_to_list(self, store):
        await store.add_target(_make_config("t", ["1.1.1.1:3128"]))
        await store.add_ip("t", "2.2.2.2:3128")
        got = await store.get_target("t")
        assert len(got.resolved_ips) == 2
        assert any(ip.address == "2.2.2.2:3128" for ip in got.resolved_ips)

    async def test_add_ip_missing_target_raises(self, store):
        with pytest.raises(ValueError, match="not found"):
            await store.add_ip("ghost", "1.1.1.1:3128")

    async def test_add_ip_uses_default_port(self, store):
        await store.add_target(_make_config("t", default_proxy_port=9999))
        await store.add_ip("t", "5.5.5.5")  # no port
        got = await store.get_target("t")
        assert any(ip.port == 9999 for ip in got.resolved_ips)


class TestRemoveIp:
    async def test_remove_ip_removes_from_list(self, store):
        await store.add_target(_make_config("t", ["1.1.1.1:3128", "2.2.2.2:3128"]))
        await store.remove_ip("t", "1.1.1.1:3128")
        got = await store.get_target("t")
        assert len(got.resolved_ips) == 1
        assert got.resolved_ips[0].address == "2.2.2.2:3128"

    async def test_remove_last_ip_raises(self, store):
        await store.add_target(_make_config("t", ["1.1.1.1:3128"]))
        with pytest.raises(ValueError, match="at least one IP"):
            await store.remove_ip("t", "1.1.1.1:3128")

    async def test_remove_ip_missing_target_raises(self, store):
        with pytest.raises(ValueError, match="not found"):
            await store.remove_ip("ghost", "1.1.1.1:3128")


class TestSwapIp:
    async def test_swap_replaces_address(self, store):
        await store.add_target(_make_config("t", ["1.1.1.1:3128", "2.2.2.2:3128"]))
        await store.swap_ip("t", "1.1.1.1:3128", "3.3.3.3:3128")
        got = await store.get_target("t")
        addresses = [ip.address for ip in got.resolved_ips]
        assert "3.3.3.3:3128" in addresses
        assert "1.1.1.1:3128" not in addresses
        assert "2.2.2.2:3128" in addresses  # untouched

    async def test_swap_old_not_found_raises(self, store):
        await store.add_target(_make_config("t", ["1.1.1.1:3128"]))
        with pytest.raises(ValueError, match="not found"):
            await store.swap_ip("t", "9.9.9.9:3128", "2.2.2.2:3128")

    async def test_swap_missing_target_raises(self, store):
        with pytest.raises(ValueError, match="not found"):
            await store.swap_ip("ghost", "1.1.1.1:3128", "2.2.2.2:3128")

    async def test_swap_preserves_list_length(self, store):
        await store.add_target(_make_config("t", ["1.1.1.1:3128", "2.2.2.2:3128"]))
        await store.swap_ip("t", "1.1.1.1:3128", "5.5.5.5:3128")
        got = await store.get_target("t")
        assert len(got.resolved_ips) == 2


# ---------------------------------------------------------------------------
# subscribe_changes
# ---------------------------------------------------------------------------

class TestSubscribeChanges:
    async def test_receives_add_event(self, store):
        received = []

        async def listen():
            async with store.subscribe_changes() as events:
                async for e in events:
                    received.append(e)
                    return

        task = asyncio.create_task(listen())
        await asyncio.sleep(0)
        await store.add_target(_make_config("sub-test"))
        await asyncio.wait_for(task, timeout=1.0)

        assert len(received) == 1
        evt = received[0]
        assert isinstance(evt, ConfigChangeEvent)
        assert evt.type == "add"
        assert evt.name == "sub-test"

    async def test_receives_update_event(self, store):
        await store.add_target(_make_config("upd-sub"))
        received = []

        async def listen():
            async with store.subscribe_changes() as events:
                async for e in events:
                    received.append(e)
                    return

        task = asyncio.create_task(listen())
        await asyncio.sleep(0)
        await store.update_target(_make_config("upd-sub", min_request_interval=5.0))
        await asyncio.wait_for(task, timeout=1.0)

        assert received[0].type == "update"

    async def test_receives_remove_event(self, store):
        await store.add_target(_make_config("rm-sub"))
        received = []

        async def listen():
            async with store.subscribe_changes() as events:
                async for e in events:
                    received.append(e)
                    return

        task = asyncio.create_task(listen())
        await asyncio.sleep(0)
        await store.remove_target("rm-sub")
        await asyncio.wait_for(task, timeout=1.0)

        assert received[0].type == "remove"
        assert received[0].data is None

    async def test_multiple_subscribers_all_receive(self, store):
        received_a: list[ConfigChangeEvent] = []
        received_b: list[ConfigChangeEvent] = []

        async def listen(out):
            async with store.subscribe_changes() as events:
                async for e in events:
                    out.append(e)
                    return

        t1 = asyncio.create_task(listen(received_a))
        t2 = asyncio.create_task(listen(received_b))
        await asyncio.sleep(0)
        await store.add_target(_make_config("broadcast"))
        await asyncio.wait_for(asyncio.gather(t1, t2), timeout=1.0)

        assert received_a[0].name == "broadcast"
        assert received_b[0].name == "broadcast"

    async def test_subscription_cleans_up_on_exit(self, store, backend):
        async with store.subscribe_changes():
            pass
        # After context manager exits, publish should not raise
        await store.add_target(_make_config("post-cleanup"))


# ---------------------------------------------------------------------------
# ConfigChangeEvent dataclass
# ---------------------------------------------------------------------------

class TestConfigChangeEvent:
    def test_add_event(self):
        e = ConfigChangeEvent(type="add", name="x", data={"a": 1})
        assert e.type == "add"
        assert e.name == "x"
        assert e.data == {"a": 1}

    def test_remove_event_data_none(self):
        e = ConfigChangeEvent(type="remove", name="x")
        assert e.data is None
