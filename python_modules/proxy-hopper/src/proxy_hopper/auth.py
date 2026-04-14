"""Authentication and authorisation for Proxy Hopper.

Three credential types are supported and tried in order on every request:

1. **API keys** — opaque strings configured under ``auth.apiKeys``.
   Used for machine-to-machine proxy access (CI pipelines, scripts).
   Clients send ``X-Proxy-Hopper-Auth: Bearer <key>`` on proxy requests.
   API keys are **not** accepted by the admin API.
   Each key carries its own ``targets`` list; ``["*"]`` (default) allows all.

2. **Local JWT** — HS256 tokens issued by the admin ``POST /auth/login``
   endpoint after a successful username/password login.

3. **OIDC JWT** — RS256/ES256 tokens issued by an external provider
   (Authentik, Keycloak, Auth0, …) when ``auth.oidc`` is configured.
   Both browser SSO tokens (authorization code flow) and service account
   tokens (client credentials flow) validate through the same JWKS path.

Roles (JWT/OIDC only)
----------------------
Built-in roles and their permissions:

admin    : read, write, admin   — full access including admin API
operator : read, write          — can use the proxy and mutate config
viewer   : read                 — read-only access to status and metrics

Custom roles can be defined under ``auth.roles`` in the YAML config to
restrict which targets a role can access.

Target access
-------------
*API keys*: each key has a ``targets`` list.  ``"*"`` means all targets.

*JWT / OIDC users*: access is controlled by the role's ``targets`` list.
Custom roles may restrict to named targets; built-in roles allow all.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, TYPE_CHECKING

import bcrypt as _bcrypt
import jwt as _jwt

if TYPE_CHECKING:
    from .config import AuthConfig, OidcConfig


# ---------------------------------------------------------------------------
# Permission model and built-in role mapping
# ---------------------------------------------------------------------------

class Permission(str, Enum):
    read = "read"
    write = "write"
    admin = "admin"


_BUILTIN_ROLE_PERMISSIONS: dict[str, set[Permission]] = {
    "admin":    {Permission.read, Permission.write, Permission.admin},
    "operator": {Permission.read, Permission.write},
    "viewer":   {Permission.read},
}


# ---------------------------------------------------------------------------
# Authenticated user
# ---------------------------------------------------------------------------

@dataclass
class AuthenticatedUser:
    sub: str                                # username, API key name, or OIDC subject
    role: str                               # role name; "api_key" for API key users
    name: str = ""
    is_api_key: bool = False
    allowed_targets: list[str] | None = None  # set for API keys; None = use role


# ---------------------------------------------------------------------------
# Password hashing (bcrypt)
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    """Return a bcrypt hash of *plain* as a UTF-8 string."""
    return _bcrypt.hashpw(plain.encode(), _bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if *plain* matches the bcrypt *hashed* value."""
    return _bcrypt.checkpw(plain.encode(), hashed.encode())


# ---------------------------------------------------------------------------
# Local JWT — HS256, issued on username/password login
# ---------------------------------------------------------------------------

_LOCAL_ALGORITHM = "HS256"


def create_access_token(sub: str, role: str, secret: str, expire_minutes: int = 60) -> str:
    """Sign and return a short-lived JWT for *sub* with the given *role*."""
    now = int(time.time())
    payload = {
        "sub": sub,
        "role": role,
        "iat": now,
        "exp": now + expire_minutes * 60,
    }
    return _jwt.encode(payload, secret, algorithm=_LOCAL_ALGORITHM)


def decode_local_token(token: str, secret: str) -> dict:
    """Decode a locally-issued JWT.  Raises ``jwt.InvalidTokenError`` on failure."""
    return _jwt.decode(token, secret, algorithms=[_LOCAL_ALGORITHM])


# ---------------------------------------------------------------------------
# OIDC JWT — RS256/ES256, issued by external provider
# ---------------------------------------------------------------------------

# Simple in-memory JWKS cache: issuer → (fetched_at_monotonic, jwks_dict)
_jwks_cache: dict[str, tuple[float, dict]] = {}
_JWKS_TTL = 300.0  # 5 minutes


async def _get_jwks(issuer: str) -> dict:
    """Return the JWKS for *issuer*, fetching from the discovery document if stale."""
    import httpx

    now = time.monotonic()
    cached = _jwks_cache.get(issuer)
    if cached and now - cached[0] < _JWKS_TTL:
        return cached[1]

    discovery_url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    async with httpx.AsyncClient() as client:
        resp = await client.get(discovery_url, timeout=10.0)
        resp.raise_for_status()
        jwks_uri = resp.json()["jwks_uri"]
        jwks_resp = await client.get(jwks_uri, timeout=10.0)
        jwks_resp.raise_for_status()
        jwks = jwks_resp.json()

    _jwks_cache[issuer] = (now, jwks)
    return jwks


