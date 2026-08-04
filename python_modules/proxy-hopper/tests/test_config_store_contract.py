"""Generic ConfigStore contract tests.

Parametrized over every registered store type. Adding a new implementation
(e.g. SqlConfigStore in a later phase) requires only adding an entry to
_STORE_FACTORIES — every test below then runs against it automatically.
Mirrors python_modules/tests/conftest.py's _BACKEND_FACTORIES pattern.
"""

from __future__ import annotations

import pytest

from proxy_hopper.config_store.base import ConfigStore
from proxy_hopper.config_store.memory import MemoryConfigStore

_STORE_FACTORIES = {
    "memory": MemoryConfigStore,
}


@pytest.fixture(params=list(_STORE_FACTORIES))
def store_name(request) -> str:
    return request.param


@pytest.fixture
async def store(store_name) -> ConfigStore:
    s = _STORE_FACTORIES[store_name]()
    await s.start()
    yield s
    await s.stop()


class TestLifecycle:
    async def test_start_stop(self, store_name):
        s = _STORE_FACTORIES[store_name]()
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
