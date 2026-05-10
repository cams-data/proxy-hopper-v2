"""GraphQL Query type."""

from __future__ import annotations

from typing import Optional

import strawberry
from strawberry.types import Info

from ._auth import require_permission
from .context import Context
from .types import (
    IpPoolType,
    IpRuntimeStateType,
    KeyValueType,
    ProviderType,
    StatusType,
    TargetType,
    pool_to_gql,
    provider_to_gql,
    target_to_gql,
)


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

    @strawberry.field(description="Current auth state and caller identity.")
    async def status(self, info: Info[Context, None]) -> StatusType:
        from proxy_hopper.auth import Permission
        require_permission(info, Permission.read)
        ctx = info.context
        return StatusType(
            auth_enabled=ctx.auth_config.enabled if ctx.auth_config else False,
            user_sub=ctx.user.sub,
            user_role=ctx.user.role,
        )
