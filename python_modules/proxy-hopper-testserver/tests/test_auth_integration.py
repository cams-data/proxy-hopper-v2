"""Integration tests for authentication in the ForwardingHandler.

Tests run through the full HTTP request lifecycle:
  HTTP client → ProxyServer (ForwardingHandler + auth) → MockProxy → UpstreamServer

This verifies auth behaviour that cannot be tested at unit level, because it
requires a real TCP connection parsed by ProxyServer, with the ForwardingHandler
auth check gating whether the request is dispatched or a 401/403 is returned.

Test matrix
-----------
Auth disabled:
  - no auth_config → all requests pass through
  - auth.enabled=False → all requests pass through
  - auth disabled + nonsense token → still passes (token ignored)

Missing / malformed token:
  - no X-Proxy-Hopper-Auth header → 401
  - wrong key value → 401
  - garbage Bearer value → 401

API key auth:
  - valid key, targets=["*"] → 200
  - valid key, named target matching the server's target → 200
  - valid key, named target NOT matching → 403
  - valid key, empty targets list → 403
  - second of two registered keys → 200

JWT auth (locally issued HS256 tokens):
  - valid JWT → 200
  - expired JWT → 401
  - JWT signed with wrong secret → 401
  - JWT with viewer role → 200 (viewer has read permission)

Target access control (JWT with custom roles):
  - custom role permitted on this target → 200
  - custom role NOT permitted on this target → 403
  - built-in admin role → 200 on any target name
"""

from __future__ import annotations

import time

import aiohttp
import pytest
import pytest_asyncio

from proxy_hopper.auth import create_access_token
from proxy_hopper.backend.memory import MemoryIPPoolBackend
from proxy_hopper.config import ApiKeyConfig, AuthConfig, RoleConfig
from proxy_hopper.server import ProxyServer
from proxy_hopper.target_manager import TargetManager
from proxy_hopper_testserver import MockProxyPool, UpstreamServer

from conftest import make_target_config

# HS256 requires a secret of at least 32 bytes; use exactly 32 ASCII chars.
_SECRET = "test-secret-32-bytes-xxxxxxxxxxxx"


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

async def _start_proxy(
    proxies: MockProxyPool,
    auth_config: AuthConfig | None = None,
    runtime_secret: str = _SECRET,
    target_name: str = "general",
) -> tuple[ProxyServer, int]:
    """Start a ProxyServer on an ephemeral port.

    Returns ``(server, port)``.  Caller must call ``await server.stop()`` to
    clean up.  ``ProxyServer.start()`` also starts the TargetManager and its
    backend, so no separate backend lifecycle is needed here.
    """
    cfg = make_target_config(ip_list=proxies.ip_list, name=target_name)
    backend = MemoryIPPoolBackend()
    mgr = TargetManager(cfg, backend)
    server = ProxyServer(
        [mgr],
        host="127.0.0.1",
        port=0,
        auth_config=auth_config,
        runtime_secret=runtime_secret,
    )
    await server.start()
    port = server._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return server, port


async def _get(
    port: int,
    path: str,
    *,
    target_url: str,
    token: str | None = None,
) -> tuple[int, str]:
    """Make a forwarding-mode GET request and return ``(status, body_text)``.

    Forwarding mode: set ``X-Proxy-Hopper-Target`` and send the request
    directly to the proxy port — proxy-hopper owns the full HTTPS lifecycle.
    """
    headers = {"X-Proxy-Hopper-Target": target_url}
    if token is not None:
        headers["X-Proxy-Hopper-Auth"] = f"Bearer {token}"
    async with aiohttp.ClientSession() as client:
        async with client.get(
            f"http://127.0.0.1:{port}{path}",
            headers=headers,
            allow_redirects=False,
        ) as resp:
            return resp.status, await resp.text()


def _make_auth(**kwargs) -> AuthConfig:
    """Build an AuthConfig with ``enabled=True`` and a fixed ``jwt_secret``."""
    defaults: dict = {"enabled": True, "jwt_secret": _SECRET}
    defaults.update(kwargs)
    return AuthConfig(**defaults)


