"""POST /token route handler."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from proxy_hopper_token_server.models import Profile, TokenRequest

if TYPE_CHECKING:
    from proxy_hopper_token_server.provider import TokenProvider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic wire models (JSON ↔ dataclasses)
# ---------------------------------------------------------------------------

class _ProfileIn(BaseModel):
    user_agent: str
    accept: str
    accept_language: str
    accept_encoding: str
    extra: dict[str, str] = {}


class _TokenRequestIn(BaseModel):
    target: str
    ip: str
    port: int
    cursor: dict[str, Any] = {}
    profile: _ProfileIn
    proxy_url: str | None = None


class _TokenResponseOut(BaseModel):
    headers: dict[str, str]
    expires_at: datetime
    cursor: dict[str, Any]


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

def create_token_router(
    provider: "TokenProvider | dict[str, TokenProvider]",
    timeout: float = 30.0,
) -> APIRouter:
    router = APIRouter()

    def _resolve_provider(target: str) -> "TokenProvider":
        if isinstance(provider, dict):
            p = provider.get(target)
            if p is None:
                raise HTTPException(
                    status_code=404,
                    detail={"error": "unknown_target", "detail": f"No provider registered for target {target!r}"},
                )
            return p
        return provider

    @router.post("/token", response_model=_TokenResponseOut)
    async def token(body: _TokenRequestIn) -> _TokenResponseOut:
        p = _resolve_provider(body.target)
        req = TokenRequest(
            target=body.target,
            ip=body.ip,
            port=body.port,
            cursor=body.cursor,
            profile=Profile(
                user_agent=body.profile.user_agent,
                accept=body.profile.accept,
                accept_language=body.profile.accept_language,
                accept_encoding=body.profile.accept_encoding,
                extra=body.profile.extra,
            ),
            proxy_url=body.proxy_url,
        )
        try:
            resp = await asyncio.wait_for(p.get_token(req), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "Token server: get_token timed out after %.1fs (target=%s ip=%s)",
                timeout, body.target, body.ip,
            )
            raise HTTPException(
                status_code=504,
                detail={"error": "timeout", "detail": f"get_token exceeded {timeout}s"},
            )
        except Exception as exc:
            logger.error(
                "Token server: get_token raised %s (target=%s ip=%s): %s",
                type(exc).__name__, body.target, body.ip, exc,
            )
            raise HTTPException(
                status_code=500,
                detail={"error": "provider_error", "detail": str(exc)},
            )

        return _TokenResponseOut(
            headers=resp.headers,
            expires_at=resp.expires_at,
            cursor=resp.cursor,
        )

    return router
