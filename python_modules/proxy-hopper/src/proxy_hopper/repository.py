"""ProxyRepository — runtime entity store backed by persistent KV + pub/sub.

Stores targets and providers as JSON blobs in the Backend KV store and
publishes change notifications over pub/sub so all instances hot-reload.

Key schema
----------
ph:repo:target:{name}    — KV — JSON-serialised TargetConfig
ph:repo:provider:{name}  — KV — JSON-serialised ProxyProvider
ph:repo:changes          — pub/sub channel — JSON-serialised ChangeEvent

Design rules
------------
- Targets and providers are domain entities.  The YAML config file is seed
  data used only for first-run bootstrapping; ``ProxyRepository`` is the
  source of truth at runtime.
- ``seed_target`` / ``seed_provider`` are write-if-not-exists helpers intended
  for startup.  They publish no events and silently skip already-stored
  entities.
- ``update_target`` / ``update_provider`` honour the ``mutable`` flag.  Set
  ``mutable: false`` explicitly to lock an entity against runtime changes.
  The default is ``mutable: true``.
- Provider cascade: ``update_provider`` and ``add_provider`` call
  ``_cascade_provider`` which rebuilds the ``resolved_ips`` for every target
  that references the provider, emitting ``target:update`` events for each.

HA / multi-instance safety
--------------------------
All writes are serialised through the Backend (Redis SET is atomic for string
values).  After each write a pub/sub message is published so other instances
pick up the change.  Subscribers call ``get_target`` / ``get_provider`` to
fetch the authoritative value from the KV store rather than trusting the
event payload.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal, Optional

from .backend.base import Backend
from .config import (
    IdentityConfig,
    ProxyProvider,
    ResolvedIP,
    TargetConfig,
    WarmupConfig,
    _parse_address,
    _parse_duration,
)

logger = logging.getLogger(__name__)

_TARGET_PREFIX   = "ph:repo:target:"
_PROVIDER_PREFIX = "ph:repo:provider:"
_CHANGES_CHANNEL = "ph:repo:changes"


# ---------------------------------------------------------------------------
# Change event
# ---------------------------------------------------------------------------

@dataclass
class ChangeEvent:
    """Published whenever a target or provider is added, updated, or removed."""
    entity: Literal["target", "provider"]
    type: Literal["add", "update", "remove"]
    name: str
    #: Serialised entity dict — present for add/update, None for remove.
    data: Optional[dict] = field(default=None)


# ---------------------------------------------------------------------------
# Serialisation helpers — targets
# ---------------------------------------------------------------------------

def _target_to_dict(config: TargetConfig) -> dict:
    """Serialise a TargetConfig to a plain dict suitable for JSON storage."""
    return config.model_dump(mode="json")


def _dict_to_target(raw: dict) -> TargetConfig:
    """Deserialise a stored dict back into a TargetConfig."""
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
# Serialisation helpers — providers
# ---------------------------------------------------------------------------

def _provider_to_dict(provider: ProxyProvider) -> dict:
    """Serialise a ProxyProvider to a plain dict suitable for JSON storage."""
    return provider.model_dump(mode="json")


def _dict_to_provider(raw: dict) -> ProxyProvider:
    """Deserialise a stored dict back into a ProxyProvider."""
    return ProxyProvider(**raw)


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

class ProxyRepository:
    """Runtime entity store — wraps Backend KV + pub/sub.

    Targets and providers are the two first-class stored entity types.
    IP-pool state (queue, failures, quarantine) lives in IPPoolStore.
    """

    def __init__(self, backend: Backend) -> None:
        self._backend = backend

    # ------------------------------------------------------------------
    # Target CRUD
    # ------------------------------------------------------------------

    async def add_target(self, config: TargetConfig) -> None:
        """Persist a new target and notify all instances.

        Raises ValueError if a target with this name already exists.
        """
        existing = await self._backend.kv_get(f"{_TARGET_PREFIX}{config.name}")
        if existing is not None:
            raise ValueError(
                f"Target '{config.name}' already exists in the repository. "
                "Use update_target to modify it."
            )
        await self._backend.kv_set(
            f"{_TARGET_PREFIX}{config.name}",
            json.dumps(_target_to_dict(config)),
        )
        await self._publish(ChangeEvent(entity="target", type="add", name=config.name, data=_target_to_dict(config)))
        logger.info("ProxyRepository: target '%s' added", config.name)

    async def update_target(self, config: TargetConfig) -> None:
        """Update an existing target and notify all instances.

        Raises ValueError if the target does not exist or is not mutable.
        """
        existing_raw = await self._backend.kv_get(f"{_TARGET_PREFIX}{config.name}")
        if existing_raw is None:
            raise ValueError(
                f"Target '{config.name}' does not exist in the repository. "
                "Use add_target to create it."
            )
        existing = _dict_to_target(json.loads(existing_raw))
        if not existing.mutable:
            raise ValueError(
                f"Target '{config.name}' is not mutable. "
                "Set mutable: true in its configuration to allow runtime updates."
            )
        await self._backend.kv_set(
            f"{_TARGET_PREFIX}{config.name}",
            json.dumps(_target_to_dict(config)),
        )
        await self._publish(ChangeEvent(entity="target", type="update", name=config.name, data=_target_to_dict(config)))
        logger.info("ProxyRepository: target '%s' updated", config.name)

    async def remove_target(self, name: str) -> None:
        """Remove a target and notify all instances.

        No-op if the target does not exist in the repository.
        """
        await self._backend.kv_delete(f"{_TARGET_PREFIX}{name}")
        await self._publish(ChangeEvent(entity="target", type="remove", name=name))
        logger.info("ProxyRepository: target '%s' removed", name)

    async def get_target(self, name: str) -> Optional[TargetConfig]:
        """Return the stored config for *name*, or None if not present."""
        raw = await self._backend.kv_get(f"{_TARGET_PREFIX}{name}")
        if raw is None:
            return None
        return _dict_to_target(json.loads(raw))

    async def list_targets(self) -> list[TargetConfig]:
        """Return all stored targets."""
        pairs = await self._backend.kv_list(_TARGET_PREFIX)
        configs = []
        for key, raw in pairs:
            try:
                configs.append(_dict_to_target(json.loads(raw)))
            except (json.JSONDecodeError, TypeError, KeyError) as exc:
                logger.error(
                    "ProxyRepository: failed to deserialise target at key '%s': %s",
                    key, exc,
                )
            except Exception as exc:
                logger.error(
                    "ProxyRepository: unexpected error loading target at key '%s': %s",
                    key, exc,
                )
        return configs

    # ------------------------------------------------------------------
    # Provider CRUD
    # ------------------------------------------------------------------

    async def add_provider(self, provider: ProxyProvider) -> None:
        """Persist a new provider, cascade its IPs to targets, and notify.

        Raises ValueError if a provider with this name already exists.
        """
        existing = await self._backend.kv_get(f"{_PROVIDER_PREFIX}{provider.name}")
        if existing is not None:
            raise ValueError(
                f"Provider '{provider.name}' already exists in the repository. "
                "Use update_provider to modify it."
            )
        await self._backend.kv_set(
            f"{_PROVIDER_PREFIX}{provider.name}",
            json.dumps(_provider_to_dict(provider)),
        )
        await self._publish(ChangeEvent(entity="provider", type="add", name=provider.name, data=_provider_to_dict(provider)))
        logger.info("ProxyRepository: provider '%s' added", provider.name)
        await self._cascade_provider(provider)

    async def update_provider(self, provider: ProxyProvider) -> None:
        """Update an existing provider, cascade its IPs to targets, and notify.

        Raises ValueError if the provider does not exist or is not mutable.
        """
        existing_raw = await self._backend.kv_get(f"{_PROVIDER_PREFIX}{provider.name}")
        if existing_raw is None:
            raise ValueError(
                f"Provider '{provider.name}' does not exist in the repository. "
                "Use add_provider to create it."
            )
        existing = _dict_to_provider(json.loads(existing_raw))
        if not existing.mutable:
            raise ValueError(
                f"Provider '{provider.name}' is not mutable. "
                "Set mutable: true in its configuration to allow runtime updates."
            )
        await self._backend.kv_set(
            f"{_PROVIDER_PREFIX}{provider.name}",
            json.dumps(_provider_to_dict(provider)),
        )
        await self._publish(ChangeEvent(entity="provider", type="update", name=provider.name, data=_provider_to_dict(provider)))
        logger.info("ProxyRepository: provider '%s' updated", provider.name)
        await self._cascade_provider(provider)

    async def remove_provider(self, name: str) -> None:
        """Remove a provider and notify all instances.

        No-op if the provider does not exist in the repository.
        Does not remove IPs from any target's resolved_ips — those remain
        until the target is updated explicitly.
        """
        await self._backend.kv_delete(f"{_PROVIDER_PREFIX}{name}")
        await self._publish(ChangeEvent(entity="provider", type="remove", name=name))
        logger.info("ProxyRepository: provider '%s' removed", name)

    async def get_provider(self, name: str) -> Optional[ProxyProvider]:
        """Return the stored config for *name*, or None if not present."""
        raw = await self._backend.kv_get(f"{_PROVIDER_PREFIX}{name}")
        if raw is None:
            return None
        return _dict_to_provider(json.loads(raw))

    async def list_providers(self) -> list[ProxyProvider]:
        """Return all stored providers."""
        pairs = await self._backend.kv_list(_PROVIDER_PREFIX)
        providers = []
        for key, raw in pairs:
            try:
                providers.append(_dict_to_provider(json.loads(raw)))
            except (json.JSONDecodeError, TypeError, KeyError) as exc:
                logger.error(
                    "ProxyRepository: failed to deserialise provider at key '%s': %s",
                    key, exc,
                )
            except Exception as exc:
                logger.error(
                    "ProxyRepository: unexpected error loading provider at key '%s': %s",
                    key, exc,
                )
        return providers

    # ------------------------------------------------------------------
    # Target IP list helpers
    # ------------------------------------------------------------------

    async def add_ip(self, name: str, address: str) -> None:
        """Add *address* to a target's IP list and re-persist.

        Does not push the IP into the live pool — call IPPoolStore.push_ip
        separately if the target's pool is already running.
        """
        config = await self._get_or_raise_target(name)
        host, port = _parse_address(address, config.default_proxy_port)
        new_ip = ResolvedIP(host=host, port=port)
        updated = config.model_copy(update={"resolved_ips": config.resolved_ips + [new_ip]})
        await self.update_target(updated)

    async def remove_ip(self, name: str, address: str) -> None:
        """Remove *address* from a target's IP list and re-persist."""
        config = await self._get_or_raise_target(name)
        remaining = [ip for ip in config.resolved_ips if ip.address != address]
        if not remaining:
            raise ValueError(
                f"Cannot remove '{address}' from target '{name}': "
                "the target must have at least one IP."
            )
        updated = config.model_copy(update={"resolved_ips": remaining})
        await self.update_target(updated)

    async def swap_ip(self, name: str, old_address: str, new_address: str) -> None:
        """Replace *old_address* with *new_address* in a target's IP list.

        The new IP is added to the stored config.  The old IP is removed from
        the stored config but NOT removed from the live pool — it will flow
        through naturally (cooldown, quarantine, or eventual pool drain).
        """
        config = await self._get_or_raise_target(name)
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
    # Startup seeding (write-if-not-exists, no pub/sub)
    # ------------------------------------------------------------------

    async def seed_target(self, config: TargetConfig) -> None:
        """Persist *config* only if no target with this name is already stored.

        Used during startup to bootstrap the repository from YAML without
        overwriting runtime mutations.  Publishes no events.
        """
        existing = await self._backend.kv_get(f"{_TARGET_PREFIX}{config.name}")
        if existing is None:
            await self._backend.kv_set(
                f"{_TARGET_PREFIX}{config.name}",
                json.dumps(_target_to_dict(config)),
            )
            logger.debug("ProxyRepository: seeded target '%s'", config.name)

    async def seed_provider(self, provider: ProxyProvider) -> None:
        """Persist *provider* only if no provider with this name is already stored.

        Used during startup to bootstrap the repository from YAML without
        overwriting runtime mutations.  Publishes no events.
        """
        existing = await self._backend.kv_get(f"{_PROVIDER_PREFIX}{provider.name}")
        if existing is None:
            await self._backend.kv_set(
                f"{_PROVIDER_PREFIX}{provider.name}",
                json.dumps(_provider_to_dict(provider)),
            )
            logger.debug("ProxyRepository: seeded provider '%s'", provider.name)

    # ------------------------------------------------------------------
    # Pub/sub change subscription
    # ------------------------------------------------------------------

    def subscribe_changes(self):
        """Async context manager yielding ``ChangeEvent`` objects.

        Usage::

            async with repo.subscribe_changes() as events:
                async for event in events:
                    handle(event)
        """
        return _ChangeSubscription(self._backend)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_or_raise_target(self, name: str) -> TargetConfig:
        config = await self.get_target(name)
        if config is None:
            raise ValueError(f"Target '{name}' not found in the repository.")
        return config

    async def _get_or_raise_provider(self, name: str) -> ProxyProvider:
        provider = await self.get_provider(name)
        if provider is None:
            raise ValueError(f"Provider '{name}' not found in the repository.")
        return provider

    async def _cascade_provider(self, provider: ProxyProvider) -> None:
        """Rebuild resolved_ips for every target that references *provider*.

        Emits ``target:update`` events for each affected target so all
        instances (including this one) can hot-reload the impacted pools.
        """
        targets = await self.list_targets()
        for target in targets:
            if not any(ip.provider == provider.name for ip in target.resolved_ips):
                continue
            non_provider = [ip for ip in target.resolved_ips if ip.provider != provider.name]
            new_ips = [
                ResolvedIP(host=h, port=p, provider=provider.name)
                for h, p in provider.resolved_ip_list(target.default_proxy_port)
            ]
            updated = target.model_copy(update={"resolved_ips": non_provider + new_ips})
            # Bypass update_target mutability check — this is an internal cascade.
            await self._backend.kv_set(
                f"{_TARGET_PREFIX}{target.name}",
                json.dumps(_target_to_dict(updated)),
            )
            await self._publish(ChangeEvent(
                entity="target", type="update",
                name=target.name, data=_target_to_dict(updated),
            ))
            logger.info(
                "ProxyRepository: cascaded provider '%s' IP update to target '%s'",
                provider.name, target.name,
            )

    async def _publish(self, event: ChangeEvent) -> None:
        payload = json.dumps({
            "entity": event.entity,
            "type": event.type,
            "name": event.name,
            "data": event.data,
        })
        await self._backend.publish(_CHANGES_CHANNEL, payload)


