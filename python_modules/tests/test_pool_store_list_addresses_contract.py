"""IPPoolStore.list_addresses contract tests.

Every test in this module runs against *each* registered backend type via
the parametrized ``pool_store`` fixture -- see conftest.py.

list_addresses() is the read side of the reconcile-on-start fix (see
CONFIG_RECONCILER_SCOPE.md §6): it lets IdentityQueue.start() diff
already-registered addresses against the current config on *every* start,
instead of relying on a one-shot claim_init race that only the very first
process to ever start a target wins.
"""

from __future__ import annotations


class TestListAddresses:
    async def test_empty_when_nothing_registered(self, pool_store):
        assert await pool_store.list_addresses("t1") == []

    async def test_returns_registered_address(self, pool_store):
        await pool_store.ip_set("t1", "1.2.3.4:8080", "uuid-1")
        assert await pool_store.list_addresses("t1") == ["1.2.3.4:8080"]

    async def test_returns_multiple_addresses(self, pool_store):
        await pool_store.ip_set("t1", "1.2.3.4:8080", "uuid-1")
        await pool_store.ip_set("t1", "5.6.7.8:8080", "uuid-2")
        assert set(await pool_store.list_addresses("t1")) == {
            "1.2.3.4:8080",
            "5.6.7.8:8080",
        }

    async def test_deleted_address_not_listed(self, pool_store):
        await pool_store.ip_set("t1", "1.2.3.4:8080", "uuid-1")
        await pool_store.ip_delete("t1", "1.2.3.4:8080")
        assert await pool_store.list_addresses("t1") == []

    async def test_scoped_to_target(self, pool_store):
        await pool_store.ip_set("t1", "1.2.3.4:8080", "uuid-1")
        await pool_store.ip_set("t2", "5.6.7.8:8080", "uuid-2")
        assert await pool_store.list_addresses("t1") == ["1.2.3.4:8080"]
        assert await pool_store.list_addresses("t2") == ["5.6.7.8:8080"]

    async def test_does_not_confuse_ip_key_with_retired_key(self, pool_store):
        await pool_store.ip_set("t1", "1.2.3.4:8080", "uuid-1")
        await pool_store.retire_add("t1", "9.9.9.9:8080")
        assert await pool_store.list_addresses("t1") == ["1.2.3.4:8080"]

    async def test_does_not_confuse_ip_key_with_failures_key(self, pool_store):
        await pool_store.ip_set("t1", "1.2.3.4:8080", "uuid-1")
        await pool_store.increment_failures("t1", "9.9.9.9:8080")
        assert await pool_store.list_addresses("t1") == ["1.2.3.4:8080"]
