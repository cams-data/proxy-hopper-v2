"""In-process dict-of-dicts ConfigStore — test-only, not durable.

Kept for contract-test symmetry with MemoryBackend, and as the zero-config
default when no `config_store_url` is set. See CONFIG_STORE_SCOPE.md:
SQLite strictly dominates this for real deployments (same zero-external-
dependency property, but durable across restarts), so this implementation
is not exposed as a CLI/chart option.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .base import ConfigEntity, ConfigStore


class MemoryConfigStore(ConfigStore):
    """dict-of-dicts store, keyed by (entity_type, name). Not durable."""

    def __init__(self) -> None:
        self._entities: dict[str, dict[str, ConfigEntity]] = {}

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def get(self, entity_type: str, name: str) -> ConfigEntity | None:
        return self._entities.get(entity_type, {}).get(name)

    async def set(
        self,
        entity_type: str,
        name: str,
        data: dict,
        *,
        static: bool,
        mutable: bool,
    ) -> None:
        bucket = self._entities.setdefault(entity_type, {})
        bucket[name] = ConfigEntity(
            name=name,
            data=data,
            static=static,
            mutable=mutable,
            # Naive UTC — matches SqlConfigStore's convention (SQLite's
            # DATETIME type doesn't reliably round-trip tz-aware values),
            # kept consistent across every ConfigStore implementation.
            updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )

    async def delete(self, entity_type: str, name: str) -> None:
        self._entities.get(entity_type, {}).pop(name, None)

    async def list(self, entity_type: str) -> list[ConfigEntity]:
        return list(self._entities.get(entity_type, {}).values())
