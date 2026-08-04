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