def _api_key(
    name: str = "ci",
    key: str = "ph_test_key",
    targets: list[str] | None = None,
) -> ApiKeyConfig:
    return ApiKeyConfig(name=name, key=key, targets=targets if targets is not None else ["*"])


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def proxies() -> MockProxyPool:
    """Two mock proxies in forward mode."""
    async with MockProxyPool(count=2) as pool:
        yield pool


@pytest_asyncio.fixture
async def upstream() -> UpstreamServer:
    """Upstream server in normal mode."""
    async with UpstreamServer() as server:
        server.set_mode("normal")
        yield server
        server.reset()


# ---------------------------------------------------------------------------
# Auth disabled
# ---------------------------------------------------------------------------

class TestAuthDisabled:
    async def test_no_auth_config_allows_all_requests(self, proxies, upstream):
        server, port = await _start_proxy(proxies, auth_config=None)
        try:
            status, _ = await _get(port, "/hello", target_url=upstream.url)
            assert status == 200
        finally:
            await server.stop()

    async def test_auth_enabled_false_allows_all_requests(self, proxies, upstream):
        auth = _make_auth(enabled=False)
        server, port = await _start_proxy(proxies, auth_config=auth)
        try:
            status, _ = await _get(port, "/hello", target_url=upstream.url)
            assert status == 200
        finally:
            await server.stop()

    async def test_auth_disabled_ignores_nonsense_token(self, proxies, upstream):
        """When auth is off, a garbage token in the header should not block the request."""
        auth = _make_auth(enabled=False)
        server, port = await _start_proxy(proxies, auth_config=auth)
        try:
            status, _ = await _get(port, "/hello", target_url=upstream.url, token="total_garbage")
            assert status == 200
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# Missing / malformed token
# ---------------------------------------------------------------------------

class TestMissingOrBadToken:
    async def test_no_auth_header_returns_401(self, proxies, upstream):
        auth = _make_auth(api_keys=[_api_key()])
        server, port = await _start_proxy(proxies, auth_config=auth)
        try:
            status, body = await _get(port, "/test", target_url=upstream.url)
            assert status == 401
            assert "Authentication required" in body
        finally:
            await server.stop()

    async def test_wrong_key_returns_401(self, proxies, upstream):
        auth = _make_auth(api_keys=[_api_key(key="ph_correct")])
        server, port = await _start_proxy(proxies, auth_config=auth)
        try:
            status, _ = await _get(port, "/test", target_url=upstream.url, token="ph_wrong")
            assert status == 401
        finally:
            await server.stop()

    async def test_garbage_bearer_value_returns_401(self, proxies, upstream):
        auth = _make_auth(api_keys=[_api_key()])
        server, port = await _start_proxy(proxies, auth_config=auth)
        try:
            status, _ = await _get(port, "/test", target_url=upstream.url, token="not.a.valid.token.at.all")
            assert status == 401
        finally:
            await server.stop()

    async def test_empty_bearer_value_returns_401(self, proxies, upstream):
        """X-Proxy-Hopper-Auth: Bearer <empty> should be treated as missing."""
        auth = _make_auth(api_keys=[_api_key()])
        server, port = await _start_proxy(proxies, auth_config=auth)
        try:
            # Send the header with an empty token value
            headers = {
                "X-Proxy-Hopper-Target": upstream.url,
                "X-Proxy-Hopper-Auth": "Bearer ",
            }
            async with aiohttp.ClientSession() as client:
                async with client.get(
                    f"http://127.0.0.1:{port}/test",
                    headers=headers,
                    allow_redirects=False,
                ) as resp:
                    assert resp.status == 401
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# API key auth
# ---------------------------------------------------------------------------

