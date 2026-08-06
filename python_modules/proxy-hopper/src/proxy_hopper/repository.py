"""ProxyRepository — runtime entity store backed by ConfigStore + Backend pub/sub.

Stores targets, providers, and IP pools as JSON-serialisable dicts in the
ConfigStore and publishes change notifications over Backend pub/sub so all
instances hot-reload.

Key schema
----------
ConfigStore entity_type "target"/"provider"/"pool" — see config_store/base.py
ph:repo:changes — Backend pub/sub channel — {entity, type, name} wake-up signal

Notify-then-reconcile
----------------------
Published change events carry no payload — just which entity changed.
Consumers (e.g. ProxyServer._config_change_listener) re-read the current
value from ConfigStore on receipt rather than trusting an embedded payload.
This makes a missed pub/sub message self-healing: the next signal (or a
periodic reconcile poll) catches a consumer back up, since ConfigStore —
not the pub/sub message — is the single source of truth.

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
All writes are serialised through the ConfigStore. After each write a pub/sub
message is published over Backend so other instances pick up the change.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
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
from .config_store.base import ConfigStore

logger = logging.getLogger(__name__)

_CHANGES_CHANNEL = "ph:repo:changes"


# ---------------------------------------------------------------------------
# Change event
# ---------------------------------------------------------------------------

@dataclass
class ChangeEvent:
    """Published whenever a target, provider, or pool is added, updated, or
    removed. Carries no payload — a wake-up signal only. Consumers re-read
    the current value from ConfigStore on receipt; see module docstring.
    """
    entity: Literal["target", "provider", "pool"]
    type: Literal["add", "update", "remove"]
    name: str


# ---------------------------------------------------------------------------
# Serialisation helpers — targets
# ---------------------------------------------------------------------------

def _target_to_dict(config: TargetConfig) -> dict:
    return config.model_dump(mode="json")


def _dict_to_target(raw: dict) -> TargetConfig:
    # Never mutate the caller's dict in place — ConfigStore.get()/list() may
    # hand back the same object it has stored internally (MemoryConfigStore
    # does), unlike the old Backend.kv_get path which always deserialised a
    # fresh dict from a JSON string. Mutating in place here would silently
    # corrupt the store's own copy on every read.
    raw = dict(raw)
    if "resolved_ips" in raw and raw["resolved_ips"]:
        raw["resolved_ips"] = [
            ResolvedIP(**ip) if isinstance(ip, dict) else ip
            for ip in raw["resolved_ips"]
        ]
    if "identity" in raw and isinstance(raw["identity"], dict):
        id_raw = dict(raw["identity"])
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
    # See _dict_to_target's comment — never mutate the caller's dict.
    raw = dict(raw)
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
    """Runtime entity store — wraps ConfigStore CRUD + Backend pub/sub.

    Three first-class stored entity types: targets, providers, and IP pools.
    IP-pool runtime state (queue, failures, quarantine) lives in IPPoolStore,
    backed directly by Backend — unrelated to this class.
    """

    def __init__(self, config_store: ConfigStore, backend: Backend) -> None:
        self._config_store = config_store
        self._backend = backend

    # ------------------------------------------------------------------
    # Target CRUD
    # ------------------------------------------------------------------

    async def add_target(self, config: TargetConfig) -> None:
        """Persist a new target and notify all instances.

        Raises ValueError if a target with this name already exists.
        """
        existing = await self._config_store.get("target", config.name)
        if existing is not None:
            raise ValueError(
                f"Target '{config.name}' already exists in the repository. "
                "Use update_target to modify it."
            )
        await self._config_store.set(
            "target", config.name, _target_to_dict(config),
            static=config.static, mutable=config.mutable,
        )
        await self._publish(ChangeEvent(entity="target", type="add", name=config.name))
        logger.info("ProxyRepository: target '%s' added", config.name)

    async def update_target(self, config: TargetConfig) -> None:
        """Update an existing target and notify all instances.

        Raises ValueError if the target does not exist or is not mutable.
        """
        existing = await self._config_store.get("target", config.name)
        if existing is None:
            raise ValueError(
                f"Target '{config.name}' does not exist in the repository. "
                "Use add_target to create it."
            )
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
        await self._config_store.set(
            "target", config.name, _target_to_dict(config),
            static=config.static, mutable=config.mutable,
        )
        await self._publish(ChangeEvent(entity="target", type="update", name=config.name))
        logger.info("ProxyRepository: target '%s' updated", config.name)

    async def remove_target(self, name: str) -> None:
        """Remove a target and notify all instances."""
        existing = await self._config_store.get("target", name)
        if existing is not None:
            if existing.static:
                raise ValueError(
                    f"Target '{name}' is config-static and cannot be removed via the API. "
                    "Remove it from the YAML configuration file instead."
                )
            if not existing.mutable:
                raise ValueError(
                    f"Target '{name}' is not mutable. "
                    "Set mutable: true in its configuration to allow runtime removal."
                )
        await self._config_store.delete("target", name)
        await self._publish(ChangeEvent(entity="target", type="remove", name=name))
        logger.info("ProxyRepository: target '%s' removed", name)

    async def get_target(self, name: str) -> Optional[TargetConfig]:
        entity = await self._config_store.get("target", name)
        if entity is None:
            return None
        return _dict_to_target(entity.data)

    async def list_targets(self) -> list[TargetConfig]:
        entities = await self._config_store.list("target")
        configs = []
        for entity in entities:
            try:
                configs.append(_dict_to_target(entity.data))
            except Exception as exc:
                logger.error("ProxyRepository: failed to deserialise target '%s': %s", entity.name, exc)
        return configs

    # ------------------------------------------------------------------
    # Provider CRUD
    # ------------------------------------------------------------------

    async def add_provider(self, provider: ProxyProvider) -> None:
        """Persist a new provider, cascade IPs through pools to targets, and notify."""
        existing = await self._config_store.get("provider", provider.name)
        if existing is not None:
            raise ValueError(
                f"Provider '{provider.name}' already exists in the repository. "
                "Use update_provider to modify it."
            )
        await self._config_store.set(
            "provider", provider.name, _provider_to_dict(provider),
            static=provider.static, mutable=provider.mutable,
        )
        await self._publish(ChangeEvent(entity="provider", type="add", name=provider.name))
        logger.info("ProxyRepository: provider '%s' added", provider.name)
        await self._cascade_provider(provider)

    async def update_provider(self, provider: ProxyProvider) -> None:
        """Update an existing provider, cascade IPs through pools to targets, and notify."""
        existing = await self._config_store.get("provider", provider.name)
        if existing is None:
            raise ValueError(
                f"Provider '{provider.name}' does not exist in the repository. "
                "Use add_provider to create it."
            )
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
        await self._config_store.set(
            "provider", provider.name, _provider_to_dict(provider),
            static=provider.static, mutable=provider.mutable,
        )
        await self._publish(ChangeEvent(entity="provider", type="update", name=provider.name))
        logger.info("ProxyRepository: provider '%s' updated", provider.name)
        await self._cascade_provider(provider)

    async def remove_provider(self, name: str) -> None:
        """Remove a provider and notify.  Does not remove IPs from pools/targets."""
        existing = await self._config_store.get("provider", name)
        if existing is not None:
            if existing.static:
                raise ValueError(
                    f"Provider '{name}' is config-static and cannot be removed via the API. "
                    "Remove it from the YAML configuration file instead."
                )
            if not existing.mutable:
                raise ValueError(
                    f"Provider '{name}' is not mutable. "
                    "Set mutable: true in its configuration to allow runtime removal."
                )
        await self._config_store.delete("provider", name)
        await self._publish(ChangeEvent(entity="provider", type="remove", name=name))
        logger.info("ProxyRepository: provider '%s' removed", name)

    async def get_provider(self, name: str) -> Optional[ProxyProvider]:
        entity = await self._config_store.get("provider", name)
        if entity is None:
            return None
        return _dict_to_provider(entity.data)

    async def list_providers(self) -> list[ProxyProvider]:
        entities = await self._config_store.list("provider")
        providers = []
        for entity in entities:
            try:
                providers.append(_dict_to_provider(entity.data))
            except Exception as exc:
                logger.error("ProxyRepository: failed to deserialise provider '%s': %s", entity.name, exc)
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
        existing = await self._config_store.get("pool", pool.name)
        if existing is not None:
            raise ValueError(
                f"Pool '{pool.name}' already exists in the repository. "
                "Use update_pool to modify it."
            )
        await self._config_store.set(
            "pool", pool.name, _pool_to_dict(pool),
            static=pool.static, mutable=pool.mutable,
        )
        await self._publish(ChangeEvent(entity="pool", type="add", name=pool.name))
        logger.info("ProxyRepository: pool '%s' added", pool.name)

    async def update_pool(self, pool: IpPool) -> None:
        """Update an existing pool, cascade resolved IPs to targets, and notify.

        Raises ValueError if the pool does not exist or is not mutable.
        """
        existing = await self._config_store.get("pool", pool.name)
        if existing is None:
            raise ValueError(
                f"Pool '{pool.name}' does not exist in the repository. "
                "Use add_pool to create it."
            )
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
        await self._config_store.set(
            "pool", pool.name, _pool_to_dict(pool),
            static=pool.static, mutable=pool.mutable,
        )
        await self._publish(ChangeEvent(entity="pool", type="update", name=pool.name))
        logger.info("ProxyRepository: pool '%s' updated", pool.name)
        await self._cascade_pool(pool)

    async def remove_pool(self, name: str) -> None:
        """Remove a pool and notify all instances."""
        existing = await self._config_store.get("pool", name)
        if existing is not None:
            if existing.static:
                raise ValueError(
                    f"Pool '{name}' is config-static and cannot be removed via the API. "
                    "Remove it from the YAML configuration file instead."
                )
            if not existing.mutable:
                raise ValueError(
                    f"Pool '{name}' is not mutable. "
                    "Set mutable: true in its configuration to allow runtime removal."
                )
        await self._config_store.delete("pool", name)
        await self._publish(ChangeEvent(entity="pool", type="remove", name=name))
        logger.info("ProxyRepository: pool '%s' removed", name)

    async def get_pool(self, name: str) -> Optional[IpPool]:
        entity = await self._config_store.get("pool", name)
        if entity is None:
            return None
        return _dict_to_pool(entity.data)

    async def list_pools(self) -> list[IpPool]:
        entities = await self._config_store.list("pool")
        pools = []
        for entity in entities:
            try:
                pools.append(_dict_to_pool(entity.data))
            except Exception as exc:
                logger.error("ProxyRepository: failed to deserialise pool '%s': %s", entity.name, exc)
        return pools

    async def resolve_pool_member_ips(self, name: str) -> list[ResolvedIP]:
        """Return the concrete IPs *name* currently draws from its providers.

        Pools don't store their own resolved IPs (only targets do) — this
        computes the same deterministic first-N selection targets get via
        the pool cascade (see ``_resolve_pool_ips``), for callers (the admin
        API's ``poolIpHealth`` query) that need a pool's membership without
        going through a target. Returns ``[]`` if the pool doesn't exist.
        """
        pool = await self.get_pool(name)
        if pool is None:
            return []
        provider_map = {p.name: p for p in await self.list_providers()}
        return _resolve_pool_ips(pool, provider_map)

    # ------------------------------------------------------------------
    # Startup seeding (write-if-not-exists, no pub/sub)
    # ------------------------------------------------------------------

    async def seed_target(self, config: TargetConfig) -> None:
        """Persist *config* from YAML.

        Managed entities (static=True, the default for YAML-defined targets) are
        always overwritten so that config-file changes take effect on restart.
        Unstatic entities are written only if no entry already exists.
        """
        existing = await self._config_store.get("target", config.name)
        if existing is not None and not config.static:
            return
        await self._config_store.set(
            "target", config.name, _target_to_dict(config),
            static=config.static, mutable=config.mutable,
        )
        logger.debug("ProxyRepository: seeded target '%s' (static=%s)", config.name, config.static)

    async def seed_provider(self, provider: ProxyProvider) -> None:
        """Persist *provider* from YAML.

        Managed providers are always overwritten; unstatic are write-if-not-exists.
        """
        existing = await self._config_store.get("provider", provider.name)
        if existing is not None and not provider.static:
            return
        await self._config_store.set(
            "provider", provider.name, _provider_to_dict(provider),
            static=provider.static, mutable=provider.mutable,
        )
        logger.debug("ProxyRepository: seeded provider '%s' (static=%s)", provider.name, provider.static)

    async def seed_pool(self, pool: IpPool) -> None:
        """Persist *pool* from YAML.

        Managed pools are always overwritten; unstatic are write-if-not-exists.
        """
        existing = await self._config_store.get("pool", pool.name)
        if existing is not None and not pool.static:
            return
        await self._config_store.set(
            "pool", pool.name, _pool_to_dict(pool),
            static=pool.static, mutable=pool.mutable,
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
            await self._config_store.set(
                "target", target.name, _target_to_dict(updated),
                static=updated.static, mutable=updated.mutable,
            )
            await self._publish(ChangeEvent(entity="target", type="update", name=target.name))
            logger.info(
                "ProxyRepository: cascaded pool '%s' IP update to target '%s'",
                pool.name, target.name,
            )

    # ------------------------------------------------------------------
    # Runtime IP state — reads from pool backend keys directly
    # ------------------------------------------------------------------

    async def get_target_ip_runtime_states(self, target_name: str) -> list[dict]:
        """Return runtime state for every resolved IP on *target_name*.

        Each entry contains: address, host, port, provider, failures,
        quarantined, release_at, user_agent, request_count, cookies_enabled.
        Returns an empty list if the target does not exist.
        """
        from .pool_store import IPPoolStore

        config = await self.get_target(target_name)
        if config is None:
            return []

        store = IPPoolStore(self._backend)
        quarantine_scores = await store.quarantine_list_with_scores(target_name)
        quarantined_map: dict[str, float] = dict(quarantine_scores)

        results = []
        for ip in config.resolved_ips:
            address = f"{ip.host}:{ip.port}"
            failures = await store.get_failures(target_name, address)
            quarantined = address in quarantined_map
            release_at = quarantined_map.get(address)

            uuid = await store.ip_get(target_name, address)
            identity_data: dict | None = None
            if uuid:
                identity_data = await store.identity_read(target_name, uuid)

            id_headers: dict = (identity_data or {}).get("headers", {})
            id_cookies: dict = (identity_data or {}).get("cookies", {})
            results.append({
                "address": address,
                "host": ip.host,
                "port": ip.port,
                "provider": ip.provider,
                "failures": failures,
                "quarantined": quarantined,
                "release_at": release_at,
                "user_agent": id_headers.get("user-agent"),
                "request_count": (identity_data or {}).get("request_count", 0),
                "cookies_enabled": (identity_data or {}).get("cookies_enabled", False),
                "profile_headers": [{"name": k, "value": v} for k, v in id_headers.items()],
                "cookies": [{"name": k, "value": v} for k, v in id_cookies.items()],
                "identity_enabled": config.identity.enabled,
            })
        return results

    async def _publish(self, event: ChangeEvent) -> None:
        payload = json.dumps({
            "entity": event.entity,
            "type": event.type,
            "name": event.name,
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
                    )
                except Exception as exc:
                    logger.warning(
                        "ProxyRepository: failed to parse change event: %s", exc
                    )

        return _iter()

    async def __aexit__(self, *args) -> None:
        if self._ctx is not None:
            await self._ctx.__aexit__(*args)
