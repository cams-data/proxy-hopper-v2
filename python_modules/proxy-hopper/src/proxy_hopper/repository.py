"""ProxyRepository — runtime entity store backed by persistent KV + pub/sub.

Stores targets, providers, and IP pools as JSON blobs in the Backend KV store
and publishes change notifications over pub/sub so all instances hot-reload.

Key schema
----------
ph:repo:target:{name}    — KV — JSON-serialised TargetConfig
ph:repo:provider:{name}  — KV — JSON-serialised ProxyProvider
ph:repo:pool:{name}      — KV — JSON-serialised IpPool
ph:repo:changes          — pub/sub channel — JSON-serialised ChangeEvent

Three-tier model
----------------
- ProxyProvider: credentials + ip_list — the ONLY place IPs are declared.
- IpPool: references providers via ip_requests with count — resolved to
  resolved_ips snapshots on targets.  Multiple targets may share a pool.
- Target: routing regex + rate-limit policy + pool_name reference.  Carries a
  resolved_ips snapshot populated by the pool cascade.

Design rules
------------
- Targets, providers, and pools are domain entities.  The YAML config file is
  seed data used only for first-run bootstrapping; ProxyRepository is the
  source of truth at runtime.
- seed_* helpers are write-if-not-exists used at startup; they publish no events.
- update_provider / add_provider call _cascade_provider which recomputes the
  resolved_ips snapshots for every pool referencing that provider, then cascades
  to targets, emitting target:update events for each.
- IP additions to a provider flow: provider → pools → targets (resolved_ips
  snapshot) → target:update events → ProxyServer diffs and pushes new IPs to
  live pool queues.

HA / multi-instance safety
--------------------------
All writes are serialised through the Backend.  After each write a pub/sub
message is published so other instances pick up the change.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal, Optional

from .backend.base import Backend
from .config import (
    IdentityConfig,
    IpPool,
    IpRequest,
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
_POOL_PREFIX     = "ph:repo:pool:"
_CHANGES_CHANNEL = "ph:repo:changes"


# ---------------------------------------------------------------------------
# Change event
# ---------------------------------------------------------------------------

@dataclass
class ChangeEvent:
    """Published whenever a target, provider, or pool is added, updated, or removed."""
    entity: Literal["target", "provider", "pool"]
    type: Literal["add", "update", "remove"]
    name: str
    #: Serialised entity dict — present for add/update, None for remove.
    data: Optional[dict] = field(default=None)


# ---------------------------------------------------------------------------
# Serialisation helpers — targets
# ---------------------------------------------------------------------------

def _target_to_dict(config: TargetConfig) -> dict:
    return config.model_dump(mode="json")


def _dict_to_target(raw: dict) -> TargetConfig:
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


# ---------------------------------------------------------------------------
# Serialisation helpers — providers
# ---------------------------------------------------------------------------

def _provider_to_dict(provider: ProxyProvider) -> dict:
    return provider.model_dump(mode="json")


def _dict_to_provider(raw: dict) -> ProxyProvider:
    return ProxyProvider(**raw)


# ---------------------------------------------------------------------------
# Serialisation helpers — pools
# ---------------------------------------------------------------------------

def _pool_to_dict(pool: IpPool) -> dict:
    return pool.model_dump(mode="json")


def _dict_to_pool(raw: dict) -> IpPool:
    if "ip_requests" in raw and raw["ip_requests"]:
        raw["ip_requests"] = [
            IpRequest(**req) if isinstance(req, dict) else req
            for req in raw["ip_requests"]
        ]
    return IpPool(**raw)


# ---------------------------------------------------------------------------
# Pool IP resolution helper
# ---------------------------------------------------------------------------

def _resolve_pool_ips(
    pool: IpPool,
    provider_map: dict[str, ProxyProvider],
    default_port: int = 8080,
) -> list[ResolvedIP]:
    """Compute the current resolved_ips snapshot for a pool.

    Uses deterministic first-N selection.  Providers missing from provider_map
    are silently skipped (provider may have been removed).
    """
    resolved: list[ResolvedIP] = []
    for req in pool.ip_requests:
        provider = provider_map.get(req.provider)
        if provider is None:
            logger.warning(
                "Pool '%s' references provider '%s' which is not in the repository — skipping",
                pool.name, req.provider,
            )
            continue
        available = provider.resolved_ip_list(default_port)
        for host, port in available[: req.count]:
            resolved.append(ResolvedIP(
                host=host,
                port=port,
                provider=provider.name,
                region_tag=provider.region_tag or "",
            ))
    return resolved


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

class ProxyRepository:
    """Runtime entity store — wraps Backend KV + pub/sub.

    Three first-class stored entity types: targets, providers, and IP pools.
    IP-pool runtime state (queue, failures, quarantine) lives in IPPoolStore.
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
        if existing.static:
            raise ValueError(
                f"Target '{config.name}' is config-static and cannot be updated via the API. "
                "Edit the YAML configuration file instead."
            )
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
        """Remove a target and notify all instances."""
        existing_raw = await self._backend.kv_get(f"{_TARGET_PREFIX}{name}")
        if existing_raw is not None:
            existing = _dict_to_target(json.loads(existing_raw))
            if existing.static:
                raise ValueError(
                    f"Target '{name}' is config-static and cannot be removed via the API. "
                    "Remove it from the YAML configuration file instead."
                )
        await self._backend.kv_delete(f"{_TARGET_PREFIX}{name}")
        await self._publish(ChangeEvent(entity="target", type="remove", name=name))
        logger.info("ProxyRepository: target '%s' removed", name)

    async def get_target(self, name: str) -> Optional[TargetConfig]:
        raw = await self._backend.kv_get(f"{_TARGET_PREFIX}{name}")
        if raw is None:
            return None
        return _dict_to_target(json.loads(raw))

    async def list_targets(self) -> list[TargetConfig]:
        pairs = await self._backend.kv_list(_TARGET_PREFIX)
        configs = []
        for key, raw in pairs:
            try:
                configs.append(_dict_to_target(json.loads(raw)))
            except Exception as exc:
                logger.error("ProxyRepository: failed to deserialise target at key '%s': %s", key, exc)
        return configs

    # ------------------------------------------------------------------
    # Provider CRUD
    # ------------------------------------------------------------------

    async def add_provider(self, provider: ProxyProvider) -> None:
        """Persist a new provider, cascade IPs through pools to targets, and notify."""
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
        """Update an existing provider, cascade IPs through pools to targets, and notify."""
        existing_raw = await self._backend.kv_get(f"{_PROVIDER_PREFIX}{provider.name}")
        if existing_raw is None:
            raise ValueError(
                f"Provider '{provider.name}' does not exist in the repository. "
                "Use add_provider to create it."
            )
        existing = _dict_to_provider(json.loads(existing_raw))
        if existing.static:
            raise ValueError(
                f"Provider '{provider.name}' is config-static and cannot be updated via the API. "
                "Edit the YAML configuration file instead."
            )
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
        """Remove a provider and notify.  Does not remove IPs from pools/targets."""
        existing_raw = await self._backend.kv_get(f"{_PROVIDER_PREFIX}{name}")
        if existing_raw is not None:
            existing = _dict_to_provider(json.loads(existing_raw))
            if existing.static:
                raise ValueError(
                    f"Provider '{name}' is config-static and cannot be removed via the API. "
                    "Remove it from the YAML configuration file instead."
                )
        await self._backend.kv_delete(f"{_PROVIDER_PREFIX}{name}")
        await self._publish(ChangeEvent(entity="provider", type="remove", name=name))
        logger.info("ProxyRepository: provider '%s' removed", name)

    async def get_provider(self, name: str) -> Optional[ProxyProvider]:
        raw = await self._backend.kv_get(f"{_PROVIDER_PREFIX}{name}")
        if raw is None:
            return None
        return _dict_to_provider(json.loads(raw))

    async def list_providers(self) -> list[ProxyProvider]:
        pairs = await self._backend.kv_list(_PROVIDER_PREFIX)
        providers = []
        for key, raw in pairs:
            try:
                providers.append(_dict_to_provider(json.loads(raw)))
            except Exception as exc:
                logger.error("ProxyRepository: failed to deserialise provider at key '%s': %s", key, exc)
        return providers

    # ------------------------------------------------------------------
    # Provider IP helpers
    # ------------------------------------------------------------------

    async def add_ip_to_provider(self, provider_name: str, address: str) -> ProxyProvider:
        """Append *address* to a provider's ip_list and cascade to pools/targets."""
        provider = await self._get_or_raise_provider(provider_name)
        if address in provider.ip_list:
            raise ValueError(f"Address '{address}' already in provider '{provider_name}'.")
        updated = provider.model_copy(update={"ip_list": provider.ip_list + [address]})
        await self.update_provider(updated)
        return updated

    async def remove_ip_from_provider(self, provider_name: str, address: str) -> ProxyProvider:
        """Remove *address* from a provider's ip_list and cascade to pools/targets."""
        provider = await self._get_or_raise_provider(provider_name)
        if address not in provider.ip_list:
            raise ValueError(f"Address '{address}' not found in provider '{provider_name}'.")
        remaining = [ip for ip in provider.ip_list if ip != address]
        if not remaining:
            raise ValueError(
                f"Cannot remove '{address}' from provider '{provider_name}': "
                "the provider must have at least one IP."
            )
        updated = provider.model_copy(update={"ip_list": remaining})
        await self.update_provider(updated)
        return updated

    # ------------------------------------------------------------------
    # Pool CRUD
    # ------------------------------------------------------------------

    async def add_pool(self, pool: IpPool) -> None:
        """Persist a new pool and notify all instances.

        Raises ValueError if a pool with this name already exists.
        """
        existing = await self._backend.kv_get(f"{_POOL_PREFIX}{pool.name}")
        if existing is not None:
            raise ValueError(
                f"Pool '{pool.name}' already exists in the repository. "
                "Use update_pool to modify it."
            )
        await self._backend.kv_set(
            f"{_POOL_PREFIX}{pool.name}",
            json.dumps(_pool_to_dict(pool)),
        )
        await self._publish(ChangeEvent(entity="pool", type="add", name=pool.name, data=_pool_to_dict(pool)))
        logger.info("ProxyRepository: pool '%s' added", pool.name)

    async def update_pool(self, pool: IpPool) -> None:
        """Update an existing pool, cascade resolved IPs to targets, and notify.

        Raises ValueError if the pool does not exist or is not mutable.
        """
        existing_raw = await self._backend.kv_get(f"{_POOL_PREFIX}{pool.name}")
        if existing_raw is None:
            raise ValueError(
                f"Pool '{pool.name}' does not exist in the repository. "
                "Use add_pool to create it."
            )
        existing = _dict_to_pool(json.loads(existing_raw))
        if existing.static:
            raise ValueError(
                f"Pool '{pool.name}' is config-static and cannot be updated via the API. "
                "Edit the YAML configuration file instead."
            )
        if not existing.mutable:
            raise ValueError(
                f"Pool '{pool.name}' is not mutable. "
                "Set mutable: true in its configuration to allow runtime updates."
            )
        await self._backend.kv_set(
            f"{_POOL_PREFIX}{pool.name}",
            json.dumps(_pool_to_dict(pool)),
        )
        await self._publish(ChangeEvent(entity="pool", type="update", name=pool.name, data=_pool_to_dict(pool)))
        logger.info("ProxyRepository: pool '%s' updated", pool.name)
        await self._cascade_pool(pool)

    async def remove_pool(self, name: str) -> None:
        """Remove a pool and notify all instances."""
        existing_raw = await self._backend.kv_get(f"{_POOL_PREFIX}{name}")
        if existing_raw is not None:
            existing = _dict_to_pool(json.loads(existing_raw))
            if existing.static:
                raise ValueError(
                    f"Pool '{name}' is config-static and cannot be removed via the API. "
                    "Remove it from the YAML configuration file instead."
                )
        await self._backend.kv_delete(f"{_POOL_PREFIX}{name}")
        await self._publish(ChangeEvent(entity="pool", type="remove", name=name))
        logger.info("ProxyRepository: pool '%s' removed", name)

    async def get_pool(self, name: str) -> Optional[IpPool]:
        raw = await self._backend.kv_get(f"{_POOL_PREFIX}{name}")
        if raw is None:
            return None
        return _dict_to_pool(json.loads(raw))

    async def list_pools(self) -> list[IpPool]:
        pairs = await self._backend.kv_list(_POOL_PREFIX)
        pools = []
        for key, raw in pairs:
            try:
                pools.append(_dict_to_pool(json.loads(raw)))
            except Exception as exc:
                logger.error("ProxyRepository: failed to deserialise pool at key '%s': %s", key, exc)
        return pools

    # ------------------------------------------------------------------
    # Startup seeding (write-if-not-exists, no pub/sub)
    # ------------------------------------------------------------------

    async def seed_target(self, config: TargetConfig) -> None:
        """Persist *config* from YAML.

        Managed entities (static=True, the default for YAML-defined targets) are
        always overwritten so that config-file changes take effect on restart.
        Unstatic entities are written only if no entry already exists.
        """
        existing = await self._backend.kv_get(f"{_TARGET_PREFIX}{config.name}")
        if existing is not None and not config.static:
            return
        await self._backend.kv_set(
            f"{_TARGET_PREFIX}{config.name}",
            json.dumps(_target_to_dict(config)),
        )
        logger.debug("ProxyRepository: seeded target '%s' (static=%s)", config.name, config.static)

    async def seed_provider(self, provider: ProxyProvider) -> None:
        """Persist *provider* from YAML.

        Managed providers are always overwritten; unstatic are write-if-not-exists.
        """
        existing = await self._backend.kv_get(f"{_PROVIDER_PREFIX}{provider.name}")
        if existing is not None and not provider.static:
            return
        await self._backend.kv_set(
            f"{_PROVIDER_PREFIX}{provider.name}",
            json.dumps(_provider_to_dict(provider)),
        )
        logger.debug("ProxyRepository: seeded provider '%s' (static=%s)", provider.name, provider.static)

    async def seed_pool(self, pool: IpPool) -> None:
        """Persist *pool* from YAML.

        Managed pools are always overwritten; unstatic are write-if-not-exists.
        """
        existing = await self._backend.kv_get(f"{_POOL_PREFIX}{pool.name}")
        if existing is not None and not pool.static:
            return
        await self._backend.kv_set(
            f"{_POOL_PREFIX}{pool.name}",
            json.dumps(_pool_to_dict(pool)),
        )
        logger.debug("ProxyRepository: seeded pool '%s' (static=%s)", pool.name, pool.static)

    # ------------------------------------------------------------------
    # Pub/sub change subscription
    # ------------------------------------------------------------------

    def subscribe_changes(self):
        """Async context manager yielding ``ChangeEvent`` objects."""
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

    async def _get_or_raise_pool(self, name: str) -> IpPool:
        pool = await self.get_pool(name)
        if pool is None:
            raise ValueError(f"Pool '{name}' not found in the repository.")
        return pool

    async def _build_provider_map(self) -> dict[str, ProxyProvider]:
        """Return all stored providers indexed by name."""
        return {p.name: p for p in await self.list_providers()}

    async def _cascade_provider(self, provider: ProxyProvider) -> None:
        """Rebuild resolved_ips for every pool that references *provider*, then
        cascade to every target that references those pools.

        Flow: provider changed → affected pools recomputed → affected targets
        updated (resolved_ips snapshot) → target:update events emitted →
        ProxyServer diffs IPs and pushes new ones to live queues.
        """
        provider_map = await self._build_provider_map()
        # Ensure the updated provider is in the map (may not be persisted yet on first call)
        provider_map[provider.name] = provider

        pools = await self.list_pools()
        affected_pools: list[IpPool] = [
            p for p in pools
            if any(req.provider == provider.name for req in p.ip_requests)
        ]

        for pool in affected_pools:
            await self._cascade_pool(pool, provider_map=provider_map)

    async def _cascade_pool(
        self,
        pool: IpPool,
        *,
        provider_map: dict[str, ProxyProvider] | None = None,
    ) -> None:
        """Rebuild resolved_ips for every target that references *pool*.

        Emits target:update events for each affected target.
        """
        if provider_map is None:
            provider_map = await self._build_provider_map()

        new_resolved = _resolve_pool_ips(pool, provider_map)

        targets = await self.list_targets()
        for target in targets:
            if target.pool_name != pool.name:
                continue
            updated = target.model_copy(update={"resolved_ips": new_resolved})
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
                "ProxyRepository: cascaded pool '%s' IP update to target '%s'",
                pool.name, target.name,
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
                    if entity not in ("target", "provider", "pool"):
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