class TestApiKeyAuth:
    async def test_valid_key_returns_200(self, proxies, upstream):
        auth = _make_auth(api_keys=[_api_key(key="ph_valid")])
        server, port = await _start_proxy(proxies, auth_config=auth)
        try:
            status, _ = await _get(port, "/ok", target_url=upstream.url, token="ph_valid")
            assert status == 200
        finally:
            await server.stop()

    async def test_wildcard_targets_allows_any_target_name(self, proxies, upstream):
        auth = _make_auth(api_keys=[_api_key(key="ph_wild", targets=["*"])])
        server, port = await _start_proxy(proxies, auth_config=auth, target_name="some-specific-target")
        try:
            status, _ = await _get(port, "/test", target_url=upstream.url, token="ph_wild")
            assert status == 200
        finally:
            await server.stop()

    async def test_named_target_matching_server_target_returns_200(self, proxies, upstream):
        auth = _make_auth(api_keys=[_api_key(key="ph_named", targets=["general"])])
        server, port = await _start_proxy(proxies, auth_config=auth, target_name="general")
        try:
            status, _ = await _get(port, "/test", target_url=upstream.url, token="ph_named")
            assert status == 200
        finally:
            await server.stop()

    async def test_named_target_not_matching_server_target_returns_403(self, proxies, upstream):
        auth = _make_auth(api_keys=[_api_key(name="restricted-key", key="ph_restricted", targets=["other-target"])])
        # Server has target named "general", key only allows "other-target" → denied
        server, port = await _start_proxy(proxies, auth_config=auth, target_name="general")
        try:
            status, body = await _get(port, "/test", target_url=upstream.url, token="ph_restricted")
            assert status == 403
            # Error message includes the key's name (sub) and the target name
            assert "restricted-key" in body
            assert "general" in body
        finally:
            await server.stop()

    async def test_empty_targets_list_returns_403(self, proxies, upstream):
        auth = _make_auth(api_keys=[_api_key(key="ph_empty", targets=[])])
        server, port = await _start_proxy(proxies, auth_config=auth)
        try:
            status, _ = await _get(port, "/test", target_url=upstream.url, token="ph_empty")
            assert status == 403
        finally:
            await server.stop()

    async def test_second_of_two_keys_is_accepted(self, proxies, upstream):
        """Key lookup must check all registered keys, not just the first."""
        auth = _make_auth(api_keys=[
            _api_key(name="first", key="ph_first"),
            _api_key(name="second", key="ph_second"),
        ])
        server, port = await _start_proxy(proxies, auth_config=auth)
        try:
            status, _ = await _get(port, "/test", target_url=upstream.url, token="ph_second")
            assert status == 200
        finally:
            await server.stop()

    async def test_api_key_cannot_spoof_as_jwt(self, proxies, upstream):
        """A JWT-shaped string that is not a registered key should fall through to JWT
        validation and fail (wrong issuer / wrong secret)."""
        auth = _make_auth(api_keys=[_api_key(key="ph_only_key")])
        server, port = await _start_proxy(proxies, auth_config=auth)
        # Craft a valid JWT with a *different* secret — not a registered key
        foreign_token = create_access_token("attacker", "admin", "wrong-secret-xxxxxxxxxxxxxxxxxx")
        try:
            status, _ = await _get(port, "/test", target_url=upstream.url, token=foreign_token)
            assert status == 401
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# JWT auth (locally issued HS256 tokens)
# ---------------------------------------------------------------------------

