"""FastAPI application factory for the token server."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI

from .handler import create_token_router

if TYPE_CHECKING:
    from proxy_hopper_token_server.provider import TokenProvider


def create_app(
    provider: "TokenProvider | dict[str, TokenProvider]",
    timeout: float = 30.0,
) -> FastAPI:
    """Build and return the FastAPI application.

    Args:
        provider: A single ``TokenProvider`` for all targets, or a dict
                  mapping target names to specific providers.
        timeout:  Hard timeout in seconds for each ``get_token`` call.
    """
    app = FastAPI(title="Proxy Hopper Token Server", docs_url=None, redoc_url=None)
    app.include_router(create_token_router(provider, timeout=timeout))

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True}

    return app
