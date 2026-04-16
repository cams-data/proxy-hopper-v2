"""GraphQL Query type."""

from __future__ import annotations

from typing import Optional

import strawberry
from strawberry.types import Info

from ._auth import require_permission
from .context import Context
from .types import ProviderType, StatusType, TargetType, provider_to_gql, target_to_gql


@strawberry.type
class Query:
    # ------------------------------------------------------------------
    # Targets
    # ------------------------------------------------------------------

    @strawberry.field(description="List all targets stored in the repository.")
    async def targets(self, info: Info[Context, None]) -> list[TargetType]:
        from ..auth import Permission
        require_permission(info, Permission.read)
        configs = await info.context.repo.list_targets()
        return [target_to_gql(c) for c in configs]

    @strawberry.field(description="Fetch a single target by name.")
    async def target(
        self, info: Info[Context, None], name: str
    ) -> Optional[TargetType]:
        from ..auth import Permission
        require_permission(info, Permission.read)
        config = await info.context.repo.get_target(name)
        return target_to_gql(config) if config else None

    # ------------------------------------------------------------------
    # Providers
    # ------------------------------------------------------------------

    @strawberry.field(description="List all providers stored in the repository.")
    async def providers(self, info: Info[Context, None]) -> list[ProviderType]:
        from ..auth import Permission
        require_permission(info, Permission.read)
        providers = await info.context.repo.list_providers()
        return [provider_to_gql(p) for p in providers]

    @strawberry.field(description="Fetch a single provider by name.")
    async def provider(
        self, info: Info[Context, None], name: str
    ) -> Optional[ProviderType]:
        from ..auth import Permission
        require_permission(info, Permission.read)
        p = await info.context.repo.get_provider(name)
        return provider_to_gql(p) if p else None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @strawberry.field(description="Current auth state and caller identity.")
    async def status(self, info: Info[Context, None]) -> StatusType:
        from ..auth import Permission
        require_permission(info, Permission.read)
        ctx = info.context
        return StatusType(
            auth_enabled=ctx.auth_config.enabled if ctx.auth_config else False,
            user_sub=ctx.user.sub,
            user_role=ctx.user.role,
        )
