"""Tests for the auth module — password hashing, JWT, API keys, RBAC."""

from __future__ import annotations

import time

import jwt
import pytest

from proxy_hopper.auth import (
    Permission,
    AuthenticatedUser,
    authenticate_token,
    can_access_target,
    create_access_token,
    decode_local_token,
    get_permissions,
    hash_password,
    make_runtime_secret,
    verify_password,
)
from proxy_hopper.config import (
    AdminUserConfig,
    ApiKeyConfig,
    AuthConfig,
    OidcConfig,
    RoleConfig,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_auth(
    enabled: bool = True,
    api_keys: list[dict] | None = None,
    admin: dict | None = None,
    oidc: dict | None = None,
    roles: dict | None = None,
    jwt_secret: str = "test-secret",
    jwt_expiry_minutes: int = 60,
) -> AuthConfig:
    return AuthConfig(
        enabled=enabled,
        api_keys=[ApiKeyConfig(**k) for k in (api_keys or [])],
        admin=AdminUserConfig(**admin) if admin else None,
        oidc=OidcConfig(**oidc) if oidc else None,
        roles={name: RoleConfig(**cfg) for name, cfg in (roles or {}).items()},
        jwt_secret=jwt_secret,
        jwt_expiry_minutes=jwt_expiry_minutes,
    )


_SECRET = "test-secret-32-bytes-xxxxxxxxxxxx"


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

class TestPasswordHashing:
    def test_hash_and_verify(self):
        hashed = hash_password("mysecret")
        assert verify_password("mysecret", hashed)

    def test_wrong_password_fails(self):
        hashed = hash_password("mysecret")
        assert not verify_password("wrong", hashed)

    def test_hash_is_different_each_time(self):
        # bcrypt uses a random salt
        assert hash_password("same") != hash_password("same")


# ---------------------------------------------------------------------------
# Local JWT
# ---------------------------------------------------------------------------

class TestLocalJWT:
    def test_create_and_decode(self):
        token = create_access_token("alice", "admin", _SECRET, expire_minutes=60)
        claims = decode_local_token(token, _SECRET)
        assert claims["sub"] == "alice"
        assert claims["role"] == "admin"

    def test_expired_token_raises(self):
        token = create_access_token("alice", "admin", _SECRET, expire_minutes=0)
        # expire_minutes=0 → exp == iat, already expired
        import time as _time
        _time.sleep(0.01)
        with pytest.raises(jwt.ExpiredSignatureError):
            decode_local_token(token, _SECRET)

    def test_wrong_secret_raises(self):
        token = create_access_token("alice", "admin", _SECRET)
        with pytest.raises(jwt.InvalidSignatureError):
            decode_local_token(token, "wrong-secret")

    def test_tampered_token_raises(self):
        token = create_access_token("alice", "admin", _SECRET)
        tampered = token[:-4] + "xxxx"
        with pytest.raises(jwt.InvalidTokenError):
            decode_local_token(tampered, _SECRET)


# ---------------------------------------------------------------------------
# authenticate_token
# ---------------------------------------------------------------------------

class TestAuthenticateToken:
    @pytest.mark.asyncio
    async def test_api_key_match(self):
        auth = _make_auth(api_keys=[{"name": "ci", "key": "ph_abc123", "targets": ["*"]}])
        user = await authenticate_token("ph_abc123", auth, _SECRET)
        assert user.sub == "ci"
        assert user.role == "api_key"
        assert user.is_api_key is True
        assert user.allowed_targets == ["*"]

    @pytest.mark.asyncio
    async def test_api_key_restricted_targets(self):
        auth = _make_auth(api_keys=[{"name": "scraper", "key": "ph_scraper", "targets": ["pool-a", "pool-b"]}])
        user = await authenticate_token("ph_scraper", auth, _SECRET)
        assert user.allowed_targets == ["pool-a", "pool-b"]

    @pytest.mark.asyncio
    async def test_api_key_wrong_value_skipped(self):
        auth = _make_auth(api_keys=[{"name": "ci", "key": "ph_abc123", "targets": ["*"]}])
        # Falls through to local JWT attempt, then fails with ValueError
        with pytest.raises(ValueError, match="Invalid or expired token"):
            await authenticate_token("ph_wrong", auth, _SECRET)

    @pytest.mark.asyncio
    async def test_local_jwt(self):
        token = create_access_token("bob", "viewer", _SECRET)
        auth = _make_auth()
        user = await authenticate_token(token, auth, _SECRET)
        assert user.sub == "bob"
        assert user.role == "viewer"
        assert user.is_api_key is False

    @pytest.mark.asyncio
    async def test_invalid_token_raises(self):
        auth = _make_auth()
        with pytest.raises(ValueError, match="Invalid or expired token"):
            await authenticate_token("garbage.token.value", auth, _SECRET)

    @pytest.mark.asyncio
    async def test_api_key_takes_priority_over_jwt(self):
        """An API key that happens to look like a JWT should still match as a key."""
        # Create a valid JWT
        token = create_access_token("jwt-user", "admin", _SECRET)
        # Register the same token value as an API key
        auth = _make_auth(api_keys=[{"name": "key-user", "key": token, "targets": ["*"]}])
        user = await authenticate_token(token, auth, _SECRET)
        # API key wins (checked first)
        assert user.sub == "key-user"
        assert user.role == "api_key"
        assert user.is_api_key is True


# ---------------------------------------------------------------------------
# get_permissions
# ---------------------------------------------------------------------------

class TestGetPermissions:
    def test_builtin_admin_has_all(self):
        auth = _make_auth()
        perms = get_permissions("admin", auth)
        assert perms == {Permission.read, Permission.write, Permission.admin}

    def test_builtin_operator(self):
        auth = _make_auth()
        perms = get_permissions("operator", auth)
        assert perms == {Permission.read, Permission.write}

    def test_builtin_viewer_read_only(self):
        auth = _make_auth()
        perms = get_permissions("viewer", auth)
        assert perms == {Permission.read}

    def test_unknown_role_empty_permissions(self):
        auth = _make_auth()
        perms = get_permissions("ghost", auth)
        assert perms == set()

    def test_custom_role_overrides_builtin(self):
        auth = _make_auth(roles={"admin": {"permissions": ["read"], "targets": ["*"]}})
        perms = get_permissions("admin", auth)
        # Custom definition: read only, even though built-in admin has all
        assert perms == {Permission.read}

    def test_custom_role_new_name(self):
        auth = _make_auth(roles={"scraper": {"permissions": ["read", "write"], "targets": ["pool-a"]}})
        perms = get_permissions("scraper", auth)
        assert perms == {Permission.read, Permission.write}


# ---------------------------------------------------------------------------
# can_access_target
# ---------------------------------------------------------------------------

class TestCanAccessTarget:
    # --- API key path (allowed_targets set) ---

    def test_api_key_wildcard_allows_all(self):
        auth = _make_auth()
        user = AuthenticatedUser(sub="ci", role="api_key", is_api_key=True, allowed_targets=["*"])
        assert can_access_target(user, "any-target", auth) is True

    def test_api_key_specific_target_allowed(self):
        auth = _make_auth()
        user = AuthenticatedUser(sub="ci", role="api_key", is_api_key=True, allowed_targets=["pool-a", "pool-b"])
        assert can_access_target(user, "pool-a", auth) is True

    def test_api_key_specific_target_denied(self):
        auth = _make_auth()
        user = AuthenticatedUser(sub="ci", role="api_key", is_api_key=True, allowed_targets=["pool-a"])
        assert can_access_target(user, "pool-b", auth) is False

    def test_api_key_empty_targets_denies_all(self):
        auth = _make_auth()
        user = AuthenticatedUser(sub="ci", role="api_key", is_api_key=True, allowed_targets=[])
        assert can_access_target(user, "any-target", auth) is False

    # --- JWT / OIDC path (allowed_targets is None, role-based) ---

    def test_builtin_role_allows_all(self):
        auth = _make_auth()
        user = AuthenticatedUser(sub="alice", role="admin")
        assert can_access_target(user, "any-target", auth) is True

    def test_custom_role_wildcard_allows_all(self):
        auth = _make_auth(roles={"scraper": {"permissions": ["read"], "targets": ["*"]}})
        user = AuthenticatedUser(sub="bot", role="scraper")
        assert can_access_target(user, "any-target", auth) is True

    def test_custom_role_specific_target_allowed(self):
        auth = _make_auth(roles={"scraper": {"permissions": ["read"], "targets": ["pool-a", "pool-b"]}})
        user = AuthenticatedUser(sub="bot", role="scraper")
        assert can_access_target(user, "pool-a", auth) is True

    def test_custom_role_specific_target_denied(self):
        auth = _make_auth(roles={"scraper": {"permissions": ["read"], "targets": ["pool-a"]}})
        user = AuthenticatedUser(sub="bot", role="scraper")
        assert can_access_target(user, "pool-b", auth) is False

    def test_custom_role_empty_targets_denies_all(self):
        auth = _make_auth(roles={"readonly": {"permissions": ["read"], "targets": []}})
        user = AuthenticatedUser(sub="viewer", role="readonly")
        assert can_access_target(user, "any-target", auth) is False


# ---------------------------------------------------------------------------
# make_runtime_secret
# ---------------------------------------------------------------------------

class TestMakeRuntimeSecret:
    def test_uses_configured_secret(self):
        assert make_runtime_secret("my-fixed-secret") == "my-fixed-secret"

    def test_generates_random_when_empty(self):
        s1 = make_runtime_secret("")
        s2 = make_runtime_secret("")
        assert s1 != s2
        assert len(s1) == 64  # token_hex(32) → 64 hex chars

    def test_generates_random_when_not_set(self):
        s = make_runtime_secret("")
        assert isinstance(s, str)
        assert len(s) > 0
