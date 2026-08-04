"""Phase 4 acceptance test: config-store persistence survives a process restart.

Tests proxy_hopper.wiring.build_repo() directly — the literal motivation for
this whole config-store migration (CONFIG_STORE_SCOPE.md's original
complaint was that admin-API-created config didn't durably survive a
restart, because durability piggybacked on Backend/Redis persistence, which
this migration treats as the wrong tool for that job).

Lives here, not in proxy-hopper/tests/, since build_repo()'s sqlite path
lazily imports proxy_hopper_sql — same reasoning as every other
cross-dialect test in this project (see test_config_store_contract.py).
"""

from __future__ import annotations

import asyncio

import pytest

from proxy_hopper.config import ResolvedIP, ServerConfig, TargetConfig
from proxy_hopper.wiring import build_repo
from proxy_hopper_sql import migrations


def _target(name: str) -> TargetConfig:
    return TargetConfig(
        name=name, regex=r".*", pool_name="p",
        resolved_ips=[ResolvedIP(host="1.2.3.4", port=3128)],
    )


class TestRestartPersistence:
    async def test_sqlite_config_survives_restart(self, tmp_path):
        url = f"sqlite+aiosqlite:///{tmp_path / 'config.db'}"
        # Schema must exist before build_repo() connects — real deployments
        # apply this via the `migrate` CLI / Helm migration job first.
        await asyncio.to_thread(migrations.upgrade, url)

        server = ServerConfig(backend="memory", config_store_url=url)

        backend1, config_store1, repo1 = await build_repo(server)
        await repo1.add_target(_target("survives"))
        await backend1.stop()
        await config_store1.stop()

        # Simulate a restart: fresh Backend + fresh SqlConfigStore, same file.
        backend2, config_store2, repo2 = await build_repo(server)
        got = await repo2.get_target("survives")
        await backend2.stop()
        await config_store2.stop()

        assert got is not None
        assert got.name == "survives"

    async def test_memory_config_store_does_not_survive_restart(self):
        """Confirms today's behaviour is unchanged for anyone who doesn't
        opt in — absence of persistence is a property worth locking down
        just as much as presence of it is."""
        server = ServerConfig(backend="memory")  # config_store_url unset

        backend1, config_store1, repo1 = await build_repo(server)
        await repo1.add_target(_target("ephemeral"))
        await backend1.stop()
        await config_store1.stop()

        backend2, config_store2, repo2 = await build_repo(server)
        got = await repo2.get_target("ephemeral")
        await backend2.stop()
        await config_store2.stop()

        assert got is None
