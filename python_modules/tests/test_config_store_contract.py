"""Generic ConfigStore contract tests.

Parametrized over every registered store type. Adding a new implementation
requires only adding an entry to _STORE_FACTORIES — every test below then
runs against it automatically. Mirrors conftest.py's _BACKEND_FACTORIES
pattern.

Lives in this cross-package `proxy-hopper-tests` project (not inside
proxy-hopper/tests/) for the same reason the Backend contract suite does:
exercising SqlConfigStore here would otherwise make the core proxy-hopper
package's own test suite depend on proxy-hopper-sql.

sqlite is backed by a temp *file* per test (pytest's built-in tmp_path,
already function-scoped/unique) rather than :memory: — async connection
pooling makes :memory: behave inconsistently across connections.

postgres, like the redis backend contract, only runs when POSTGRES_URL is
set in the environment (a real service container in CI); skipped locally
without one — there's no in-memory Postgres fake worth reaching for here.
Unlike sqlite's temp-file-per-test isolation, postgres tests share one
long-lived database, so the table is truncated before each test instead.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import text

from proxy_hopper.config_store.base import ConfigStore
from proxy_hopper.config_store.memory import MemoryConfigStore
from proxy_hopper_sql import migrations
from proxy_hopper_sql.config_store import SqlConfigStore

_POSTGRES_URL = os.environ.get("POSTGRES_URL", "")


def _make_memory(tmp_path) -> ConfigStore:
    return MemoryConfigStore()


def _make_sqlite(tmp_path) -> ConfigStore:
    # migrations.upgrade() calls alembic, which internally does its own
    # asyncio.run() — can't call it directly from inside a running loop
    # (i.e. from an async fixture), so factories are invoked via
    # asyncio.to_thread below instead of awaited directly.
    url = f"sqlite+aiosqlite:///{tmp_path / 'config.db'}"
    migrations.upgrade(url)
    return SqlConfigStore(url)


def _make_postgres(tmp_path) -> ConfigStore:
    # Schema is applied once, session-wide, by _postgres_schema below.
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
async def store(store_name, tmp_path) -> ConfigStore:
    s = await asyncio.to_thread(_STORE_FACTORIES[store_name], tmp_path)
    await s.start()
    if store_name == "postgres":
        async with s._engine.begin() as conn:
            await conn.execute(text("DELETE FROM config_entities"))
    yield s
    await s.stop()


class TestLifecycle:
    async def test_start_stop(self, store_name, tmp_path):
        s = await asyncio.to_thread(_STORE_FACTORIES[store_name], tmp_path)
        await s.start()
        await s.stop()


class TestRoundTrip:
    async def test_set_then_get(self, store):
        await store.set("target", "t1", {"regex": ".*"}, static=False, mutable=True)
        entity = await store.get("target", "t1")
        assert entity is not None
        assert entity.name == "t1"
        assert entity.data == {"regex": ".*"}
        assert entity.static is False
        assert entity.mutable is True

    async def test_source_file_defaults_to_none(self, store):
        await store.set("target", "t1", {"regex": ".*"}, static=False, mutable=True)
        entity = await store.get("target", "t1")
        assert entity.source_file is None

    async def test_source_file_round_trips(self, store):
        await store.set(
            "provider", "p1", {"ipList": []}, static=True, mutable=False,
            source_file="providers/aws.yaml",
        )
        entity = await store.get("provider", "p1")
        assert entity.source_file == "providers/aws.yaml"

    async def test_source_file_round_trips_through_list(self, store):
        await store.set(
            "provider", "p1", {"ipList": []}, static=True, mutable=False,
            source_file="providers/aws.yaml",
        )
        entities = await store.list("provider")
        assert entities[0].source_file == "providers/aws.yaml"

    async def test_source_file_updated_on_overwrite(self, store):
        await store.set(
            "target", "t1", {"v": 1}, static=True, mutable=False,
            source_file="01-first.yaml",
        )
        await store.set(
            "target", "t1", {"v": 1}, static=True, mutable=False,
            source_file="02-second.yaml",
        )
        entity = await store.get("target", "t1")
        assert entity.source_file == "02-second.yaml"

    async def test_get_missing_returns_none(self, store):
        assert await store.get("target", "does-not-exist") is None

    async def test_delete(self, store):
        await store.set("provider", "p1", {"ipList": []}, static=False, mutable=True)
        await store.delete("provider", "p1")
        assert await store.get("provider", "p1") is None

    async def test_delete_missing_is_noop(self, store):
        await store.delete("provider", "does-not-exist")


class TestStaticMutableFlags:
    async def test_static_true(self, store):
        await store.set("pool", "p1", {}, static=True, mutable=False)
        entity = await store.get("pool", "p1")
        assert entity.static is True
        assert entity.mutable is False

    async def test_static_false_mutable_true(self, store):
        await store.set("pool", "p1", {}, static=False, mutable=True)
        entity = await store.get("pool", "p1")
        assert entity.static is False
        assert entity.mutable is True


class TestOverwrite:
    async def test_set_replaces_data_and_flags(self, store):
        await store.set("target", "t1", {"v": 1}, static=True, mutable=False)
        first = await store.get("target", "t1")

        await store.set("target", "t1", {"v": 2}, static=False, mutable=True)
        second = await store.get("target", "t1")

        assert second.data == {"v": 2}
        assert second.static is False
        assert second.mutable is True
        assert second.updated_at >= first.updated_at

    async def test_overwrite_does_not_create_duplicate(self, store):
        await store.set("target", "t1", {"v": 1}, static=False, mutable=True)
        await store.set("target", "t1", {"v": 2}, static=False, mutable=True)
        assert len(await store.list("target")) == 1


class TestList:
    async def test_list_empty(self, store):
        assert await store.list("target") == []

    async def test_list_returns_only_requested_type(self, store):
        await store.set("target", "t1", {}, static=False, mutable=True)
        await store.set("provider", "pr1", {}, static=False, mutable=True)
        targets = await store.list("target")
        assert [e.name for e in targets] == ["t1"]

    async def test_list_multiple(self, store):
        await store.set("target", "t1", {}, static=False, mutable=True)
        await store.set("target", "t2", {}, static=False, mutable=True)
        names = {e.name for e in await store.list("target")}
        assert names == {"t1", "t2"}
