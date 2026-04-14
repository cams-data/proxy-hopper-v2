"""FastAPI admin API for Proxy Hopper.

Runs on a separate port from the proxy server (default 8081, configurable
via ``server.adminPort`` / ``PROXY_HOPPER_ADMIN_PORT``).

Endpoints
---------
POST /auth/login
    Exchange username + password for a short-lived JWT.  Only available when
    ``auth.enabled: true`` and ``auth.admin`` is configured.

GET /health
    Public liveness check — always returns ``{"status": "ok"}``.

GET /api/v1/status
    Basic server status (target list, backend type).
    Requires ``read`` permission.

This module is the foundation for the GraphQL management API; future routes
will be registered on the same ``app`` instance.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm

from .auth import (
    AuthenticatedUser,
    Permission,
    create_access_token,
    make_fastapi_deps,
    verify_password,  # re-exported from auth for admin login use
)

if TYPE_CHECKING:
    from .config import ProxyHopperConfig

logger = logging.getLogger(__name__)


def create_admin_app(cfg: "ProxyHopperConfig", runtime_secret: str) -> FastAPI:
    """Build and return the configured FastAPI admin application.

    *cfg* is captured at construction time; it reflects the state of the
    config file at startup.  Live mutations (once GraphQL is added) will
    operate on shared in-memory or Redis state, not on this snapshot.
    """
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

    # ------------------------------------------------------------------
    # Auth routes
    # ------------------------------------------------------------------

    @app.post("/auth/login", summary="Obtain a JWT via username and password")
    async def login(form_data: OAuth2PasswordRequestForm = Depends()):
        """Authenticate with local credentials and receive a Bearer JWT.

        Returns ``404`` when local authentication is not configured, so
        deployments using OIDC exclusively do not expose a login surface.
        """
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

    # ------------------------------------------------------------------
    # Public routes
    # ------------------------------------------------------------------

    @app.get("/health", summary="Liveness check")
    async def health():
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # Protected routes
    # ------------------------------------------------------------------

    @app.get("/api/v1/status", summary="Server status")
    async def api_status(user: AuthenticatedUser = Depends(require(Permission.read))):
        """Return basic server state.  Requires ``read`` permission."""
        return {
            "targets": [
                {
                    "name": t.name,
                    "regex": t.regex,
                    "ip_count": len(t.resolved_ips),
                }
                for t in cfg.targets
            ],
            "backend": cfg.server.backend,
            "auth_enabled": auth_config.enabled,
            "user": {"sub": user.sub, "role": user.role},
        }

    return app


async def run_admin_server(cfg: "ProxyHopperConfig", runtime_secret: str) -> None:
    """Start the admin API server as an asyncio-native task.

    Uses ``uvicorn.Server`` directly so the admin app runs inside the same
    event loop as the proxy server — no threads, no subprocesses.
    Signal handling is delegated to the main process.
    """
    import uvicorn

    app = create_admin_app(cfg, runtime_secret)
    host = cfg.server.admin_host
    port = cfg.server.admin_port

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="error",   # proxy-hopper controls its own logging
        access_log=False,
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # main process owns signals

    logger.info("Admin API listening on %s:%d", host, port)
    await server.serve()