class TestJwtAuth:
    async def test_valid_jwt_returns_200(self, proxies, upstream):
        auth = _make_auth()
        server, port = await _start_proxy(proxies, auth_config=auth)
        token = create_access_token("alice", "operator", _SECRET)
        try:
            status, _ = await _get(port, "/test", target_url=upstream.url, token=token)
            assert status == 200
        finally:
            await server.stop()

    async def test_viewer_role_can_use_proxy(self, proxies, upstream):
        """viewer has read permission — allowed to make proxy requests."""
        auth = _make_auth()
        server, port = await _start_proxy(proxies, auth_config=auth)
        token = create_access_token("bob", "viewer", _SECRET)
        try:
            status, _ = await _get(port, "/test", target_url=upstream.url, token=token)
            assert status == 200
        finally:
            await server.stop()

    async def test_expired_jwt_returns_401(self, proxies, upstream):
        auth = _make_auth()
        server, port = await _start_proxy(proxies, auth_config=auth)
        # expire_minutes=0 → exp == iat, immediately expired
        token = create_access_token("alice", "operator", _SECRET, expire_minutes=0)
        time.sleep(0.05)  # ensure clock has advanced past exp
        try:
            status, _ = await _get(port, "/test", target_url=upstream.url, token=token)
            assert status == 401
        finally:
            await server.stop()

    async def test_jwt_signed_with_wrong_secret_returns_401(self, proxies, upstream):
        auth = _make_auth()
        server, port = await _start_proxy(proxies, auth_config=auth, runtime_secret=_SECRET)
        bad_token = create_access_token("alice", "operator", "wrong-secret-xxxxxxxxxxxxxxxxxx")
        try:
            status, _ = await _get(port, "/test", target_url=upstream.url, token=bad_token)
            assert status == 401
        finally:
            await server.stop()

    async def test_unknown_role_has_no_permissions_returns_403(self, proxies, upstream):
        """A JWT with an unrecognised role name gets empty permissions → 403."""
        auth = _make_auth()
        server, port = await _start_proxy(proxies, auth_config=auth)
        token = create_access_token("ghost", "unknown_role", _SECRET)
        try:
            status, _ = await _get(port, "/test", target_url=upstream.url, token=token)
            assert status == 403
        finally:
            await server.stop()


# ---------------------------------------------------------------------------
# Target access control via JWT roles
# ---------------------------------------------------------------------------

class TestJwtTargetAccess:
    async def test_builtin_admin_role_allows_any_target(self, proxies, upstream):
        auth = _make_auth()
        server, port = await _start_proxy(proxies, auth_config=auth, target_name="special")
        token = create_access_token("admin-user", "admin", _SECRET)
        try:
            status, _ = await _get(port, "/test", target_url=upstream.url, token=token)
            assert status == 200
        finally:
            await server.stop()

    async def test_custom_role_permitted_on_target_returns_200(self, proxies, upstream):
        auth = AuthConfig(
            enabled=True,
            jwt_secret=_SECRET,
            roles={
                "scraper": RoleConfig(permissions=["read", "write"], targets=["general"]),
            },
        )
        server, port = await _start_proxy(proxies, auth_config=auth, target_name="general")
        token = create_access_token("bot", "scraper", _SECRET)
        try:
            status, _ = await _get(port, "/test", target_url=upstream.url, token=token)
            assert status == 200
        finally:
            await server.stop()

    async def test_custom_role_not_permitted_on_target_returns_403(self, proxies, upstream):
        auth = AuthConfig(
            enabled=True,
            jwt_secret=_SECRET,
            roles={
                "scraper": RoleConfig(permissions=["read", "write"], targets=["allowed-target"]),
            },
        )
        # Server target is "general"; role only allows "allowed-target" → denied
        server, port = await _start_proxy(proxies, auth_config=auth, target_name="general")
        token = create_access_token("bot", "scraper", _SECRET)
        try:
            status, body = await _get(port, "/test", target_url=upstream.url, token=token)
            assert status == 403
            assert "scraper" in body
        finally:
            await server.stop()

    async def test_custom_role_wildcard_targets_allows_all(self, proxies, upstream):
        auth = AuthConfig(
            enabled=True,
            jwt_secret=_SECRET,
            roles={
                "any-reader": RoleConfig(permissions=["read"], targets=["*"]),
            },
        )
        server, port = await _start_proxy(proxies, auth_config=auth, target_name="whatever")
        token = create_access_token("reader", "any-reader", _SECRET)
        try:
            status, _ = await _get(port, "/test", target_url=upstream.url, token=token)
            assert status == 200
        finally:
            await server.stop()

    async def test_custom_role_empty_targets_denies_all(self, proxies, upstream):
        auth = AuthConfig(
            enabled=True,
            jwt_secret=_SECRET,
            roles={
                "locked": RoleConfig(permissions=["read", "write"], targets=[]),
            },
        )
        server, port = await _start_proxy(proxies, auth_config=auth, target_name="general")
        token = create_access_token("locked-user", "locked", _SECRET)
        try:
            status, _ = await _get(port, "/test", target_url=upstream.url, token=token)
            assert status == 403
        finally:
            await server.stop()
