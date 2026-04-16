"""DynamicConfigStore — runtime config mutations backed by persistent storage.

Stores target configs as JSON blobs in the Backend KV store and publishes
change notifications over pub/sub so all instances hot-reload in sync.

Key schema
----------
ph:dynamic:target:{name}     — KV  — JSON-serialised TargetConfig dict
ph:config:changes            — pub/sub channel — JSON-serialised ConfigChangeEvent

Design rules
------------
- Only targets marked ``mutable: true`` in the static YAML may be *updated*
  (not deleted) via this store.  Fully-dynamic targets (created through this
  store at runtime) may be updated or deleted freely.
- Static targets with ``mutable: false`` are never touched by this store.
- The store has no knowledge of policy (quarantine thresholds, etc.) — that
  lives in IPPool.  IP-list mutations go through IPPoolStore.

HA / multi-instance safety
--------------------------
All writes are serialised through the Backend (Redis SET is atomic for string
values).  After each write a pub/sub message is published so other instances
pick up the change.  Subscribers call ``get_target`` to fetch the authoritative
value from the store rather than trusting the event payload.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal, Optional

from .backend.base import Backend
from .config import (
    IdentityConfig,
    ResolvedIP,
    TargetConfig,
    WarmupConfig,
    _parse_address,
    _parse_duration,
)

logger = logging.getLogger(__name__)

_KV_PREFIX = "ph:dynamic:target:"
_CHANGES_CHANNEL = "ph:config:changes"


# ---------------------------------------------------------------------------
# Change event
# ---------------------------------------------------------------------------

@dataclass
class ConfigChangeEvent:
    """Published whenever a target is added, updated, or removed."""
    type: Literal["add", "update", "remove"]
    name: str
    #: Serialised TargetConfig dict — present for add/update, None for remove.
    data: Optional[dict] = field(default=None)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _target_to_dict(config: TargetConfig) -> dict:
    """Serialise a TargetConfig to a plain dict suitable for JSON storage."""
    return config.model_dump(mode="json")


def _dict_to_target(raw: dict) -> TargetConfig:
    """Deserialise a stored dict back into a TargetConfig."""
    # resolved_ips are stored as dicts — reconstruct ResolvedIP objects
    if "resolved_ips" in raw and raw["resolved_ips"]:
        raw["resolved_ips"] = [
            ResolvedIP(**ip) if isinstance(ip, dict) else ip
            for ip in raw["resolved_ips"]
        ]
    if "identity" in raw and isinstance(raw["identity"], dict):
        id_raw = raw["identity"]
        if "warmup" in id_raw and isinstance(id_raw["warmup"], dict):
            id_raw["warmup"] = WarmupConfig(**id_raw["warmup"])
        raw["identity"] = IdentityConfig(**id_raw)
    return TargetConfig(**raw)


def _build_target(
    name: str,
    regex: str,
    ip_list: list[str],
    *,
    default_proxy_port: int = 8080,
    min_request_interval: float = 1.0,
    max_queue_wait: float = 30.0,
    num_retries: int = 3,
    ip_failures_until_quarantine: int = 5,
    quarantine_time: float = 120.0,
    identity: Optional[IdentityConfig] = None,
    mutable: bool = True,
) -> TargetConfig:
    """Construct a TargetConfig from raw user-supplied fields."""
    resolved = []
    for entry in ip_list:
        host, port = _parse_address(entry, default_proxy_port)
        resolved.append(ResolvedIP(host=host, port=port))
    return TargetConfig(
        name=name,
        regex=regex,
        resolved_ips=resolved,
        min_request_interval=min_request_interval,
        max_queue_wait=max_queue_wait,
        num_retries=num_retries,
        ip_failures_until_quarantine=ip_failures_until_quarantine,
        quarantine_time=quarantine_time,
        default_proxy_port=default_proxy_port,
        identity=identity or IdentityConfig(),
        mutable=mutable,
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class DynamicConfigStore:
    """Runtime target config mutations — wraps Backend KV + pub/sub."""

    def __init__(self, backend: Backend) -> None:
        self._backend = backend

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def add_target(self, config: TargetConfig) -> None:
        """Persist a new dynamic target and notify all instances.

        Raises ValueError if a target with this name already exists.
        """
        existing = await self._backend.kv_get(f"{_KV_PREFIX}{config.name}")
        if existing is not None:
            raise ValueError(
                f"Target '{config.name}' already exists in the dynamic store. "
                "Use update_target to modify it."
            )
        await self._backend.kv_set(
            f"{_KV_PREFIX}{config.name}",
            json.dumps(_target_to_dict(config)),
        )
        await self._publish(ConfigChangeEvent(type="add", name=config.name, data=_target_to_dict(config)))
        logger.info("DynamicConfigStore: target '%s' added", config.name)

    async def update_target(self, config: TargetConfig) -> None:
        """Update an existing dynamic target and notify all instances.

        For static targets marked ``mutable: true`` this replaces the stored
        override.  Raises ValueError if the target does not exist or is not
        mutable.
        """
        existing_raw = await self._backend.kv_get(f"{_KV_PREFIX}{config.name}")
        if existing_raw is None:
            raise ValueError(
                f"Target '{config.name}' does not exist in the dynamic store. "
                "Use add_target to create it."
            )
        existing_config = _dict_to_target(json.loads(existing_raw))
        if not existing_config.mutable:
            raise ValueError(
                f"Target '{config.name}' is not mutable. "
                "Set mutable: true in its configuration to allow runtime updates."
            )
        await self._backend.kv_set(
            f"{_KV_PREFIX}{config.name}",
            json.dumps(_target_to_dict(config)),
        )
        await self._publish(ConfigChangeEvent(type="update", name=config.name, data=_target_to_dict(config)))
        logger.info("DynamicConfigStore: target '%s' updated", config.name)

    async def remove_target(self, name: str) -> None:
        """Remove a dynamic target and notify all instances.

        No-op if the target does not exist in the dynamic store.
        """
        await self._backend.kv_delete(f"{_KV_PREFIX}{name}")
        await self._publish(ConfigChangeEvent(type="remove", name=name))
        logger.info("DynamicConfigStore: target '%s' removed", name)

    async def get_target(self, name: str) -> Optional[TargetConfig]:
        """Return the stored config for *name*, or None if not present."""
        raw = await self._backend.kv_get(f"{_KV_PREFIX}{name}")
        if raw is None:
            return None
        return _dict_to_target(json.loads(raw))

    async def list_targets(self) -> list[TargetConfig]:
        """Return all dynamically-stored targets."""
        pairs = await self._backend.kv_list(_KV_PREFIX)
        configs = []
        for key, raw in pairs:
            try:
                configs.append(_dict_to_target(json.loads(raw)))
            except (json.JSONDecodeError, TypeError, KeyError) as exc:
                logger.error(
                    "DynamicConfigStore: failed to deserialise target at key '%s': %s",
                    key, exc,
                )
            except Exception as exc:
                logger.error(
                    "DynamicConfigStore: unexpected error loading target at key '%s': %s",
                    key, exc,
                )
        return configs

    # ------------------------------------------------------------------
    # IP list helpers
    # ------------------------------------------------------------------

    async def add_ip(self, name: str, address: str) -> None:
        """Add *address* to a dynamic target's IP list and re-persist.

        Does not push the IP into the live pool — call IPPoolStore.push_ip
        separately if the target's pool is already running.
        """
        config = await self._get_or_raise(name)
        host, port = _parse_address(address, config.default_proxy_port)
        new_ip = ResolvedIP(host=host, port=port)
        updated = config.model_copy(update={"resolved_ips": config.resolved_ips + [new_ip]})
        await self.update_target(updated)

    async def remove_ip(self, name: str, address: str) -> None:
        """Remove *address* from a dynamic target's IP list and re-persist."""
        config = await self._get_or_raise(name)
        remaining = [ip for ip in config.resolved_ips if ip.address != address]
        if not remaining:
            raise ValueError(
                f"Cannot remove '{address}' from target '{name}': "
                "the target must have at least one IP."
            )
        updated = config.model_copy(update={"resolved_ips": remaining})
        await self.update_target(updated)

    async def swap_ip(self, name: str, old_address: str, new_address: str) -> None:
        """Replace *old_address* with *new_address* in a dynamic target's IP list.

        The new IP is added to the stored config.  The old IP is removed from
        the stored config but NOT removed from the live pool — it will flow
        through naturally (cooldown, quarantine, or eventual pool drain).
        """
        config = await self._get_or_raise(name)
        host, port = _parse_address(new_address, config.default_proxy_port)
        new_ip = ResolvedIP(host=host, port=port)
        new_ips = [
            (new_ip if ip.address == old_address else ip)
            for ip in config.resolved_ips
        ]
        if new_ips == config.resolved_ips:
            raise ValueError(
                f"Address '{old_address}' not found in target '{name}'."
            )
        updated = config.model_copy(update={"resolved_ips": new_ips})
        await self.update_target(updated)

    # ------------------------------------------------------------------
    # Pub/sub change subscription
    # ------------------------------------------------------------------

    def subscribe_changes(self):
        """Async context manager yielding ``ConfigChangeEvent`` objects.

        Usage::

            async with store.subscribe_changes() as events:
                async for event in events:
                    handle(event)
        """
        return _ChangeSubscription(self._backend)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_or_raise(self, name: str) -> TargetConfig:
        config = await self.get_target(name)
        if config is None:
            raise ValueError(f"Target '{name}' not found in the dynamic store.")
        return config

    async def _publish(self, event: ConfigChangeEvent) -> None:
        payload = json.dumps(
            {"type": event.type, "name": event.name, "data": event.data}
        )
        await self._backend.publish(_CHANGES_CHANNEL, payload)


# ---------------------------------------------------------------------------
# Change subscription context manager
# ---------------------------------------------------------------------------

class _ChangeSubscription:
    """Wraps Backend.subscribe to yield typed ConfigChangeEvent objects."""

    def __init__(self, backend: Backend) -> None:
        self._backend = backend
        self._ctx = None

    async def __aenter__(self) -> AsyncIterator[ConfigChangeEvent]:
        self._ctx = self._backend.subscribe(_CHANGES_CHANNEL)
        messages: AsyncIterator[str] = await self._ctx.__aenter__()

        async def _iter() -> AsyncIterator[ConfigChangeEvent]:
            async for msg in messages:
                try:
                    raw = json.loads(msg)
                    yield ConfigChangeEvent(
                        type=raw["type"],
                        name=raw["name"],
                        data=raw.get("data"),
                    )
                except Exception as exc:
                    logger.warning(
                        "DynamicConfigStore: failed to parse change event: %s", exc
                    )

        return _iter()

    async def __aexit__(self, *args) -> None:
        if self._ctx is not None:
            await self._ctx.__aexit__(*args)
