"""Tests for ProxyRepository — target CRUD, provider CRUD, cascade, seeding, pub/sub."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from proxy_hopper.backend.memory import MemoryBackend
from proxy_hopper.config import ProxyProvider, ResolvedIP, TargetConfig
from proxy_hopper.repository import (
    ChangeEvent,
    ProxyRepository,
    _TARGET_PREFIX,
    _PROVIDER_PREFIX,
    _build_target,
    _dict_to_target,
    _dict_to_provider,
    _target_to_dict,
    _provider_to_dict,
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
async def repo(backend):
    return ProxyRepository(backend)


def _make_target(name="api", ip_list=None, **kw) -> TargetConfig:
    return _build_target(
        name=name,
        regex=r".*",
        ip_list=ip_list or ["1.2.3.4:3128"],
        **kw,
    )


def _make_provider(name="prov", ip_list=None, **kw) -> ProxyProvider:
    return ProxyProvider(
        name=name,
        ip_list=ip_list or ["10.0.0.1:3128"],
        **kw,
    )


# ---------------------------------------------------------------------------
# Serialisation round-trip — targets
# ---------------------------------------------------------------------------

class TestTargetSerialisation:
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

    def test_mutable_defaults_true(self):
        cfg = _build_target("t", r".*", ["1.2.3.4:3128"])
        assert cfg.mutable is True


# ---------------------------------------------------------------------------
# Serialisation round-trip — providers
# ---------------------------------------------------------------------------

class TestProviderSerialisation:
    def test_round_trip_basic(self):
        p = _make_provider("p", ["1.1.1.1:3128", "2.2.2.2:3128"])
        restored = _dict_to_provider(_provider_to_dict(p))
        assert restored.name == p.name
        assert restored.ip_list == p.ip_list
        assert restored.auth is None

    def test_round_trip_with_auth(self):
        from proxy_hopper.config import BasicAuth
        p = ProxyProvider(
            name="auth-prov",
            ip_list=["1.1.1.1:3128"],
            auth=BasicAuth(username="u", password="p"),
        )
        restored = _dict_to_provider(_provider_to_dict(p))
        assert restored.auth is not None
        assert restored.auth.username == "u"

    def test_round_trip_with_region_tag(self):
        p = ProxyProvider(name="eu", ip_list=["1.1.1.1:3128"], region_tag="eu-west")
        restored = _dict_to_provider(_provider_to_dict(p))
        assert restored.region_tag == "eu-west"

    def test_mutable_defaults_true(self):
        p = _make_provider()
        assert p.mutable is True


# ---------------------------------------------------------------------------
# Target CRUD
# ---------------------------------------------------------------------------

class TestAddTarget:
    async def test_add_persists_to_kv(self, repo, backend):
        cfg = _make_target("new-target")
        await repo.add_target(cfg)
        raw = await backend.kv_get(f"{_TARGET_PREFIX}new-target")
        assert raw is not None

    async def test_add_then_get_round_trips(self, repo):
        cfg = _make_target("roundtrip")
        await repo.add_target(cfg)
        got = await repo.get_target("roundtrip")
        assert got is not None
        assert got.name == "roundtrip"
        assert got.resolved_ips == cfg.resolved_ips

    async def test_add_duplicate_raises(self, repo):
        cfg = _make_target("dup")
        await repo.add_target(cfg)
        with pytest.raises(ValueError, match="already exists"):
            await repo.add_target(cfg)

    async def test_add_publishes_event(self, repo):
        events = []

        async def collect():
            async with repo.subscribe_changes() as evts:
                async for e in evts:
                    events.append(e)
                    return

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        await repo.add_target(_make_target("pub-add"))
        await asyncio.wait_for(task, timeout=1.0)

        assert len(events) == 1
        assert events[0].entity == "target"
        assert events[0].type == "add"
        assert events[0].name == "pub-add"
        assert events[0].data is not None


class TestUpdateTarget:
    async def test_update_overwrites_stored_value(self, repo):
        cfg = _make_target("upd", min_request_interval=1.0)
        await repo.add_target(cfg)
        updated = cfg.model_copy(update={"min_request_interval": 10.0})
        await repo.update_target(updated)
        got = await repo.get_target("upd")
        assert got.min_request_interval == 10.0

    async def test_update_nonexistent_raises(self, repo):
        cfg = _make_target("ghost")
        with pytest.raises(ValueError, match="does not exist"):
            await repo.update_target(cfg)

    async def test_update_immutable_target_raises(self, repo):
        cfg = _make_target("frozen", mutable=False)
        await repo.add_target(cfg)
        updated = cfg.model_copy(update={"min_request_interval": 99.0})
        with pytest.raises(ValueError, match="not mutable"):
            await repo.update_target(updated)

    async def test_update_publishes_event(self, repo):
        await repo.add_target(_make_target("pub-upd"))
        events = []

        async def collect():
            async with repo.subscribe_changes() as evts:
                async for e in evts:
                    events.append(e)
                    return

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        updated = _make_target("pub-upd", min_request_interval=99.0)
        await repo.update_target(updated)
        await asyncio.wait_for(task, timeout=1.0)

        assert events[0].entity == "target"
        assert events[0].type == "update"
        assert events[0].name == "pub-upd"


class TestRemoveTarget:
    async def test_remove_deletes_from_kv(self, repo, backend):
        await repo.add_target(_make_target("to-remove"))
        await repo.remove_target("to-remove")
        assert await backend.kv_get(f"{_TARGET_PREFIX}to-remove") is None

    async def test_remove_nonexistent_is_noop(self, repo):
        await repo.remove_target("does-not-exist")  # must not raise

    async def test_remove_publishes_event(self, repo):
        await repo.add_target(_make_target("pub-rm"))
        events = []

        async def collect():
            async with repo.subscribe_changes() as evts:
                async for e in evts:
                    events.append(e)
                    return

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        await repo.remove_target("pub-rm")
        await asyncio.wait_for(task, timeout=1.0)

        assert events[0].entity == "target"
        assert events[0].type == "remove"
        assert events[0].name == "pub-rm"
        assert events[0].data is None

    async def test_get_after_remove_returns_none(self, repo):
        await repo.add_target(_make_target("del-me"))
        await repo.remove_target("del-me")
        assert await repo.get_target("del-me") is None


class TestGetTarget:
    async def test_get_missing_returns_none(self, repo):
        assert await repo.get_target("nope") is None

    async def test_get_returns_correct_target(self, repo):
        await repo.add_target(_make_target("find-me"))
        got = await repo.get_target("find-me")
        assert got is not None
        assert got.name == "find-me"


class TestListTargets:
    async def test_empty_store_returns_empty(self, repo):
        assert await repo.list_targets() == []

    async def test_lists_all_added_targets(self, repo):
        await repo.add_target(_make_target("a"))
        await repo.add_target(_make_target("b"))
        targets = await repo.list_targets()
        assert {t.name for t in targets} == {"a", "b"}

    async def test_removed_targets_not_listed(self, repo):
        await repo.add_target(_make_target("keep"))
        await repo.add_target(_make_target("drop"))
        await repo.remove_target("drop")
        targets = await repo.list_targets()
        assert [t.name for t in targets] == ["keep"]

    async def test_only_lists_target_prefix(self, repo, backend):
        await backend.kv_set("ph:repo:provider:noise", '{"name": "noise", "ip_list": ["1.1.1.1:3128"], "mutable": true}')
        await repo.add_target(_make_target("real"))
        targets = await repo.list_targets()
        assert len(targets) == 1
        assert targets[0].name == "real"


# ---------------------------------------------------------------------------
# Provider CRUD
# ---------------------------------------------------------------------------

class TestAddProvider:
    async def test_add_persists_to_kv(self, repo, backend):
        p = _make_provider("new-prov")
        await repo.add_provider(p)
        raw = await backend.kv_get(f"{_PROVIDER_PREFIX}new-prov")
        assert raw is not None

    async def test_add_then_get_round_trips(self, repo):
        p = _make_provider("roundtrip")
        await repo.add_provider(p)
        got = await repo.get_provider("roundtrip")
        assert got is not None
        assert got.name == "roundtrip"
        assert got.ip_list == p.ip_list

    async def test_add_duplicate_raises(self, repo):
        p = _make_provider("dup")
        await repo.add_provider(p)
        with pytest.raises(ValueError, match="already exists"):
            await repo.add_provider(p)

    async def test_add_publishes_event(self, repo):
        events = []

        async def collect():
            async with repo.subscribe_changes() as evts:
                async for e in evts:
                    if e.entity == "provider":
                        events.append(e)
                        return

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        await repo.add_provider(_make_provider("pub-add"))
        await asyncio.wait_for(task, timeout=1.0)

        assert events[0].entity == "provider"
        assert events[0].type == "add"
        assert events[0].name == "pub-add"


class TestUpdateProvider:
    async def test_update_overwrites_stored_value(self, repo):
        p = _make_provider("upd", ["1.1.1.1:3128"])
        await repo.add_provider(p)
        updated = p.model_copy(update={"ip_list": ["2.2.2.2:3128"]})
        await repo.update_provider(updated)
        got = await repo.get_provider("upd")
        assert got.ip_list == ["2.2.2.2:3128"]

    async def test_update_nonexistent_raises(self, repo):
        p = _make_provider("ghost")
        with pytest.raises(ValueError, match="does not exist"):
            await repo.update_provider(p)

    async def test_update_immutable_provider_raises(self, repo):
        p = ProxyProvider(name="frozen", ip_list=["1.1.1.1:3128"], mutable=False)
        await repo.add_provider(p)
        updated = p.model_copy(update={"ip_list": ["9.9.9.9:3128"]})
        with pytest.raises(ValueError, match="not mutable"):
            await repo.update_provider(updated)


class TestRemoveProvider:
    async def test_remove_deletes_from_kv(self, repo, backend):
        await repo.add_provider(_make_provider("to-remove"))
        await repo.remove_provider("to-remove")
        assert await backend.kv_get(f"{_PROVIDER_PREFIX}to-remove") is None

    async def test_remove_nonexistent_is_noop(self, repo):
        await repo.remove_provider("does-not-exist")  # must not raise

    async def test_remove_publishes_event(self, repo):
        await repo.add_provider(_make_provider("pub-rm"))
        events = []

        async def collect():
            async with repo.subscribe_changes() as evts:
                async for e in evts:
                    if e.entity == "provider":
                        events.append(e)
                        return

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        await repo.remove_provider("pub-rm")
        await asyncio.wait_for(task, timeout=1.0)

        assert events[0].entity == "provider"
        assert events[0].type == "remove"


class TestGetProvider:
    async def test_get_missing_returns_none(self, repo):
        assert await repo.get_provider("nope") is None


class TestListProviders:
    async def test_empty_returns_empty(self, repo):
        assert await repo.list_providers() == []

    async def test_lists_all_added_providers(self, repo):
        await repo.add_provider(_make_provider("a"))
        await repo.add_provider(_make_provider("b"))
        providers = await repo.list_providers()
        assert {p.name for p in providers} == {"a", "b"}

    async def test_only_lists_provider_prefix(self, repo, backend):
        await backend.kv_set(f"{_TARGET_PREFIX}noise", '{"name":"noise"}')
        await repo.add_provider(_make_provider("real"))
        providers = await repo.list_providers()
        assert len(providers) == 1
        assert providers[0].name == "real"


# ---------------------------------------------------------------------------
# Provider cascade
# ---------------------------------------------------------------------------

class TestCascadeProvider:
    async def test_update_provider_rebuilds_target_ips(self, repo):
        """Updating provider IPs cascades to targets referencing it."""
        # Target has IPs tagged with provider "prov"
        ip1 = ResolvedIP(host="1.1.1.1", port=3128, provider="prov")
        ip2 = ResolvedIP(host="2.2.2.2", port=3128, provider="prov")
        cfg = _build_target("t", r".*", ["1.1.1.1:3128"])
        cfg = cfg.model_copy(update={"resolved_ips": [ip1, ip2]})
        await repo.add_target(cfg)

        p = ProxyProvider(name="prov", ip_list=["9.9.9.9:3128"])
        await repo.add_provider(p)

        # Cascade should have replaced provider IPs
        updated = await repo.get_target("t")
        assert len(updated.resolved_ips) == 1
        assert updated.resolved_ips[0].host == "9.9.9.9"
        assert updated.resolved_ips[0].provider == "prov"

    async def test_cascade_preserves_non_provider_ips(self, repo):
        """IPs not tagged with the provider must survive cascade."""
        inline_ip = ResolvedIP(host="5.5.5.5", port=3128)  # no provider
        prov_ip = ResolvedIP(host="1.1.1.1", port=3128, provider="prov")
        cfg = _build_target("t", r".*", ["5.5.5.5:3128"])
        cfg = cfg.model_copy(update={"resolved_ips": [inline_ip, prov_ip]})
        await repo.add_target(cfg)

        p = ProxyProvider(name="prov", ip_list=["9.9.9.9:3128"])
        await repo.add_provider(p)

        updated = await repo.get_target("t")
        addresses = {ip.host for ip in updated.resolved_ips}
        assert "5.5.5.5" in addresses   # inline preserved
        assert "9.9.9.9" in addresses   # provider replaced
        assert "1.1.1.1" not in addresses

    async def test_cascade_emits_target_update_events(self, repo):
        """Cascade must publish target:update events for affected targets."""
        prov_ip = ResolvedIP(host="1.1.1.1", port=3128, provider="prov")
        cfg = _build_target("t", r".*", ["1.1.1.1:3128"])
        cfg = cfg.model_copy(update={"resolved_ips": [prov_ip]})
        await repo.add_target(cfg)

        target_events = []

        async def collect():
            async with repo.subscribe_changes() as evts:
                async for e in evts:
                    if e.entity == "target":
                        target_events.append(e)
                        return

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        await repo.add_provider(ProxyProvider(name="prov", ip_list=["9.9.9.9:3128"]))
        await asyncio.wait_for(task, timeout=1.0)

        assert len(target_events) == 1
        assert target_events[0].type == "update"
        assert target_events[0].name == "t"

    async def test_cascade_skips_unrelated_targets(self, repo):
        """Targets with no IPs from the provider must not be touched."""
        cfg = _build_target("unrelated", r".*", ["8.8.8.8:3128"])
        await repo.add_target(cfg)

        p = ProxyProvider(name="prov", ip_list=["9.9.9.9:3128"])
        await repo.add_provider(p)

        unchanged = await repo.get_target("unrelated")
        assert unchanged.resolved_ips[0].host == "8.8.8.8"


# ---------------------------------------------------------------------------
# Target IP list helpers
# ---------------------------------------------------------------------------

class TestAddIp:
    async def test_add_ip_appends_to_list(self, repo):
        await repo.add_target(_make_target("t", ["1.1.1.1:3128"]))
        await repo.add_ip("t", "2.2.2.2:3128")
        got = await repo.get_target("t")
        assert len(got.resolved_ips) == 2
        assert any(ip.address == "2.2.2.2:3128" for ip in got.resolved_ips)

    async def test_add_ip_missing_target_raises(self, repo):
        with pytest.raises(ValueError, match="not found"):
            await repo.add_ip("ghost", "1.1.1.1:3128")

    async def test_add_ip_uses_default_port(self, repo):
        await repo.add_target(_make_target("t", default_proxy_port=9999))
        await repo.add_ip("t", "5.5.5.5")
        got = await repo.get_target("t")
        assert any(ip.port == 9999 for ip in got.resolved_ips)


class TestRemoveIp:
    async def test_remove_ip_removes_from_list(self, repo):
        await repo.add_target(_make_target("t", ["1.1.1.1:3128", "2.2.2.2:3128"]))
        await repo.remove_ip("t", "1.1.1.1:3128")
        got = await repo.get_target("t")
        assert len(got.resolved_ips) == 1
        assert got.resolved_ips[0].address == "2.2.2.2:3128"

    async def test_remove_last_ip_raises(self, repo):
        await repo.add_target(_make_target("t", ["1.1.1.1:3128"]))
        with pytest.raises(ValueError, match="at least one IP"):
            await repo.remove_ip("t", "1.1.1.1:3128")

    async def test_remove_ip_missing_target_raises(self, repo):
        with pytest.raises(ValueError, match="not found"):
            await repo.remove_ip("ghost", "1.1.1.1:3128")


class TestSwapIp:
    async def test_swap_replaces_address(self, repo):
        await repo.add_target(_make_target("t", ["1.1.1.1:3128", "2.2.2.2:3128"]))
        await repo.swap_ip("t", "1.1.1.1:3128", "3.3.3.3:3128")
        got = await repo.get_target("t")
        addresses = [ip.address for ip in got.resolved_ips]
        assert "3.3.3.3:3128" in addresses
        assert "1.1.1.1:3128" not in addresses
        assert "2.2.2.2:3128" in addresses

    async def test_swap_old_not_found_raises(self, repo):
        await repo.add_target(_make_target("t", ["1.1.1.1:3128"]))
        with pytest.raises(ValueError, match="not found"):
            await repo.swap_ip("t", "9.9.9.9:3128", "2.2.2.2:3128")

    async def test_swap_missing_target_raises(self, repo):
        with pytest.raises(ValueError, match="not found"):
            await repo.swap_ip("ghost", "1.1.1.1:3128", "2.2.2.2:3128")

    async def test_swap_preserves_list_length(self, repo):
        await repo.add_target(_make_target("t", ["1.1.1.1:3128", "2.2.2.2:3128"]))
        await repo.swap_ip("t", "1.1.1.1:3128", "5.5.5.5:3128")
        got = await repo.get_target("t")
        assert len(got.resolved_ips) == 2


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

class TestSeedTarget:
    async def test_seed_writes_if_absent(self, repo):
        cfg = _make_target("new")
        await repo.seed_target(cfg)
        got = await repo.get_target("new")
        assert got is not None
        assert got.name == "new"

    async def test_seed_skips_if_present(self, repo):
        cfg = _make_target("existing", min_request_interval=1.0)
        await repo.add_target(cfg)
        seed = _make_target("existing", min_request_interval=99.0)
        await repo.seed_target(seed)
        got = await repo.get_target("existing")
        # original value preserved — seed did not overwrite
        assert got.min_request_interval == 1.0

    async def test_seed_publishes_no_events(self, repo):
        events = []

        async def collect():
            async with repo.subscribe_changes() as evts:
                async for e in evts:
                    events.append(e)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        await repo.seed_target(_make_target("silent"))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert events == []


class TestSeedProvider:
    async def test_seed_writes_if_absent(self, repo):
        p = _make_provider("new")
        await repo.seed_provider(p)
        got = await repo.get_provider("new")
        assert got is not None
        assert got.name == "new"

    async def test_seed_skips_if_present(self, repo):
        p = _make_provider("existing", ["1.1.1.1:3128"])
        await repo.add_provider(p)
        seed = _make_provider("existing", ["9.9.9.9:3128"])
        await repo.seed_provider(seed)
        got = await repo.get_provider("existing")
        assert got.ip_list == ["1.1.1.1:3128"]  # original preserved

    async def test_seed_publishes_no_events(self, repo):
        events = []

        async def collect():
            async with repo.subscribe_changes() as evts:
                async for e in evts:
                    events.append(e)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        await repo.seed_provider(_make_provider("silent"))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert events == []


# ---------------------------------------------------------------------------
# subscribe_changes
# ---------------------------------------------------------------------------

class TestSubscribeChanges:
    async def test_receives_target_add_event(self, repo):
        received = []

        async def listen():
            async with repo.subscribe_changes() as events:
                async for e in events:
                    received.append(e)
                    return

        task = asyncio.create_task(listen())
        await asyncio.sleep(0)
        await repo.add_target(_make_target("sub-test"))
        await asyncio.wait_for(task, timeout=1.0)

        assert len(received) == 1
        evt = received[0]
        assert isinstance(evt, ChangeEvent)
        assert evt.entity == "target"
        assert evt.type == "add"
        assert evt.name == "sub-test"

    async def test_receives_provider_add_event(self, repo):
        received = []

        async def listen():
            async with repo.subscribe_changes() as events:
                async for e in events:
                    if e.entity == "provider":
                        received.append(e)
                        return

        task = asyncio.create_task(listen())
        await asyncio.sleep(0)
        await repo.add_provider(_make_provider("prov-sub"))
        await asyncio.wait_for(task, timeout=1.0)

        assert received[0].entity == "provider"
        assert received[0].type == "add"

    async def test_multiple_subscribers_all_receive(self, repo):
        received_a: list[ChangeEvent] = []
        received_b: list[ChangeEvent] = []

        async def listen(out):
            async with repo.subscribe_changes() as events:
                async for e in events:
                    out.append(e)
                    return

        t1 = asyncio.create_task(listen(received_a))
        t2 = asyncio.create_task(listen(received_b))
        await asyncio.sleep(0)
        await repo.add_target(_make_target("broadcast"))
        await asyncio.wait_for(asyncio.gather(t1, t2), timeout=1.0)

        assert received_a[0].name == "broadcast"
        assert received_b[0].name == "broadcast"

    async def test_subscription_cleans_up_on_exit(self, repo):
        async with repo.subscribe_changes():
            pass
        # After context manager exits, publish should not raise
        await repo.add_target(_make_target("post-cleanup"))


# ---------------------------------------------------------------------------
# ChangeEvent dataclass
# ---------------------------------------------------------------------------

class TestChangeEvent:
    def test_target_add_event(self):
        e = ChangeEvent(entity="target", type="add", name="x", data={"a": 1})
        assert e.entity == "target"
        assert e.type == "add"
        assert e.name == "x"
        assert e.data == {"a": 1}

    def test_provider_remove_event_data_none(self):
        e = ChangeEvent(entity="provider", type="remove", name="x")
        assert e.data is None