# ---------------------------------------------------------------------------
# Change subscription context manager
# ---------------------------------------------------------------------------

class _ChangeSubscription:
    """Wraps Backend.subscribe to yield typed ChangeEvent objects."""

    def __init__(self, backend: Backend) -> None:
        self._backend = backend
        self._ctx = None

    async def __aenter__(self) -> AsyncIterator[ChangeEvent]:
        self._ctx = self._backend.subscribe(_CHANGES_CHANNEL)
        messages: AsyncIterator[str] = await self._ctx.__aenter__()

        async def _iter() -> AsyncIterator[ChangeEvent]:
            async for msg in messages:
                try:
                    raw = json.loads(msg)
                    entity = raw.get("entity")
                    if entity not in ("target", "provider"):
                        logger.warning(
                            "ProxyRepository: change event with unknown entity %r — skipping",
                            entity,
                        )
                        continue
                    yield ChangeEvent(
                        entity=entity,
                        type=raw["type"],
                        name=raw["name"],
                        data=raw.get("data"),
                    )
                except Exception as exc:
                    logger.warning(
                        "ProxyRepository: failed to parse change event: %s", exc
                    )

        return _iter()

    async def __aexit__(self, *args) -> None:
        if self._ctx is not None:
            await self._ctx.__aexit__(*args)
