"""ProxyRepository CRUD + cascade, proven against every ConfigStore dialect.

Lives here rather than in proxy-hopper/tests/test_repository.py for the same
reason test_config_store_contract.py does: the sqlite/postgres entries
import proxy_hopper_sql, and doing that from inside the core package's own
test suite would make core depend on proxy-hopper-sql. test_repository.py
keeps testing ProxyRepository's business logic against one representative
implementation (MemoryConfigStore); this file is the equivalence proof
across dialects, same split already used for Backend (memory/redis).

Backend stays fixed to MemoryBackend throughout — this file is proving
ConfigStore dialect equivalence, not Backend equivalence (already covered
by test_backend_contract.py).
"""

from __future__ import annotations

import asyncio
import os

import pytest

from proxy_hopper.backend.memory import MemoryBackend
from proxy_hopper.config import IpPool, IpRequest, ProxyProvider, ResolvedIP, TargetConfig
from proxy_hopper.config_source import MergedFileConfig, MergedPool, MergedProvider, MergedTargetSpec
from proxy_hopper.config_store.base import ConfigStore
from proxy_hopper.config_store.memory import MemoryConfigStore
from proxy_hopper.repository import ProxyRepository
from proxy_hopper_sql import migrations
from proxy_hopper_sql.config_store import SqlConfigStore

_POSTGRES_URL = os.environ.get("POSTGRES_URL", "")


def _make_memory(tmp_path) -> ConfigStore:
    return MemoryConfigStore()


def _make_sqlite(tmp_path) -> ConfigStore:
    url = f"sqlite+aiosqlite:///{tmp_path / 'config.db'}"
    migrations.upgrade(url)
    return SqlConfigStore(url)


def _make_postgres(tmp_path) -> ConfigStore:
    return SqlConfigStore(_POSTGRES_URL)


_STORE_FACTORIES = {
    "memory": _make_memory,
    "sqlite": _make_sqlite,
}
if _POSTGRES_URL:
    _STORE_FACTORIES["postgres"] = _make_postgres


@pytest.fixture(scope="session", autouse=True)
def _postgres_schema() -> None:
    if _POSTGRES_URL:
        migrations.upgrade(_POSTGRES_URL)


@pytest.fixture(params=list(_STORE_FACTORIES))
def store_name(request) -> str:
    return request.param


@pytest.fixture
async def config_store(store_name, tmp_path) -> ConfigStore:
    s = await asyncio.to_thread(_STORE_FACTORIES[store_name], tmp_path)
    await s.start()
    if store_name == "postgres":
        from sqlalchemy import text
        async with s._engine.begin() as conn:
            await conn.execute(text("DELETE FROM config_entities"))
    yield s
    await s.stop()


@pytest.fixture
async def backend():
    b = MemoryBackend()
    await b.start()
    yield b
    await b.stop()


@pytest.fixture
async def repo(config_store, backend) -> ProxyRepository:
    return ProxyRepository(config_store=config_store, backend=backend)


def _target(name="t", pool_name="p", ip_list=None, **kw) -> TargetConfig:
    ips = ip_list or ["1.2.3.4:3128"]
    resolved = []
    for entry in ips:
        host, _, port_str = entry.rpartition(":")
        resolved.append(ResolvedIP(host=host, port=int(port_str)))
    return TargetConfig(name=name, regex=r".*", pool_name=pool_name, resolved_ips=resolved, **kw)


class TestCrudRoundTrip:
    async def test_add_get_update_remove_target(self, repo):
        await repo.add_target(_target("rt"))
        got = await repo.get_target("rt")
        assert got is not None and got.name == "rt"

        updated = _target("rt", min_request_interval=5.0)
        await repo.update_target(updated)
        got = await repo.get_target("rt")
        assert got.min_request_interval == 5.0

        await repo.remove_target("rt")
        assert await repo.get_target("rt") is None

    async def test_add_get_provider(self, repo):
        p = ProxyProvider(name="prov", ip_list=["1.1.1.1:3128"])
        await repo.add_provider(p)
        got = await repo.get_provider("prov")
        assert got is not None and got.ip_list == ["1.1.1.1:3128"]

    async def test_add_get_pool(self, repo):
        pool = IpPool(name="pool", ip_requests=[IpRequest(provider="prov", count=1)])
        await repo.add_pool(pool)
        got = await repo.get_pool("pool")
        assert got is not None and got.ip_requests[0].count == 1


class TestCascade:
    async def test_provider_update_cascades_to_target_across_dialects(self, repo, config_store):
        provider = ProxyProvider(name="prov", ip_list=["1.1.1.1:3128"])
        await repo.seed_provider(provider)
        pool = IpPool(name="pool", ip_requests=[IpRequest(provider="prov", count=1)])
        await repo.seed_pool(pool)
        target = TargetConfig(
            name="t", regex=r".*", pool_name="pool",
            resolved_ips=[ResolvedIP(host="1.1.1.1", port=3128, provider="prov")],
        )
        await repo.seed_target(target)

        await repo.update_provider(provider.model_copy(update={"ip_list": ["9.9.9.9:3128"]}))

        updated = await repo.get_target("t")
        assert updated.resolved_ips[0].host == "9.9.9.9"

        # Same assertion straight against the store — proves the write
        # landed in durable storage, not an intermediate cache.
        entity = await config_store.get("target", "t")
        assert entity.data["resolved_ips"][0]["host"] == "9.9.9.9"


