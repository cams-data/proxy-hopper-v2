"""Config file loading — reads YAML and assembles a ProxyHopperConfig."""

from __future__ import annotations

from pathlib import Path

import yaml

from .models import (
    BasicAuth,
    IpPool,
    ProxyHopperConfig,
    ProxyProvider,
    ResolvedIP,
    ServerConfig,
    TargetConfig,
    _parse_address,
)
from .normalization import (
    _normalise_pool_to_model,
    _normalise_provider,
    _normalise_server,
    _normalise_target,
    _parse_auth,
)


def _resolve_pool_ips(
    pool: IpPool,
    provider_map: dict[str, ProxyProvider],
    default_port: int = 8080,
) -> list[ResolvedIP]:
    """Resolve a pool's ip_requests against providers into a flat list of ResolvedIP.

    Count is treated as a maximum — if the provider has fewer IPs than requested
    the available IPs are taken without error.  Selection is deterministic
    (first N) so that the cascade logic at runtime can reproduce the same set.
    """
    resolved: list[ResolvedIP] = []
    for req in pool.ip_requests:
        if req.provider not in provider_map:
            raise ValueError(
                f"ipPool '{pool.name}' references unknown provider '{req.provider}'. "
                f"Defined providers: {list(provider_map)}"
            )
        provider = provider_map[req.provider]
        available = provider.resolved_ip_list(default_port)
        # Graceful: take min(count, available) — never hard-error on count > len
        selected = available[: req.count]
        for host, port in selected:
            resolved.append(ResolvedIP(
                host=host,
                port=port,
                provider=provider.name,
                region_tag=provider.region_tag or "",
            ))
    return resolved


def load_config(path: Path | str) -> ProxyHopperConfig:
    """Load and return the full configuration from a YAML file.

    Resolution order:
      1. proxyProviders are parsed and indexed by name.
      2. ipPools are parsed into IpPool objects (provider references kept).
         IPs are resolved to a snapshot for the initial TargetConfig.resolved_ips.
      3. Targets must reference an ipPool by name; inline ipList is not supported.
      4. ServerConfig is constructed with YAML values as explicit kwargs, which
         pydantic-settings treats as higher priority than env vars — giving the
         correct chain: CLI > YAML > env vars > defaults.
    """
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}

    default_port = 8080

    # --- Proxy providers ----------------------------------------------------
    providers: list[ProxyProvider] = []
    provider_map: dict[str, ProxyProvider] = {}
    for p_raw in raw.get("proxyProviders", []):
        normalised = _normalise_provider(p_raw)
        if "auth" in normalised and isinstance(normalised["auth"], dict):
            normalised["auth"] = BasicAuth(**normalised["auth"])
        provider = ProxyProvider(**normalised)
        if provider.name in provider_map:
            raise ValueError(f"Duplicate proxyProvider name: '{provider.name}'")
        provider_map[provider.name] = provider
        providers.append(provider)

    # --- IP pools -----------------------------------------------------------
    pools: list[IpPool] = []
    pool_map: dict[str, IpPool] = {}
    for pool_raw in raw.get("ipPools", []):
        pool = _normalise_pool_to_model(pool_raw)
        if not pool.name:
            raise ValueError("ipPool entry is missing a 'name' field")
        if pool.name in pool_map:
            raise ValueError(f"Duplicate ipPool name: '{pool.name}'")
        # Validate all provider references exist
        for req in pool.ip_requests:
            if req.provider not in provider_map:
                raise ValueError(
                    f"ipPool '{pool.name}' references unknown provider '{req.provider}'. "
                    f"Defined providers: {list(provider_map)}"
                )
        pool_map[pool.name] = pool
        pools.append(pool)

    # --- Targets ------------------------------------------------------------
    targets: list[TargetConfig] = []
    for t_raw in raw.get("targets", []):
        normalised = _normalise_target(t_raw)
        target_name = normalised.get("name", "<unnamed>")
        default_proxy_port = normalised.get("default_proxy_port", default_port)

        # Support both camelCase key (ipPool → ip_pool) and snake_case (pool_name)
        pool_ref = normalised.pop("ip_pool", None) or normalised.pop("pool_name", None)

        if pool_ref is None:
            raise ValueError(
                f"Target '{target_name}' must specify 'ipPool' referencing an ipPool. "
                "Inline ipList is no longer supported — declare IPs in a proxyProvider."
            )
        if pool_ref not in pool_map:
            raise ValueError(
                f"Target '{target_name}' references unknown ipPool '{pool_ref}'. "
                f"Defined pools: {list(pool_map)}"
            )

        pool = pool_map[pool_ref]
        resolved_ips = _resolve_pool_ips(pool, provider_map, default_proxy_port)

        targets.append(TargetConfig(
            pool_name=pool_ref,
            resolved_ips=resolved_ips,
            **normalised,
        ))

    # --- Server settings ----------------------------------------------------
    yaml_server = _normalise_server(raw.get("server") or {})
    server = ServerConfig(**yaml_server)

    # --- Auth config --------------------------------------------------------
    auth = _parse_auth(raw.get("auth") or {})

    return ProxyHopperConfig(
        server=server,
        targets=targets,
        providers=providers,
        pools=pools,
        auth=auth,
    )
