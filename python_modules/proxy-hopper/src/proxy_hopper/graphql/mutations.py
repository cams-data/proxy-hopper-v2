"""GraphQL Mutation type."""

from __future__ import annotations

import strawberry
from strawberry.types import Info

from ._auth import require_permission
from .context import Context
from .inputs import ProviderInput, TargetInput, provider_input_to_model, target_input_to_config
from .types import ProviderType, TargetType, provider_to_gql, target_to_gql


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
        require_permission(info, Permission.write)
        config = target_input_to_config(input)
        await info.context.repo.add_target(config)
        return target_to_gql(config)

    @strawberry.mutation(description="Update an existing mutable target.")
    async def update_target(
        self, info: Info[Context, None], input: TargetInput
    ) -> TargetType:
        from ..auth import Permission
        require_permission(info, Permission.write)
        config = target_input_to_config(input)
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

    # ------------------------------------------------------------------
    # Target IP list mutations
    # ------------------------------------------------------------------

    @strawberry.mutation(description="Append an IP address to a target's pool.")
    async def add_ip(
        self, info: Info[Context, None], target: str, address: str
    ) -> TargetType:
        from ..auth import Permission
        require_permission(info, Permission.write)
        await info.context.repo.add_ip(target, address)
        config = await info.context.repo.get_target(target)
        return target_to_gql(config)

    @strawberry.mutation(
        description="Remove an IP address from a target's pool. "
        "Raises if it is the last IP."
    )
    async def remove_ip(
        self, info: Info[Context, None], target: str, address: str
    ) -> TargetType:
        from ..auth import Permission
        require_permission(info, Permission.write)
        await info.context.repo.remove_ip(target, address)
        config = await info.context.repo.get_target(target)
        return target_to_gql(config)

    @strawberry.mutation(
        description="Replace oldAddress with newAddress in a target's pool. "
        "The old IP drains naturally from the live pool."
    )
    async def swap_ip(
        self,
        info: Info[Context, None],
        target: str,
        old_address: str,
        new_address: str,
    ) -> TargetType:
        from ..auth import Permission
        require_permission(info, Permission.write)
        await info.context.repo.swap_ip(target, old_address, new_address)
        config = await info.context.repo.get_target(target)
        return target_to_gql(config)