# ---------------------------------------------------------------------------
# ProxyRepository.reconcile() -- Phase 4, CONFIG_RECONCILER_SCOPE.md §5
# ---------------------------------------------------------------------------

def _merged(providers=None, pools=None, target_specs=None) -> MergedFileConfig:
    return MergedFileConfig(
        providers=providers or [],
        pools=pools or [],
        target_specs=target_specs or [],
    )


def _std_scenario(target_static: bool = True) -> tuple[MergedFileConfig, ProxyProvider, IpPool]:
    """A provider + pool + target that resolve cleanly together."""
    provider = ProxyProvider(name="prov", ip_list=["1.1.1.1:8080"], static=True)
    pool = IpPool(name="pool", ip_requests=[IpRequest(provider="prov", count=5)], static=True)
    target_fields = {"name": "t1", "regex": ".*", "static": target_static}
    merged = _merged(
        providers=[MergedProvider(provider=provider, source_file="a.yaml")],
        pools=[MergedPool(pool=pool, source_file="a.yaml")],
        target_specs=[MergedTargetSpec(fields=target_fields, pool_ref="pool", source_file="a.yaml")],
    )
    return merged, provider, pool


async def _collect_events(repo) -> tuple[asyncio.Task, list]:
    events: list = []

    async def collect():
        async with repo.subscribe_changes() as evts:
            async for e in evts:
                events.append(e)

    task = asyncio.create_task(collect())
    await asyncio.sleep(0)
    return task, events


async def _stop_collecting(task: asyncio.Task) -> None:
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


class TestReconcileCreate:
    async def test_new_static_provider_created_and_published(self, repo, config_store):
        provider = ProxyProvider(name="prov", ip_list=["1.1.1.1:8080"], static=True)
        merged = _merged(providers=[MergedProvider(provider=provider, source_file="providers.yaml")])

        task, events = await _collect_events(repo)
        errors = await repo.reconcile(merged)
        await _stop_collecting(task)

        assert errors == []
        got = await repo.get_provider("prov")
        assert got is not None and got.ip_list == ["1.1.1.1:8080"]
        entity = await config_store.get("provider", "prov")
        assert entity.source_file == "providers.yaml"
        assert len(events) == 1
        assert events[0].entity == "provider"
        assert events[0].type == "add"
        assert events[0].name == "prov"

    async def test_provider_pool_target_created_together(self, repo):
        merged, _, _ = _std_scenario()
        errors = await repo.reconcile(merged)

        assert errors == []
        target = await repo.get_target("t1")
        assert target is not None
        assert {ip.address for ip in target.resolved_ips} == {"1.1.1.1:8080"}


class TestReconcileUpdate:
    async def test_provider_ip_change_updates_target_and_publishes_update_events(self, repo):
        merged1, provider, pool = _std_scenario()
        await repo.reconcile(merged1)

        changed_provider = provider.model_copy(update={"ip_list": ["9.9.9.9:8080"]})
        merged2 = _merged(
            providers=[MergedProvider(provider=changed_provider, source_file="a.yaml")],
            pools=[MergedPool(pool=pool, source_file="a.yaml")],
            target_specs=[MergedTargetSpec(
                fields={"name": "t1", "regex": ".*", "static": True},
                pool_ref="pool", source_file="a.yaml",
            )],
        )

        task, events = await _collect_events(repo)
        errors = await repo.reconcile(merged2)
        await _stop_collecting(task)

        assert errors == []
        target = await repo.get_target("t1")
        assert target.resolved_ips[0].host == "9.9.9.9"

        target_events = [e for e in events if e.entity == "target" and e.name == "t1"]
        assert len(target_events) == 1 and target_events[0].type == "update"
        provider_events = [e for e in events if e.entity == "provider" and e.name == "prov"]
        assert len(provider_events) == 1 and provider_events[0].type == "update"


class TestReconcileRemove:
    async def test_static_target_absent_from_merge_is_removed(self, repo):
        merged1, provider, pool = _std_scenario()
        await repo.reconcile(merged1)
        assert await repo.get_target("t1") is not None

        merged2 = _merged(
            providers=[MergedProvider(provider=provider, source_file="a.yaml")],
            pools=[MergedPool(pool=pool, source_file="a.yaml")],
            target_specs=[],
        )

        task, events = await _collect_events(repo)
        errors = await repo.reconcile(merged2)
        await _stop_collecting(task)

        assert errors == []
        assert await repo.get_target("t1") is None
        remove_events = [e for e in events if e.entity == "target" and e.type == "remove"]
        assert len(remove_events) == 1 and remove_events[0].name == "t1"

    async def test_non_static_entity_is_never_auto_removed(self, repo):
        merged1, provider, pool = _std_scenario(target_static=False)
        await repo.reconcile(merged1)
        assert await repo.get_target("t1") is not None

        merged2 = _merged(
            providers=[MergedProvider(provider=provider, source_file="a.yaml")],
            pools=[MergedPool(pool=pool, source_file="a.yaml")],
            target_specs=[],
        )
        errors = await repo.reconcile(merged2)
        assert errors == []
        assert await repo.get_target("t1") is not None


