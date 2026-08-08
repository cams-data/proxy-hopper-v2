"""In-process dict-of-dicts ConfigStore — test-only, not durable.

Kept for contract-test symmetry with MemoryBackend, and as the zero-config
default when no `config_store_url` is set. See CONFIG_STORE_SCOPE.md:
SQLite strictly dominates this for real deployments (same zero-external-
dependency property, but durable across restarts), so this implementation
is not exposed as a CLI/chart option.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Optional

from .base import ConfigEntity, ConfigStore


class MemoryConfigStore(ConfigStore):
    """dict-of-dicts store, keyed by (entity_type, name). Not durable.

    Deep-copies `data` on both set() and get()/list() so callers can never
    mutate the store's internal state through a returned ConfigEntity, or
    corrupt it by later mutating a dict they'd previously passed to set().
    A real SQL-backed store gets this isolation for free from its JSON
    (de)serialisation round-trip; this in-memory double has to do it
    explicitly to behave the same way.
    """

    def __init__(self) -> None:
        self._entities: dict[str, dict[str, ConfigEntity]] = {}

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def get(self, entity_type: str, name: str) -> ConfigEntity | None:
        entity = self._entities.get(entity_type, {}).get(name)
        return self._copy_entity(entity) if entity is not None else None

    async def set(
        self,
        entity_type: str,
        name: str,
        data: dict,
        *,
        static: bool,
        mutable: bool,
        source_file: Optional[str] = None,
    ) -> None:
        bucket = self._entities.setdefault(entity_type, {})
        bucket[name] = ConfigEntity(
            name=name,
            data=copy.deepcopy(data),
            static=static,
            mutable=mutable,
            # Naive UTC — matches SqlConfigStore's convention (SQLite's
            # DATETIME type doesn't reliably round-trip tz-aware values),
            # kept consistent across every ConfigStore implementation.
            updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
            source_file=source_file,
        )

    async def delete(self, entity_type: str, name: str) -> None:
        self._entities.get(entity_type, {}).pop(name, None)

    async def list(self, entity_type: str) -> list[ConfigEntity]:
        return [self._copy_entity(e) for e in self._entities.get(entity_type, {}).values()]

    @staticmethod
    def _copy_entity(entity: ConfigEntity) -> ConfigEntity:
        return ConfigEntity(
            name=entity.name,
            data=copy.deepcopy(entity.data),
            static=entity.static,
            mutable=entity.mutable,
            updated_at=entity.updated_at,
            source_file=entity.source_file,
        )
