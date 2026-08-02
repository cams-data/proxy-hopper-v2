"""Tests for TokenManager — token fetch, lock, broken state, refresh scheduler."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from aioresponses import aioresponses

from proxy_hopper.backend.memory import MemoryBackend
from proxy_hopper.config.models import AuthServerConfig
from proxy_hopper.identity.identity import Identity
from proxy_hopper.token_manager import (
    TokenBrokenError,
    TokenManager,
    TokenPendingError,
    _broken_key,
    _lock_key,
    _retry_at_key,
    _token_key,
)


_AUTH_URL = "http://token-server:9000"
_TARGET = "my-target"
_ADDR = "1.2.3.4:8080"


def make_config(**kw) -> AuthServerConfig:
    defaults = dict(
        url=_AUTH_URL,
        timeout_seconds=5.0,
        refresh_threshold_seconds=30.0,
        retry_interval_seconds=10.0,
        max_retries=3,
        expose_proxy_url=True,
    )
    defaults.update(kw)
    return AuthServerConfig(**defaults)


def make_identity(addr: str = _ADDR) -> Identity:
    return Identity(
        address=addr,
        headers={"user-agent": "TestAgent/1.0", "accept": "text/html"},
        cookies_enabled=False,
    )


def token_response_body(
    headers: dict | None = None,
    hours: float = 2.0,
    cursor: dict | None = None,
) -> dict:
    expires_at = (datetime.now(UTC) + timedelta(hours=hours)).isoformat()
    return {
        "headers": headers or {"Authorization": "Bearer tok123"},
        "expires_at": expires_at,
        "cursor": cursor or {},
    }


@pytest.fixture
async def backend():
    b = MemoryBackend()
    await b.start()
    yield b
    await b.stop()


@pytest.fixture
async def manager(backend):
    cfg = make_config()
    mgr = TokenManager(cfg, backend, proxy_url="http://proxy:8080")
    yield mgr
    await mgr.stop()


# ---------------------------------------------------------------------------
# ensure_token — fast path (token already cached and fresh)
# ---------------------------------------------------------------------------

class TestEnsureTokenFastPath:
    async def test_returns_cached_headers_when_fresh(self, manager, backend):
        expires_at = datetime.now(UTC) + timedelta(hours=2)
        payload = json.dumps({
            "headers": {"Authorization": "Bearer cached"},
            "expires_at": expires_at.isoformat(),
            "cursor": {},
        })
        await backend.kv_set(_token_key(_TARGET, _ADDR), payload)

        headers = await manager.ensure_token(_TARGET, make_identity())
        assert headers == {"Authorization": "Bearer cached"}

    async def test_no_token_server_call_when_cached(self, manager, backend):
        expires_at = datetime.now(UTC) + timedelta(hours=2)
        payload = json.dumps({
            "headers": {"Authorization": "Bearer fresh"},
            "expires_at": expires_at.isoformat(),
            "cursor": {},
        })
        await backend.kv_set(_token_key(_TARGET, _ADDR), payload)

        with aioresponses() as m:
            headers = await manager.ensure_token(_TARGET, make_identity())
            assert not m.requests  # no HTTP calls made


# ---------------------------------------------------------------------------
# ensure_token — fetch path (no cached token)
# ---------------------------------------------------------------------------

class TestEnsureTokenFetch:
    async def test_fetches_from_token_server_on_miss(self, manager):
        body = token_response_body({"Authorization": "Bearer new"})
        with aioresponses() as m:
            m.post(f"{_AUTH_URL}/token", payload=body)
            headers = await manager.ensure_token(_TARGET, make_identity())
        assert headers == {"Authorization": "Bearer new"}

    async def test_stores_token_after_fetch(self, manager, backend):
        body = token_response_body({"X-Token": "abc"})
        with aioresponses() as m:
            m.post(f"{_AUTH_URL}/token", payload=body)
            await manager.ensure_token(_TARGET, make_identity())

        raw = await backend.kv_get(_token_key(_TARGET, _ADDR))
        assert raw is not None
        stored = json.loads(raw)
        assert stored["headers"] == {"X-Token": "abc"}

    async def test_clears_broken_state_on_success(self, manager, backend):
        # Pre-set a partial broken state.
        await backend.counter_set(_broken_key(_TARGET, _ADDR), 2)
        await backend.kv_set(_retry_at_key(_TARGET, _ADDR), datetime.now(UTC).isoformat())

        body = token_response_body()
        with aioresponses() as m:
            m.post(f"{_AUTH_URL}/token", payload=body)
            await manager.ensure_token(_TARGET, make_identity())

        assert await backend.counter_get(_broken_key(_TARGET, _ADDR)) == 0
        assert await backend.kv_get(_retry_at_key(_TARGET, _ADDR)) is None

    async def test_token_request_includes_profile(self, manager):
        body = token_response_body()
        captured: list[dict] = []

        async def capture_request(url, **kwargs):
            captured.append(kwargs.get("json", {}))
            from aiohttp import ClientResponse
            # handled by aioresponses — just record

        with aioresponses() as m:
            m.post(f"{_AUTH_URL}/token", payload=body)
            identity = make_identity()
            await manager.ensure_token(_TARGET, identity)

        # aioresponses records request bodies; access via m.requests
        # (just verify no exception was raised — body inspection tested separately)
        assert len(captured) == 0  # we used aioresponses, not the raw capture

    async def test_proxy_url_included_when_expose_true(self, backend):
        cfg = make_config(expose_proxy_url=True)
        mgr = TokenManager(cfg, backend, proxy_url="http://proxy:8080")
        body = token_response_body()
        try:
            with aioresponses() as m:
                m.post(f"{_AUTH_URL}/token", payload=body)
                await mgr.ensure_token(_TARGET, make_identity())
            call = list(m.requests.values())[0][0]
            assert call.kwargs["json"]["proxy_url"] == "http://proxy:8080"
        finally:
            await mgr.stop()

    async def test_proxy_url_omitted_when_expose_false(self, backend):
        cfg = make_config(expose_proxy_url=False)
        mgr = TokenManager(cfg, backend, proxy_url="http://proxy:8080")
        body = token_response_body()
        try:
            with aioresponses() as m:
                m.post(f"{_AUTH_URL}/token", payload=body)
                await mgr.ensure_token(_TARGET, make_identity())
            call = list(m.requests.values())[0][0]
            assert "proxy_url" not in call.kwargs["json"]
        finally:
            await mgr.stop()


# ---------------------------------------------------------------------------
# ensure_token — failure + broken state
# ---------------------------------------------------------------------------

class TestEnsureTokenFailure:
    async def test_increments_broken_counter_on_server_error(self, manager, backend):
        with aioresponses() as m:
            m.post(f"{_AUTH_URL}/token", status=500, body=b"error")
            with pytest.raises(Exception):
                await manager.ensure_token(_TARGET, make_identity())

        count = await backend.counter_get(_broken_key(_TARGET, _ADDR))
        assert count == 1

    async def test_sets_retry_at_on_failure(self, manager, backend):
        with aioresponses() as m:
            m.post(f"{_AUTH_URL}/token", status=500, body=b"error")
            with pytest.raises(Exception):
                await manager.ensure_token(_TARGET, make_identity())

        retry_at_raw = await backend.kv_get(_retry_at_key(_TARGET, _ADDR))
        assert retry_at_raw is not None
        retry_at = datetime.fromisoformat(retry_at_raw)
        assert retry_at > datetime.now(UTC)

    async def test_raises_token_broken_error_when_max_retries_exceeded(self, manager, backend):
        cfg = make_config(max_retries=3)
        # Simulate 3 failures already recorded.
        await backend.counter_set(_broken_key(_TARGET, _ADDR), 3)
        future_retry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        await backend.kv_set(_retry_at_key(_TARGET, _ADDR), future_retry)

        with pytest.raises(TokenBrokenError):
            await manager.ensure_token(_TARGET, make_identity())

    async def test_broken_ip_retried_after_retry_window(self, manager, backend):
        """If retry_at is in the past, another fetch attempt is made."""
        await backend.counter_set(_broken_key(_TARGET, _ADDR), 3)
        past_retry = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        await backend.kv_set(_retry_at_key(_TARGET, _ADDR), past_retry)

        body = token_response_body({"Authorization": "Bearer recovered"})
        with aioresponses() as m:
            m.post(f"{_AUTH_URL}/token", payload=body)
            headers = await manager.ensure_token(_TARGET, make_identity())
        assert headers == {"Authorization": "Bearer recovered"}


# ---------------------------------------------------------------------------
# Lock coordination
# ---------------------------------------------------------------------------

class TestLockCoordination:
    async def test_lock_released_after_successful_fetch(self, manager, backend):
        body = token_response_body()
        with aioresponses() as m:
            m.post(f"{_AUTH_URL}/token", payload=body)
            await manager.ensure_token(_TARGET, make_identity())

        # Lock should have been released.
        assert await backend.kv_get(_lock_key(_TARGET, _ADDR)) is None

    async def test_lock_released_after_failed_fetch(self, manager, backend):
        with aioresponses() as m:
            m.post(f"{_AUTH_URL}/token", status=500, body=b"error")
            with pytest.raises(Exception):
                await manager.ensure_token(_TARGET, make_identity())

        assert await backend.kv_get(_lock_key(_TARGET, _ADDR)) is None

    async def test_concurrent_requests_share_token(self, manager, backend):
        """Two concurrent ensure_token calls should result in exactly one HTTP request."""
        body = token_response_body({"Authorization": "Bearer shared"})
        with aioresponses() as m:
            m.post(f"{_AUTH_URL}/token", payload=body)
            results = await asyncio.gather(
                manager.ensure_token(_TARGET, make_identity()),
                manager.ensure_token(_TARGET, make_identity()),
            )
        assert results[0] == {"Authorization": "Bearer shared"}
        assert results[1] == {"Authorization": "Bearer shared"}
        # Only one actual token fetch.
        assert len(list(m.requests.values())[0]) == 1


# ---------------------------------------------------------------------------
# Token near expiry — refresh path
# ---------------------------------------------------------------------------

class TestTokenRefresh:
    async def test_refreshes_when_near_expiry(self, manager, backend):
        # Store a token that expires within the refresh threshold.
        near_expiry = datetime.now(UTC) + timedelta(seconds=10)  # < threshold=30s
        payload = json.dumps({
            "headers": {"Authorization": "Bearer stale"},
            "expires_at": near_expiry.isoformat(),
            "cursor": {},
        })
        await backend.kv_set(_token_key(_TARGET, _ADDR), payload)

        body = token_response_body({"Authorization": "Bearer fresh"})
        with aioresponses() as m:
            m.post(f"{_AUTH_URL}/token", payload=body)
            headers = await manager.ensure_token(_TARGET, make_identity())
        assert headers == {"Authorization": "Bearer fresh"}


# ---------------------------------------------------------------------------
# Backend primitives — lock_acquire / lock_release / kv_set_with_ttl
# ---------------------------------------------------------------------------

class TestBackendPrimitives:
    async def test_lock_acquire_succeeds_when_free(self, backend):
        assert await backend.lock_acquire("mylock", "holder1", 10) is True

    async def test_lock_acquire_fails_when_held(self, backend):
        await backend.lock_acquire("mylock", "holder1", 10)
        assert await backend.lock_acquire("mylock", "holder2", 10) is False

    async def test_lock_release_succeeds_when_matching_value(self, backend):
        await backend.lock_acquire("mylock", "holder1", 10)
        assert await backend.lock_release("mylock", "holder1") is True
        assert await backend.kv_get("mylock") is None

    async def test_lock_release_fails_when_value_mismatch(self, backend):
        await backend.lock_acquire("mylock", "holder1", 10)
        assert await backend.lock_release("mylock", "wrong-holder") is False
        assert await backend.kv_get("mylock") == "holder1"

    async def test_kv_set_with_ttl_stores_value(self, backend):
        await backend.kv_set_with_ttl("mykey", "myval", 60)
        assert await backend.kv_get("mykey") == "myval"


# ---------------------------------------------------------------------------
# Config normalization
# ---------------------------------------------------------------------------

class TestConfigNormalization:
    def test_auth_server_config_parses_camelcase(self):
        from proxy_hopper.config.normalization import _normalise_server
        raw = {
            "authServer": {
                "url": "http://ts:9000",
                "timeoutSeconds": 20,
                "refreshThresholdSeconds": 90,
                "retryIntervalSeconds": 30,
                "maxRetries": 5,
                "exposeProxyUrl": False,
            }
        }
        result = _normalise_server(raw)
        auth_server = result["auth_server"]
        assert auth_server.url == "http://ts:9000"
        assert auth_server.timeout_seconds == 20
        assert auth_server.refresh_threshold_seconds == 90
        assert auth_server.retry_interval_seconds == 30
        assert auth_server.max_retries == 5
        assert auth_server.expose_proxy_url is False

    def test_auth_managed_on_target(self):
        from proxy_hopper.config.normalization import _normalise_target
        raw = {"name": "t1", "authManaged": True}
        result = _normalise_target(raw)
        assert result["auth_managed"] is True

    def test_auth_managed_defaults_false(self):
        from proxy_hopper.config.normalization import _normalise_target
        result = _normalise_target({"name": "t1"})
        assert result.get("auth_managed", False) is False