class TestReconcileNamespaceConflict:
    async def test_file_cannot_claim_name_owned_by_admin_created_entity(self, repo):
        admin_target = TargetConfig(
            name="admin-t", regex="admin-owned", pool_name="whatever",
            resolved_ips=[ResolvedIP(host="1.2.3.4", port=8080)],
        )
        await repo.add_target(admin_target)  # static=False by default -> admin-owned

        merged = _merged(target_specs=[MergedTargetSpec(
            fields={"name": "admin-t", "regex": "file-owned", "static": True},
            pool_ref="does-not-matter", source_file="conflict.yaml",
        )])

        task, events = await _collect_events(repo)
        errors = await repo.reconcile(merged)
        await _stop_collecting(task)

        assert len(errors) == 1
        assert "admin-t" in errors[0]
        assert "conflict.yaml" in errors[0]
        still_there = await repo.get_target("admin-t")
        assert still_there.regex == "admin-owned"
        assert events == []

    async def test_file_cannot_claim_provider_name_owned_by_admin(self, repo):
        await repo.add_provider(ProxyProvider(name="admin-p", ip_list=["1.1.1.1:8080"]))

        merged = _merged(providers=[MergedProvider(
            provider=ProxyProvider(name="admin-p", ip_list=["9.9.9.9:8080"], static=True),
            source_file="conflict.yaml",
        )])
        errors = await repo.reconcile(merged)

        assert len(errors) == 1
        assert "admin-p" in errors[0] and "conflict.yaml" in errors[0]
        still_there = await repo.get_provider("admin-p")
        assert still_there.ip_list == ["1.1.1.1:8080"]


class TestReconcileNoOp:
    async def test_unchanged_merge_writes_and_publishes_nothing(self, repo):
        merged1, _, _ = _std_scenario()
        await repo.reconcile(merged1)

        # Fresh-but-equal model instances -- proves the diff is by value,
        # not by object identity.
        merged2, _, _ = _std_scenario()

        task, events = await _collect_events(repo)
        errors = await repo.reconcile(merged2)
        await _stop_collecting(task)

        assert errors == []
        assert events == []

    async def test_non_static_entity_untouched_even_if_file_field_changes(self, repo):
        merged1, provider, pool = _std_scenario(target_static=False)
        await repo.reconcile(merged1)
        assert (await repo.get_target("t1")).regex == ".*"

        merged2 = _merged(
            providers=[MergedProvider(provider=provider, source_file="a.yaml")],
            pools=[MergedPool(pool=pool, source_file="a.yaml")],
            target_specs=[MergedTargetSpec(
                fields={"name": "t1", "regex": "changed", "static": False},
                pool_ref="pool", source_file="a.yaml",
            )],
        )
        errors = await repo.reconcile(merged2)
        assert errors == []
        assert (await repo.get_target("t1")).regex == ".*"


class TestReconcileEmptyResultGuard:
    async def test_empty_merge_on_populated_store_trips_guard(self, repo):
        merged, _, _ = _std_scenario()
        await repo.reconcile(merged)

        errors = await repo.reconcile(_merged())

        assert len(errors) == 1
        assert "empty" in errors[0].lower()
        assert await repo.get_target("t1") is not None
        assert await repo.get_provider("prov") is not None
        assert await repo.get_pool("pool") is not None

    async def test_empty_merge_on_empty_store_is_not_an_error(self, repo):
        errors = await repo.reconcile(_merged())
        assert errors == []


class TestReconcileUnresolvableTarget:
    async def test_unknown_pool_reference_is_skipped_not_fatal(self, repo):
        provider = ProxyProvider(name="prov", ip_list=["1.1.1.1:8080"], static=True)
        merged = _merged(
            providers=[MergedProvider(provider=provider, source_file="a.yaml")],
            target_specs=[MergedTargetSpec(
                fields={"name": "bad", "regex": ".*", "static": True},
                pool_ref="does-not-exist", source_file="a.yaml",
            )],
        )
        errors = await repo.reconcile(merged)

        assert len(errors) == 1
        assert "bad" in errors[0] and "does-not-exist" in errors[0]
        assert await repo.get_target("bad") is None
        # The provider still gets created -- one bad target doesn't block others.
        assert await repo.get_provider("prov") is not None


class TestReconcileSourceFile:
    async def test_source_file_round_trips_through_the_configured_store(self, repo, config_store):
        merged, _, _ = _std_scenario()
        await repo.reconcile(merged)

        for entity_type, name in (("provider", "prov"), ("pool", "pool"), ("target", "t1")):
            entity = await config_store.get(entity_type, name)
            assert entity.source_file == "a.yaml"
