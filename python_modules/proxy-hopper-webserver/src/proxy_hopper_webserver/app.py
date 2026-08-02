"""FastAPI admin application for Proxy Hopper.

Serves the GraphQL API, auth endpoints, and the bundled web UI.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, OAuth2PasswordRequestForm
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

if TYPE_CHECKING:
    from proxy_hopper.app_metrics import AppMetricsStore
    from proxy_hopper.config import ProxyHopperConfig
    from proxy_hopper.repository import ProxyRepository

logger = logging.getLogger(__name__)

_UI_DIR = Path(__file__).parent / "ui"


def make_fastapi_deps(auth_config, runtime_secret: str):
    """Return ``(get_current_user, require)`` FastAPI dependency factories."""
    from proxy_hopper.auth import (
        AuthenticatedUser,
        Permission,
        authenticate_token,
        get_permissions,
    )

    bearer = HTTPBearer(auto_error=False)

    async def get_current_user(
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    ) -> AuthenticatedUser:
        if not auth_config.enabled:
            return AuthenticatedUser(sub="anonymous", role="admin", name="anonymous")
        if credentials is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            return await authenticate_token(credentials.credentials, auth_config, runtime_secret)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
                headers={"WWW-Authenticate": "Bearer"},
            )

    def require(permission: Permission):
        async def dep(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
            perms = get_permissions(user.role, auth_config)
            if permission not in perms:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Permission '{permission.value}' required",
                )
            return user
        return dep

    return get_current_user, require


def create_admin_app(
    cfg: "ProxyHopperConfig",
    runtime_secret: str,
    repo: "ProxyRepository | None" = None,
    event_bus=None,
    app_metrics: "AppMetricsStore | None" = None,
) -> FastAPI:
    """Build and return the configured FastAPI admin application."""
    from proxy_hopper.auth import Permission, create_access_token, verify_password

    app = FastAPI(
        title="Proxy Hopper Admin API",
        description="Management API for Proxy Hopper",
        version="1",
        docs_url="/docs",
        redoc_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    auth_config = cfg.auth
    get_current_user, require = make_fastapi_deps(auth_config, runtime_secret)

    @app.post("/auth/login", summary="Obtain a JWT via username and password")
    async def login(form_data: OAuth2PasswordRequestForm = Depends()):
        if not auth_config.enabled or auth_config.admin is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Local authentication is not configured",
            )
        admin = auth_config.admin
        if (
            form_data.username != admin.username
            or not verify_password(form_data.password, admin.password_hash)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect username or password",
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = create_access_token(
            sub=admin.username,
            role=admin.role,
            secret=runtime_secret,
            expire_minutes=auth_config.jwt_expiry_minutes,
        )
        return {"access_token": token, "token_type": "bearer"}

    @app.get("/health", summary="Liveness check")
    async def health():
        return {"status": "ok"}

    @app.get("/api/v1/status", summary="Server status")
    async def api_status(user=Depends(require(Permission.read))):
        return {
            "targets": [
                {"name": t.name, "regex": t.regex, "ip_count": len(t.resolved_ips)}
                for t in cfg.targets
            ],
            "backend": cfg.server.backend,
            "auth_enabled": auth_config.enabled,
            "user": {"sub": user.sub, "role": user.role},
        }

    if repo is not None:
        from .graphql import create_graphql_router
        graphql_router = create_graphql_router(
            repo=repo,
            auth_config=auth_config,
            get_current_user=get_current_user,
            app_metrics=app_metrics,
            prometheus_url=cfg.server.prometheus_url,
        )
        app.include_router(graphql_router, prefix="/graphql")
        logger.info("GraphQL API mounted at /graphql")

    if event_bus is not None:
        from .events_router import create_events_router
        events_router = create_events_router(event_bus, auth_config, runtime_secret)
        app.include_router(events_router, prefix="/events")
        logger.info("Event stream mounted at /events/stream")

    if _UI_DIR.exists():
        class _SPAFiles(StaticFiles):
            async def get_response(self, path: str, scope: dict) -> FileResponse:
                try:
                    return await super().get_response(path, scope)
                except StarletteHTTPException as exc:
                    if exc.status_code == 404:
                        return FileResponse(_UI_DIR / "index.html")
                    raise

        app.mount("/", _SPAFiles(directory=_UI_DIR), name="ui")
        logger.info("Admin UI served from %s", _UI_DIR)

    return app


async def run_admin_server(
    cfg: "ProxyHopperConfig",
    runtime_secret: str,
    repo: "ProxyRepository | None" = None,
    event_bus=None,
    app_metrics: "AppMetricsStore | None" = None,
) -> None:
    """Start the admin server as an asyncio-native task."""
    import uvicorn

    app = create_admin_app(cfg, runtime_secret, repo=repo, event_bus=event_bus, app_metrics=app_metrics)
    host = cfg.server.admin_host
    port = cfg.server.admin_port

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="error",
        access_log=False,
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None

    logger.info("Admin server listening on %s:%d", host, port)
    await server.serve()
