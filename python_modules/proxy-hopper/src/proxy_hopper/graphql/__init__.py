"""GraphQL API for Proxy Hopper — powered by Strawberry.

Mounted at ``/graphql`` on the admin FastAPI app.  Requires a
``ProxyRepository`` instance at construction time; the admin app only
registers the router when one is available.

Schema
------
Queries  : targets, target, providers, provider, status
Mutations: addTarget, updateTarget, removeTarget,
           addProvider, updateProvider, removeProvider,
           addIp, removeIp, swapIp

Auth
----
All operations require at least ``read`` permission; all mutations require
``write`` permission.  When auth is disabled every caller is treated as an
admin and all permission checks are skipped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import strawberry
from strawberry.fastapi import GraphQLRouter

from .mutations import Mutation
from .queries import Query

if TYPE_CHECKING:
    from ..auth import AuthConfig
    from ..repository import ProxyRepository

schema = strawberry.Schema(query=Query, mutation=Mutation)


def create_graphql_router(
    repo: "ProxyRepository",
    auth_config: "AuthConfig | None",
    get_current_user: Any,
) -> GraphQLRouter:
    """Return a Strawberry ``GraphQLRouter`` wired to *repo* and *auth_config*.

    *get_current_user* is the FastAPI dependency produced by
    ``make_fastapi_deps`` — it validates the Bearer token and returns an
    ``AuthenticatedUser`` (or a synthetic admin when auth is disabled).
    """
    from fastapi import Depends, Request

    from .context import Context

    async def get_context(
        request: Request,
        user=Depends(get_current_user),
    ) -> Context:
        return Context(repo=repo, user=user, auth_config=auth_config)

    return GraphQLRouter(schema, context_getter=get_context)
