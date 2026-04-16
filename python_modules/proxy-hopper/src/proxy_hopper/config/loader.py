"""Config file loading — reads YAML and assembles a ProxyHopperConfig."""

from __future__ import annotations

import random
from pathlib import Path

import yaml

from .models import (
    BasicAuth,
    ProxyHopperConfig,
    ProxyProvider,
    ResolvedIP,
    ServerConfig,
    TargetConfig,
    _parse_address,
)
from .normalization import (
    _normalise_pool,
    _normalise_provider,
    _normalise_server,
    _normalise_target,
    _parse_auth,
)


def load_config(path: Path | str) -> ProxyHopperConfig:
    """Load and return the full configuration from a YAML file.

    Resolution order:
      1. proxyProviders are parsed and indexed by name.
      2. ipPools resolve their ipRequests against providers, randomly sampling
         the requested count of IPs from each provider's list.
      3. Targets reference pools or inline ipLists; the result is a flat list
         of ResolvedIP objects that carry provider/region metadata.
      4. ServerConfig is constructed with YAML values as explicit kwargs, which
         pydantic-settings treats as higher priority than env vars — giving the
         correct chain: CLI > YAML > env vars > defaults.
    """
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}

    default_port = 8080  # used when parsing addresses without explicit ports

    # --- Proxy providers ----------------------------------------------------
    providers: list[ProxyProvider] = []
    provider_map: dict[str, ProxyProvider] = {}
    for p_raw in raw.get("proxyProviders", []):
        normalised = _normalise_provider(p_raw)
        # Normalise auth sub-block if present
        if "auth" in normalised and isinstance(normalised["auth"], dict):
            normalised["auth"] = BasicAuth(**normalised["auth"])
        provider = ProxyProvider(**normalised)
        if provider.name in provider_map:
            raise ValueError(f"Duplicate proxyProvider name: '{provider.name}'")
        provider_map[provider.name] = provider
        providers.append(provider)

    # --- IP pools -----------------------------------------------------------
    # Each pool resolves to a list of ResolvedIP (carries provider metadata).
    pool_map: dict[str, list[ResolvedIP]] = {}
    for pool_raw in raw.get("ipPools", []):
        normalised = _normalise_pool(pool_raw)
        pool_name = normalised.get("name")
        if not pool_name:
            raise ValueError("ipPool entry is missing a 'name' field")

        resolved: list[ResolvedIP] = []

        # ipRequests — draw from providers
        for req in normalised.get("ip_requests", []):
            provider_name = req.get("provider")
            count = req.get("count")
            if provider_name not in provider_map:
                raise ValueError(
                    f"ipPool '{pool_name}' references unknown provider '{provider_name}'. "
                    f"Defined providers: {list(provider_map)}"
                )
            provider = provider_map[provider_name]
            available = provider.resolved_ip_list(default_port)
            if count is not None and count > len(available):
                raise ValueError(
                    f"ipPool '{pool_name}' requests {count} IPs from provider "
                    f"'{provider_name}' but only {len(available)} are available."
                )
            selected = random.sample(available, count) if count is not None else list(available)
            for host, port in selected:
                resolved.append(ResolvedIP(
                    host=host,
                    port=port,
                    provider=provider.name,
                    region_tag=provider.region_tag or "",
                ))

        # ipList — inline IPs with no provider metadata
        for entry in normalised.get("ip_list", []):
            host, port = _parse_address(entry, default_port)
            resolved.append(ResolvedIP(host=host, port=port))

        if not resolved:
            raise ValueError(
                f"ipPool '{pool_name}' has no IPs — add ipRequests or ipList."
            )

        if pool_name in pool_map:
            raise ValueError(f"Duplicate ipPool name: '{pool_name}'")
        pool_map[pool_name] = resolved

    # --- Targets ------------------------------------------------------------
    targets: list[TargetConfig] = []
    for t_raw in raw.get("targets", []):
        normalised = _normalise_target(t_raw)
        target_name = normalised.get("name", "<unnamed>")
        default_proxy_port = normalised.get("default_proxy_port", default_port)

        pool_ref = normalised.pop("ip_pool", None)
        inline_ip_list = normalised.pop("ip_list", None)

        if pool_ref is not None and inline_ip_list is not None:
            raise ValueError(
                f"Target '{target_name}' specifies both ipPool and ipList — use one."
            )
        if pool_ref is None and inline_ip_list is None:
            raise ValueError(
                f"Target '{target_name}' must specify either ipPool or ipList."
            )

        if pool_ref is not None:
            if pool_ref not in pool_map:
                raise ValueError(
                    f"Target '{target_name}' references unknown ipPool '{pool_ref}'. "
                    f"Defined pools: {list(pool_map)}"
                )
            resolved_ips = pool_map[pool_ref]
        else:
            resolved_ips = [
                ResolvedIP(host=h, port=p)
                for h, p in (
                    _parse_address(entry, default_proxy_port)
                    for entry in inline_ip_list
                )
            ]

        targets.append(TargetConfig(resolved_ips=resolved_ips, **normalised))

    # --- Server settings ----------------------------------------------------
    yaml_server = _normalise_server(raw.get("server") or {})
    server = ServerConfig(**yaml_server)

    # --- Auth config --------------------------------------------------------
    auth = _parse_auth(raw.get("auth") or {})

    return ProxyHopperConfig(server=server, targets=targets, providers=providers, auth=auth)
