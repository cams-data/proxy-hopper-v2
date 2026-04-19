"""GraphQL Mutation type."""

from __future__ import annotations

import strawberry
from strawberry.types import Info

from ._auth import require_permission
from .context import Context
from .inputs import (
    IpPoolInput,
    ProviderInput,
    TargetInput,
    pool_input_to_model,
    provider_input_to_model,
)
from .types import (
    IpPoolType,
    ProviderType,
    TargetType,
    pool_to_gql,
    provider_to_gql,
    target_to_gql,
)


@strawberry.type
class Mutation:
    # ------------------------------------------------------------------
    # Target mutations
    # ------------------------------------------------------------------

    @strawberry.mutation(description="Add a new target to the repository.")
    async def add_target(
        self, info: Info[Context, None], input: TargetInput
    ) -> TargetType:
        from ..auth import Permission
        from ..config import ResolvedIP, TargetConfig
        require_permission(info, Permission.write)
        # Resolve the pool's current IPs as the initial snapshot
        pool = await info.context.repo.get_pool(input.pool_name)
        if pool is None:
            raise ValueError(f"Pool '{input.pool_name}' not found in the repository.")
        provider_map = await info.context.repo._build_provider_map()
        from ..repository import _resolve_pool_ips
        resolved_ips = _resolve_pool_ips(pool, provider_map, input.default_proxy_port)
        config = TargetConfig(
            name=input.name,
            regex=input.regex,
            pool_name=input.pool_name,
            resolved_ips=resolved_ips,
            min_request_interval=input.min_request_interval,
            max_queue_wait=input.max_queue_wait,
            num_retries=input.num_retries,
            ip_failures_until_quarantine=input.ip_failures_until_quarantine,
            quarantine_time=input.quarantine_time,
            default_proxy_port=input.default_proxy_port,
            spoof_user_agent=input.spoof_user_agent,
            mutable=input.mutable,
            static=input.static,
        )
        await info.context.repo.add_target(config)
        return target_to_gql(config)

    @strawberry.mutation(description="Update an existing mutable target.")
    async def update_target(
        self, info: Info[Context, None], input: TargetInput
    ) -> TargetType:
        from ..auth import Permission
        from ..config import TargetConfig
        require_permission(info, Permission.write)
        pool = await info.context.repo.get_pool(input.pool_name)
        if pool is None:
            raise ValueError(f"Pool '{input.pool_name}' not found in the repository.")
        provider_map = await info.context.repo._build_provider_map()
        from ..repository import _resolve_pool_ips
        resolved_ips = _resolve_pool_ips(pool, provider_map, input.default_proxy_port)
        config = TargetConfig(
            name=input.name,
            regex=input.regex,
            pool_name=input.pool_name,
            resolved_ips=resolved_ips,
            min_request_interval=input.min_request_interval,
            max_queue_wait=input.max_queue_wait,
            num_retries=input.num_retries,
            ip_failures_until_quarantine=input.ip_failures_until_quarantine,
            quarantine_time=input.quarantine_time,
            default_proxy_port=input.default_proxy_port,
            spoof_user_agent=input.spoof_user_agent,
            mutable=input.mutable,
            static=input.static,
        )
        await info.context.repo.update_target(config)
        return target_to_gql(config)

    @strawberry.mutation(description="Remove a target from the repository.")
    async def remove_target(
        self, info: Info[Context, None], name: str
    ) -> bool:
        from ..auth import Permission
        require_permission(info, Permission.write)
        await info.context.repo.remove_target(name)
        return True

    # ------------------------------------------------------------------
    # Pool mutations
    # ------------------------------------------------------------------

    @strawberry.mutation(description="Add a new IP pool to the repository.")
    async def add_pool(
        self, info: Info[Context, None], input: IpPoolInput
    ) -> IpPoolType:
        from ..auth import Permission
        require_permission(info, Permission.write)
        pool = pool_input_to_model(input)
        await info.context.repo.add_pool(pool)
        return pool_to_gql(pool)

    @strawberry.mutation(description="Update an existing mutable IP pool.")
    async def update_pool(
        self, info: Info[Context, None], input: IpPoolInput
    ) -> IpPoolType:
        from ..auth import Permission
        require_permission(info, Permission.write)
        pool = pool_input_to_model(input)
        await info.context.repo.update_pool(pool)
        return pool_to_gql(pool)

    @strawberry.mutation(description="Remove an IP pool from the repository.")
    async def remove_pool(
        self, info: Info[Context, None], name: str
    ) -> bool:
        from ..auth import Permission
        require_permission(info, Permission.write)
        await info.context.repo.remove_pool(name)
        return True

    # ------------------------------------------------------------------
    # Provider mutations
    # ------------------------------------------------------------------

    @strawberry.mutation(description="Add a new provider to the repository.")
    async def add_provider(
        self, info: Info[Context, None], input: ProviderInput
    ) -> ProviderType:
        from ..auth import Permission
        require_permission(info, Permission.write)
        provider = provider_input_to_model(input)
        await info.context.repo.add_provider(provider)
        return provider_to_gql(provider)

    @strawberry.mutation(description="Update an existing mutable provider.")
    async def update_provider(
        self, info: Info[Context, None], input: ProviderInput
    ) -> ProviderType:
        from ..auth import Permission
        require_permission(info, Permission.write)
        provider = provider_input_to_model(input)
        await info.context.repo.update_provider(provider)
        return provider_to_gql(provider)

    @strawberry.mutation(description="Remove a provider from the repository.")
    async def remove_provider(
        self, info: Info[Context, None], name: str
    ) -> bool:
        from ..auth import Permission
        require_permission(info, Permission.write)
        await info.context.repo.remove_provider(name)
        return True

    @strawberry.mutation(
        description="Append an IP address to a provider's ip_list. "
        "Cascades through pools to all referencing targets."
    )
    async def add_ip_to_provider(
        self, info: Info[Context, None], provider: str, address: str
    ) -> ProviderType:
        from ..auth import Permission
        require_permission(info, Permission.write)
        updated = await info.context.repo.add_ip_to_provider(provider, address)
        return provider_to_gql(updated)

    @strawberry.mutation(
        description="Remove an IP address from a provider's ip_list. "
        "Cascades through pools to all referencing targets. "
        "Old IP drains naturally from live pool queues."
    )
    async def remove_ip_from_provider(
        self, info: Info[Context, None], provider: str, address: str
    ) -> ProviderType:
        from ..auth import Permission
        require_permission(info, Permission.write)
        updated = await info.context.repo.remove_ip_from_provider(provider, address)
        return provider_to_gql(updated)
