"""GraphQL API for Proxy Hopper — powered by Strawberry.

Mounted at ``/graphql`` on the admin FastAPI app.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import strawberry
from strawberry.fastapi import GraphQLRouter

from .mutations import Mutation
from .queries import Query

if TYPE_CHECKING:
    from proxy_hopper.config import AuthConfig
    from proxy_hopper.repository import ProxyRepository

schema = strawberry.Schema(query=Query, mutation=Mutation)


def create_graphql_router(
    repo: "ProxyRepository",
    auth_config: "AuthConfig | None",
    get_current_user: Any,
) -> GraphQLRouter:
    """Return a Strawberry ``GraphQLRouter`` wired to *repo* and *auth_config*."""
    from fastapi import Depends, Request

    from .context import Context

    async def get_context(
        user=Depends(get_current_user),
    ) -> Context:
        return Context(repo=repo, user=user, auth_config=auth_config)

    return GraphQLRouter(schema, context_getter=get_context)
