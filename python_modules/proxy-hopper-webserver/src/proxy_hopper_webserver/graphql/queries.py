"""GraphQL Query type."""

from __future__ import annotations

from typing import Optional

import strawberry
from strawberry.types import Info

from ._auth import require_permission
from .context import Context
from .types import (
    IpHealthType,
    IpPoolType,
    IpRuntimeStateType,
    KeyValueType,
    ProviderType,
    StatusType,
    TargetMetricsType,
    TargetType,
    ip_health_to_gql,
    pool_to_gql,
    provider_to_gql,
    target_metrics_to_gql,
    target_to_gql,
)


async def _resolve_ip_health(ctx: Context, addresses: list[str]) -> list[IpHealthType]:
    """Dual-source lookup shared by provider_ip_health/pool_ip_health — same
    Prometheus-first, IpHealthStore-fallback priority as target_metrics.
    Every requested address gets a row (status=None when neither source has
    data), never a partial list — see IpHealthStore.get_many.
    """
    if not addresses:
        return []
    if ctx.prometheus_url:
        from proxy_hopper_webserver.prometheus_query import query_ip_health
        snapshots = await query_ip_health(ctx.prometheus_url, addresses)
    elif ctx.ip_health is not None:
        snapshots = await ctx.ip_health.get_many(addresses)
    else:
        from proxy_hopper.ip_health import IpHealthSnapshot
        snapshots = {
            addr: IpHealthSnapshot(address=addr, provider=None, status=None, last_check_at=None, reason=None)
            for addr in addresses
        }
    return [ip_health_to_gql(snapshots[addr]) for addr in addresses]


@strawberry.type
class Query:
    @strawberry.field(description="List all targets stored in the repository.")
    async def targets(self, info: Info[Context, None]) -> list[TargetType]:
        from proxy_hopper.auth import Permission
        require_permission(info, Permission.read)
        configs = await info.context.repo.list_targets()
        return [target_to_gql(c) for c in configs]

    @strawberry.field(description="Fetch a single target by name.")
    async def target(self, info: Info[Context, None], name: str) -> Optional[TargetType]:
        from proxy_hopper.auth import Permission
        require_permission(info, Permission.read)
        config = await info.context.repo.get_target(name)
        return target_to_gql(config) if config else None

    @strawberry.field(description="List all IP pools stored in the repository.")
    async def pools(self, info: Info[Context, None]) -> list[IpPoolType]:
        from proxy_hopper.auth import Permission
        require_permission(info, Permission.read)
        pools = await info.context.repo.list_pools()
        return [pool_to_gql(p) for p in pools]

    @strawberry.field(description="Fetch a single IP pool by name.")
    async def pool(self, info: Info[Context, None], name: str) -> Optional[IpPoolType]:
        from proxy_hopper.auth import Permission
        require_permission(info, Permission.read)
        p = await info.context.repo.get_pool(name)
        return pool_to_gql(p) if p else None

    @strawberry.field(description="List all providers stored in the repository.")
    async def providers(self, info: Info[Context, None]) -> list[ProviderType]:
        from proxy_hopper.auth import Permission
        require_permission(info, Permission.read)
        providers = await info.context.repo.list_providers()
        return [provider_to_gql(p) for p in providers]

    @strawberry.field(description="Fetch a single provider by name.")
    async def provider(self, info: Info[Context, None], name: str) -> Optional[ProviderType]:
        from proxy_hopper.auth import Permission
        require_permission(info, Permission.read)
        p = await info.context.repo.get_provider(name)
        return provider_to_gql(p) if p else None

    @strawberry.field(description="Runtime state for every resolved IP on a target.")
    async def target_ip_states(
        self, info: Info[Context, None], target_name: str
    ) -> list[IpRuntimeStateType]:
        from proxy_hopper.auth import Permission
        require_permission(info, Permission.read)
        rows = await info.context.repo.get_target_ip_runtime_states(target_name)
        return [
            IpRuntimeStateType(
                address=r["address"],
                host=r["host"],
                port=r["port"],
                provider=r["provider"],
                failures=r["failures"],
                quarantined=r["quarantined"],
                release_at=r["release_at"],
                user_agent=r["user_agent"],
                request_count=r["request_count"],
                cookies_enabled=r["cookies_enabled"],
                profile_headers=[KeyValueType(name=h["name"], value=h["value"]) for h in r.get("profile_headers", [])],
                cookies=[KeyValueType(name=c["name"], value=c["value"]) for c in r.get("cookies", [])],
                identity_enabled=r.get("identity_enabled", False),
            )
            for r in rows
        ]

    @strawberry.field(
        description="Aggregate request metrics for one target — source is Prometheus "
        "when server.prometheusUrl is configured, otherwise lightweight in-process "
        "counters (empty/zero if the admin process doesn't share a backend with the "
        "proxy — see the core README's Admin API section)."
    )
    async def target_metrics(
        self, info: Info[Context, None], name: str
    ) -> Optional[TargetMetricsType]:
        from proxy_hopper.auth import Permission
        require_permission(info, Permission.read)
        ctx = info.context

        if ctx.prometheus_url:
            from proxy_hopper_webserver.prometheus_query import query_target_metrics
            snapshot = await query_target_metrics(ctx.prometheus_url, name)
        elif ctx.app_metrics is not None:
            snapshot = await ctx.app_metrics.get(name)
        else:
            return None

        return target_metrics_to_gql(snapshot)

    @strawberry.field(
        description="Reachability status for every IP in a provider's ipList, from "
        "the background prober — source is Prometheus when server.prometheusUrl is "
        "configured, otherwise the in-process/shared-backend IpHealthStore (empty "
        "status if the admin process doesn't share a backend with the running "
        "proxies — see admin-server/overview.mdx's Embedded vs separate process). "
        "Diagnostic only — does not affect quarantine, which is target-scoped and "
        "driven solely by live-traffic failures."
    )
    async def provider_ip_health(
        self, info: Info[Context, None], provider_name: str
    ) -> list[IpHealthType]:
        from proxy_hopper.auth import Permission
        require_permission(info, Permission.read)
        ctx = info.context
        provider = await ctx.repo.get_provider(provider_name)
        if provider is None:
            return []
        return await _resolve_ip_health(ctx, list(provider.ip_list))

    @strawberry.field(
        description="Reachability status for every IP a pool currently draws from its "
        "providers (same first-N selection used to build target resolvedIps). Same "
        "dual source and diagnostic-only caveat as providerIpHealth."
    )
    async def pool_ip_health(
        self, info: Info[Context, None], pool_name: str
    ) -> list[IpHealthType]:
        from proxy_hopper.auth import Permission
        require_permission(info, Permission.read)
        ctx = info.context
        resolved = await ctx.repo.resolve_pool_member_ips(pool_name)
        addresses = [f"{ip.host}:{ip.port}" for ip in resolved]
        rows = await _resolve_ip_health(ctx, addresses)
        # Attach provider attribution from the pool resolution itself — the
        # health source (especially Prometheus) may not always have it.
        provider_by_address = {f"{ip.host}:{ip.port}": ip.provider for ip in resolved}
        for row in rows:
            if row.provider is None:
                row.provider = provider_by_address.get(row.address)
        return rows

    @strawberry.field(description="Current auth state and caller identity.")
    async def status(self, info: Info[Context, None]) -> StatusType:
        from proxy_hopper.auth import Permission
        require_permission(info, Permission.read)
        ctx = info.context
        return StatusType(
            auth_enabled=ctx.auth_config.enabled if ctx.auth_config else False,
            user_sub=ctx.user.sub,
            user_role=ctx.user.role,
            read_only=ctx.read_only,
        )