async def verify_oidc_token(token: str, oidc_config: "OidcConfig") -> dict:
    """Validate an OIDC JWT against the provider's JWKS and return its claims."""
    jwks = await _get_jwks(oidc_config.issuer)

    header = _jwt.get_unverified_header(token)
    kid = header.get("kid")
    alg = header.get("alg", "RS256")

    public_key = None
    for jwk in jwks.get("keys", []):
        if kid is None or jwk.get("kid") == kid:
            from jwt.algorithms import ECAlgorithm, RSAAlgorithm
            if alg.startswith("RS"):
                public_key = RSAAlgorithm.from_jwk(jwk)
            elif alg.startswith("ES"):
                public_key = ECAlgorithm.from_jwk(jwk)
            break

    if public_key is None:
        raise _jwt.InvalidTokenError(f"No matching JWK for kid={kid!r}")

    decode_kwargs: dict = {
        "algorithms": ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
    }
    if oidc_config.audience:
        decode_kwargs["audience"] = oidc_config.audience

    return _jwt.decode(token, public_key, **decode_kwargs)


# ---------------------------------------------------------------------------
# Token → AuthenticatedUser resolution
# ---------------------------------------------------------------------------

async def authenticate_token(
    token: str,
    auth_config: "AuthConfig",
    runtime_secret: str,
) -> AuthenticatedUser:
    """Resolve a raw Bearer token string to an ``AuthenticatedUser``.

    Tries API key → local JWT → OIDC JWT in order.
    Raises ``ValueError`` with a human-readable message if none match.
    """
    # 1. API key exact match
    for key_cfg in auth_config.api_keys:
        if key_cfg.key == token:
            return AuthenticatedUser(
                sub=key_cfg.name,
                role="api_key",
                name=key_cfg.name,
                is_api_key=True,
                allowed_targets=list(key_cfg.targets),
            )

    # 2. Local JWT (HS256)
    try:
        claims = decode_local_token(token, runtime_secret)
        return AuthenticatedUser(
            sub=claims["sub"],
            role=claims.get("role", "viewer"),
            name=claims.get("sub", ""),
        )
    except _jwt.InvalidTokenError:
        pass

    # 3. OIDC JWT (RS256 / ES256)
    if auth_config.oidc is not None:
        try:
            claims = await verify_oidc_token(token, auth_config.oidc)
            role = claims.get(auth_config.oidc.roles_claim, "viewer")
            return AuthenticatedUser(
                sub=claims.get("sub", ""),
                role=role,
                name=claims.get("name", claims.get("sub", "")),
            )
        except Exception:
            pass

    raise ValueError("Invalid or expired token")


# ---------------------------------------------------------------------------
# Permission and target access helpers
# ---------------------------------------------------------------------------

def get_permissions(role: str, auth_config: "AuthConfig") -> set[Permission]:
    """Return the permission set for *role*, checking custom config before built-ins."""
    custom = auth_config.roles.get(role)
    if custom is not None:
        return {Permission(p) for p in custom.permissions}
    return _BUILTIN_ROLE_PERMISSIONS.get(role, set())


def can_access_target(
    user: AuthenticatedUser,
    target_name: str,
    auth_config: "AuthConfig",
) -> bool:
    """Return True if *user* is permitted to access *target_name*.

    API keys carry their own ``allowed_targets`` list.  JWT/OIDC users are
    checked against the target list of their assigned role.
    """
    # API keys: use the per-key target list
    if user.allowed_targets is not None:
        return "*" in user.allowed_targets or target_name in user.allowed_targets
    # JWT / OIDC: check via role
    custom = auth_config.roles.get(user.role)
    if custom is None:
        # Built-in roles allow all targets
        return True
    return "*" in custom.targets or target_name in custom.targets


# ---------------------------------------------------------------------------
# Runtime secret
# ---------------------------------------------------------------------------

def make_runtime_secret(configured_secret: str) -> str:
    """Return the configured JWT signing secret, or generate a random one.

    A random secret means sessions do not survive a server restart.
    Set ``auth.jwtSecret`` in config for persistent sessions.
    """
    return configured_secret if configured_secret else secrets.token_hex(32)


# ---------------------------------------------------------------------------
# FastAPI dependency factories
# ---------------------------------------------------------------------------

def make_fastapi_deps(auth_config: "AuthConfig", runtime_secret: str):
    """Return ``(get_current_user, require)`` FastAPI dependency factories.

    Called once at admin app startup.  *auth_config* and *runtime_secret* are
    captured in closures so they do not need to be injected per-request.

    Usage::

        get_current_user, require = make_fastapi_deps(cfg.auth, secret)

        @app.get("/protected")
        async def protected(user = Depends(require(Permission.write))):
            ...
    """
    from fastapi import Depends, HTTPException, status
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

    bearer = HTTPBearer(auto_error=False)

    async def get_current_user(
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    ) -> AuthenticatedUser:
        if not auth_config.enabled:
            # Auth disabled — grant anonymous admin access so the API is usable
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
        """Return a FastAPI dependency that enforces *permission*."""
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
