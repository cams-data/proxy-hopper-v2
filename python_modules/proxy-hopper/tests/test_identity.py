"""Unit tests for the proxy_hopper.identity package.

Covers:
  - FingerprintProfile and get_profile() (fingerprint.py)
  - Identity cookie/header management (identity.py)
  - IdentityStore lifecycle and rotation (store.py)
  - IdentityConfig YAML normalisation through config._normalise_target (config.py)
  - IPPool.record_failure return value and on_quarantine_release callback (pool.py)
"""

from __future__ import annotations

import asyncio
import time
from textwrap import dedent
from unittest.mock import AsyncMock, MagicMock

import pytest

from proxy_hopper.backend.memory import MemoryBackend
from proxy_hopper.pool_store import IPPoolStore
from proxy_hopper.config import _normalise_target, load_config
from proxy_hopper.identity import (
    Identity,
    IdentityConfig,
    IdentityStore,
    VALID_PROFILE_NAMES,
    WarmupConfig,
    get_profile,
)
from proxy_hopper.identity.fingerprint import PROFILES, FingerprintProfile
from proxy_hopper.pool import IPPool

from test_helpers import make_target_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_identity(
    *,
    profile_name: str = "chrome-windows",
    cookies_enabled: bool = True,
    cookies: dict | None = None,
) -> Identity:
    identity = Identity(
        profile=PROFILES[profile_name],
        cookies_enabled=cookies_enabled,
    )
    if cookies:
        identity.cookies.update(cookies)
    return identity


def _make_store(
    *,
    enabled: bool = True,
    cookies: bool = True,
    profile: str | None = None,
    rotate_after_requests: int | None = None,
    rotate_on_429: bool = True,
) -> IdentityStore:
    config = IdentityConfig(
        enabled=enabled,
        cookies=cookies,
        profile=profile,
        rotate_after_requests=rotate_after_requests,
        rotate_on_429=rotate_on_429,
    )
    return IdentityStore(target_name="test-target", config=config)


# ===========================================================================
# FingerprintProfile + get_profile
# ===========================================================================

class TestFingerprintProfile:
    def test_all_profiles_have_required_fields(self):
        for name, profile in PROFILES.items():
            assert profile.name == name
            assert profile.user_agent
            assert profile.accept
            assert profile.accept_language
            assert profile.accept_encoding

    def test_as_headers_returns_all_four_keys(self):
        profile = PROFILES["chrome-windows"]
        h = profile.as_headers()
        assert set(h.keys()) == {"user-agent", "accept", "accept-language", "accept-encoding"}

    def test_as_headers_values_match_profile(self):
        profile = PROFILES["safari-macos"]
        h = profile.as_headers()
        assert h["user-agent"] == profile.user_agent
        assert h["accept-language"] == profile.accept_language

    def test_profile_is_frozen(self):
        profile = PROFILES["chrome-windows"]
        with pytest.raises((AttributeError, TypeError)):
            profile.user_agent = "tampered"  # type: ignore[misc]

    def test_valid_profile_names_matches_profiles_keys(self):
        assert VALID_PROFILE_NAMES == frozenset(PROFILES.keys())


class TestGetProfile:
    def test_get_named_profile_returns_correct_profile(self):
        profile = get_profile("firefox-linux")
        assert profile.name == "firefox-linux"

    def test_get_all_named_profiles(self):
        for name in VALID_PROFILE_NAMES:
            p = get_profile(name)
            assert p.name == name

    def test_get_none_returns_a_profile(self):
        p = get_profile(None)
        assert isinstance(p, FingerprintProfile)
        assert p.name in VALID_PROFILE_NAMES

    def test_get_none_does_not_always_return_same_profile(self):
        # With 5 profiles, the probability of 20 calls all returning the same
        # one is (1/5)^19 ≈ 2e-14 — safe to treat as impossible.
        results = {get_profile(None).name for _ in range(20)}
        assert len(results) > 1

    def test_unknown_name_raises_key_error(self):
        with pytest.raises(KeyError, match="Unknown fingerprint profile"):
            get_profile("not-a-real-profile")

    def test_unknown_name_error_lists_valid_names(self):
        with pytest.raises(KeyError) as exc_info:
            get_profile("bad")
        assert "chrome-windows" in str(exc_info.value)


# ===========================================================================
# Identity — apply_to_headers
# ===========================================================================

class TestIdentityApplyHeaders:
    def test_injects_fingerprint_headers(self):
        identity = _make_identity(profile_name="chrome-windows")
        result = identity.apply_to_headers({})
        assert "user-agent" in result
        assert "accept" in result
        assert "accept-language" in result
        assert "accept-encoding" in result

    def test_overrides_caller_user_agent(self):
        identity = _make_identity(profile_name="chrome-windows")
        result = identity.apply_to_headers({"user-agent": "caller-ua"})
        assert result["user-agent"] == PROFILES["chrome-windows"].user_agent

    def test_preserves_non_fingerprint_headers(self):
        identity = _make_identity()
        result = identity.apply_to_headers({"x-custom": "value", "host": "example.com"})
        assert result["x-custom"] == "value"
        assert result["host"] == "example.com"

    def test_injects_cookie_header_when_cookies_stored(self):
        identity = _make_identity(cookies_enabled=True, cookies={"session": "abc", "tok": "xyz"})
        result = identity.apply_to_headers({})
        assert "cookie" in result
        # Both cookies must appear, order not guaranteed
        assert "session=abc" in result["cookie"]
        assert "tok=xyz" in result["cookie"]

    def test_no_cookie_header_when_store_empty(self):
        identity = _make_identity(cookies_enabled=True)
        assert not identity.cookies
        result = identity.apply_to_headers({})
        assert "cookie" not in result

    def test_removes_caller_cookie_when_identity_cookies_empty(self):
        """Caller's cookie must not leak into a fresh identity's session."""
        identity = _make_identity(cookies_enabled=True)
        result = identity.apply_to_headers({"cookie": "caller-session=secret"})
        assert "cookie" not in result

    def test_does_not_touch_cookie_when_cookies_disabled(self):
        identity = _make_identity(cookies_enabled=False)
        result = identity.apply_to_headers({"cookie": "caller=value"})
        # cookies_enabled=False → identity leaves cookie header alone
        assert result.get("cookie") == "caller=value"

    def test_returns_new_dict_not_mutating_original(self):
        original = {"host": "example.com"}
        identity = _make_identity()
        result = identity.apply_to_headers(original)
        assert result is not original
        assert "host" in original  # original unchanged


# ===========================================================================
# Identity — update_from_response
# ===========================================================================

class TestIdentityUpdateFromResponse:
    def test_stores_cookie_from_dict_input(self):
        identity = _make_identity(cookies_enabled=True)
        identity.update_from_response({"set-cookie": "session=abc123; Path=/"})
        assert identity.cookies["session"] == "abc123"

    def test_stores_cookie_from_raw_headers_list(self):
        """aiohttp raw_headers format: list of (bytes, bytes) tuples."""
        identity = _make_identity(cookies_enabled=True)
        identity.update_from_response([
            (b"set-cookie", b"session=abc123; Path=/"),
        ])
        assert identity.cookies["session"] == "abc123"

    def test_stores_multiple_cookies_from_list(self):
        identity = _make_identity(cookies_enabled=True)
        identity.update_from_response([
            (b"set-cookie", b"a=1"),
            (b"set-cookie", b"b=2"),
        ])
        assert identity.cookies["a"] == "1"
        assert identity.cookies["b"] == "2"

    def test_updates_existing_cookie(self):
        identity = _make_identity(cookies_enabled=True, cookies={"session": "old"})
        identity.update_from_response({"set-cookie": "session=new"})
        assert identity.cookies["session"] == "new"

    def test_removes_cookie_on_max_age_zero(self):
        identity = _make_identity(cookies_enabled=True, cookies={"session": "abc"})
        identity.update_from_response({"set-cookie": "session=; Max-Age=0"})
        assert "session" not in identity.cookies

    def test_removes_cookie_on_epoch_expires(self):
        identity = _make_identity(cookies_enabled=True, cookies={"session": "abc"})
        identity.update_from_response({
            "set-cookie": "session=; Expires=Thu, 01 Jan 1970 00:00:00 GMT"
        })
        assert "session" not in identity.cookies

    def test_ignores_other_headers_in_list(self):
        identity = _make_identity(cookies_enabled=True)
        identity.update_from_response([
            (b"content-type", b"application/json"),
            (b"set-cookie", b"tok=xyz"),
            (b"x-custom", b"irrelevant"),
        ])
        assert identity.cookies["tok"] == "xyz"
        assert len(identity.cookies) == 1

    def test_no_op_when_no_set_cookie_in_dict(self):
        identity = _make_identity(cookies_enabled=True)
        identity.update_from_response({"content-type": "application/json"})
        assert not identity.cookies

    def test_no_op_when_cookies_disabled(self):
        identity = _make_identity(cookies_enabled=False)
        identity.update_from_response({"set-cookie": "session=abc"})
        assert not identity.cookies

    def test_handles_str_tuples_in_list(self):
        """Covers str-typed raw headers (not just bytes)."""
        identity = _make_identity(cookies_enabled=True)
        identity.update_from_response([
            ("set-cookie", "session=str_value"),
        ])
        assert identity.cookies["session"] == "str_value"


# ===========================================================================
# Identity — record_request
# ===========================================================================

class TestIdentityRecordRequest:
    def test_starts_at_zero(self):
        identity = _make_identity()
        assert identity.request_count == 0

    def test_increments_on_each_call(self):
        identity = _make_identity()
        identity.record_request()
        identity.record_request()
        assert identity.request_count == 2


# ===========================================================================
# IdentityStore
# ===========================================================================

class TestIdentityStoreGetOrCreate:
    def test_creates_identity_on_first_call(self):
        store = _make_store()
        identity = store.get_or_create("1.2.3.4:8080")
        assert isinstance(identity, Identity)

    def test_returns_same_object_on_subsequent_calls(self):
        store = _make_store()
        first = store.get_or_create("1.2.3.4:8080")
        second = store.get_or_create("1.2.3.4:8080")
        assert first is second

    def test_different_addresses_get_different_identities(self):
        store = _make_store()
        a = store.get_or_create("1.1.1.1:8080")
        b = store.get_or_create("2.2.2.2:8080")
        assert a is not b

    def test_identity_cookies_enabled_matches_config(self):
        store = _make_store(cookies=True)
        identity = store.get_or_create("1.2.3.4:8080")
        assert identity.cookies_enabled is True

    def test_identity_cookies_disabled_when_config_false(self):
        store = _make_store(cookies=False)
        identity = store.get_or_create("1.2.3.4:8080")
        assert identity.cookies_enabled is False

    def test_identity_uses_specified_profile(self):
        store = _make_store(profile="safari-macos")
        identity = store.get_or_create("1.2.3.4:8080")
        assert identity.profile.name == "safari-macos"

    def test_identity_uses_random_profile_when_none(self):
        store = _make_store(profile=None)
        identity = store.get_or_create("1.2.3.4:8080")
        assert identity.profile.name in VALID_PROFILE_NAMES


class TestIdentityStoreRotate:
    def test_rotate_returns_new_identity(self):
        store = _make_store()
        original = store.get_or_create("1.2.3.4:8080")
        rotated = store.rotate("1.2.3.4:8080", reason="test")
        assert rotated is not original

    def test_get_or_create_after_rotate_returns_new_identity(self):
        store = _make_store()
        original = store.get_or_create("1.2.3.4:8080")
        store.rotate("1.2.3.4:8080", reason="test")
        current = store.get_or_create("1.2.3.4:8080")
        assert current is not original

    def test_rotate_discards_cookies(self):
        store = _make_store()
        identity = store.get_or_create("1.2.3.4:8080")
        identity.cookies["session"] = "old-session"
        store.rotate("1.2.3.4:8080", reason="test")
        new_identity = store.get_or_create("1.2.3.4:8080")
        assert not new_identity.cookies

    def test_rotate_on_unknown_address_creates_fresh(self):
        store = _make_store()
        identity = store.rotate("9.9.9.9:8080", reason="test")
        assert isinstance(identity, Identity)
        assert identity.request_count == 0

    def test_rotate_fresh_identity_has_zero_request_count(self):
        store = _make_store()
        identity = store.get_or_create("1.2.3.4:8080")
        identity.record_request()
        identity.record_request()
        new_identity = store.rotate("1.2.3.4:8080", reason="test")
        assert new_identity.request_count == 0

    def test_different_addresses_rotate_independently(self):
        store = _make_store()
        a = store.get_or_create("1.1.1.1:8080")
        b = store.get_or_create("2.2.2.2:8080")
        store.rotate("1.1.1.1:8080", reason="test")
        assert store.get_or_create("2.2.2.2:8080") is b


class TestIdentityStoreNeedsRotation:
    def test_returns_false_when_limit_not_configured(self):
        store = _make_store(rotate_after_requests=None)
        store.get_or_create("1.2.3.4:8080")
        assert store.needs_rotation("1.2.3.4:8080") is False

    def test_returns_false_before_limit(self):
        store = _make_store(rotate_after_requests=5)
        identity = store.get_or_create("1.2.3.4:8080")
        for _ in range(4):
            identity.record_request()
        assert store.needs_rotation("1.2.3.4:8080") is False

    def test_returns_true_at_limit(self):
        store = _make_store(rotate_after_requests=3)
        identity = store.get_or_create("1.2.3.4:8080")
        for _ in range(3):
            identity.record_request()
        assert store.needs_rotation("1.2.3.4:8080") is True

    def test_returns_true_above_limit(self):
        store = _make_store(rotate_after_requests=3)
        identity = store.get_or_create("1.2.3.4:8080")
        for _ in range(10):
            identity.record_request()
        assert store.needs_rotation("1.2.3.4:8080") is True

    def test_returns_false_for_unknown_address(self):
        store = _make_store(rotate_after_requests=5)
        assert store.needs_rotation("never-seen:8080") is False

    def test_returns_false_after_rotation_resets_count(self):
        store = _make_store(rotate_after_requests=3)
        identity = store.get_or_create("1.2.3.4:8080")
        for _ in range(3):
            identity.record_request()
        assert store.needs_rotation("1.2.3.4:8080") is True
        store.rotate("1.2.3.4:8080", reason="request_limit")
        assert store.needs_rotation("1.2.3.4:8080") is False


# ===========================================================================
# IdentityConfig — defaults and validation
# ===========================================================================

class TestIdentityConfig:
    def test_defaults_disabled(self):
        cfg = IdentityConfig()
        assert cfg.enabled is False

    def test_defaults_cookies_true(self):
        cfg = IdentityConfig()
        assert cfg.cookies is True

    def test_defaults_rotate_on_429_true(self):
        cfg = IdentityConfig()
        assert cfg.rotate_on_429 is True

    def test_defaults_no_rotation_limit(self):
        cfg = IdentityConfig()
        assert cfg.rotate_after_requests is None

    def test_defaults_no_profile(self):
        cfg = IdentityConfig()
        assert cfg.profile is None

    def test_defaults_no_warmup(self):
        cfg = IdentityConfig()
        assert cfg.warmup is None

    def test_rotate_after_requests_must_be_at_least_one(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            IdentityConfig(rotate_after_requests=0)

    def test_warmup_defaults(self):
        w = WarmupConfig()
        assert w.enabled is True
        assert w.path == "/"


# ===========================================================================
# config._normalise_target — identity YAML block
# ===========================================================================

class TestNormaliseTargetIdentity:
    def _raw_target(self, **identity_overrides) -> dict:
        raw = {
            "name": "test",
            "regex": ".*",
            "ipList": ["1.2.3.4:8080"],
        }
        if identity_overrides is not None:
            raw["identity"] = identity_overrides
        return raw

    def test_identity_block_absent_gives_default_config(self):
        raw = {"name": "t", "regex": ".*", "ipList": ["1.2.3.4:8080"]}
        out = _normalise_target(raw)
        assert "identity" not in out  # default applied by TargetConfig

    def test_identity_block_parsed_to_identity_config(self):
        raw = self._raw_target(enabled=True, cookies=False)
        out = _normalise_target(raw)
        cfg = out["identity"]
        assert isinstance(cfg, IdentityConfig)
        assert cfg.enabled is True
        assert cfg.cookies is False

    def test_camel_case_rotate_after_requests(self):
        raw = self._raw_target(enabled=True, rotateAfterRequests=100)
        out = _normalise_target(raw)
        assert out["identity"].rotate_after_requests == 100

    def test_camel_case_rotate_on_429(self):
        raw = self._raw_target(enabled=True, rotateOn429=False)
        out = _normalise_target(raw)
        assert out["identity"].rotate_on_429 is False

    def test_profile_field_passed_through(self):
        raw = self._raw_target(enabled=True, profile="firefox-linux")
        out = _normalise_target(raw)
        assert out["identity"].profile == "firefox-linux"

    def test_warmup_block_parsed(self):
        raw = self._raw_target(enabled=True, warmup={"enabled": True, "path": "/ping"})
        out = _normalise_target(raw)
        warmup = out["identity"].warmup
        assert isinstance(warmup, WarmupConfig)
        assert warmup.path == "/ping"


# ===========================================================================
# TargetConfig — identity field defaults
# ===========================================================================

class TestTargetConfigIdentityField:
    def test_default_identity_is_disabled(self):
        cfg = make_target_config(["1.2.3.4:8080"])
        assert cfg.identity.enabled is False

    def test_identity_config_can_be_set(self):
        id_cfg = IdentityConfig(enabled=True, profile="chrome-macos")
        cfg = make_target_config(["1.2.3.4:8080"], identity=id_cfg)
        assert cfg.identity.enabled is True
        assert cfg.identity.profile == "chrome-macos"


# ===========================================================================
# IPPool.record_failure return value
# ===========================================================================

class TestIPPoolRecordFailureReturnValue:
    @pytest.fixture
    async def pool(self):
        cfg = make_target_config(
            ["1.2.3.4:8080"],
            ip_failures_until_quarantine=3,
            quarantine_time=60.0,
            min_request_interval=0.0,
        )
        raw_backend = MemoryBackend()
        await raw_backend.start()
        backend = IPPoolStore(raw_backend)
        p = IPPool(cfg, backend)
        await p.start()
        yield p
        await p.stop()
        await raw_backend.stop()

    async def test_returns_false_before_threshold(self, pool):
        result = await pool.record_failure("1.2.3.4:8080")
        assert result is False

    async def test_returns_false_one_before_threshold(self, pool):
        await pool.record_failure("1.2.3.4:8080")
        result = await pool.record_failure("1.2.3.4:8080")
        assert result is False

    async def test_returns_true_at_threshold(self, pool):
        await pool.record_failure("1.2.3.4:8080")
        await pool.record_failure("1.2.3.4:8080")
        result = await pool.record_failure("1.2.3.4:8080")
        assert result is True

    async def test_returns_true_above_threshold(self, pool):
        for _ in range(4):
            result = await pool.record_failure("1.2.3.4:8080")
        assert result is True


# ===========================================================================
# IPPool.on_quarantine_release callback
# ===========================================================================

class TestIPPoolQuarantineReleaseCallback:
    async def test_callback_not_called_when_none(self):
        cfg = make_target_config(
            ["1.2.3.4:8080"],
            ip_failures_until_quarantine=1,
            quarantine_time=0.05,
            min_request_interval=0.0,
        )
        raw_backend = MemoryBackend()
        await raw_backend.start()
        pool_store = IPPoolStore(raw_backend)
        p = IPPool(cfg, pool_store, sweep_interval=0.02)
        await p.start()
        await p.record_failure("1.2.3.4:8080")
        await asyncio.sleep(0.15)
        await p.stop()
        await raw_backend.stop()
        # No assertion needed — just must not raise

    async def test_callback_called_when_ip_released(self):
        released: list[str] = []

        async def on_release(address: str) -> None:
            released.append(address)

        cfg = make_target_config(
            ["1.2.3.4:8080"],
            ip_failures_until_quarantine=1,
            quarantine_time=0.05,
            min_request_interval=0.0,
        )
        raw_backend = MemoryBackend()
        await raw_backend.start()
        pool_store = IPPoolStore(raw_backend)
        p = IPPool(cfg, pool_store, sweep_interval=0.02, on_quarantine_release=on_release)
        await p.start()
        await p.record_failure("1.2.3.4:8080")
        await asyncio.sleep(0.2)
        await p.stop()
        await raw_backend.stop()

        assert released == ["1.2.3.4:8080"]

    async def test_callback_called_before_ip_returns_to_pool(self):
        """Callback fires before push_ips so fresh identity is ready on acquire."""
        call_order: list[str] = []

        async def on_release(address: str) -> None:
            call_order.append("callback")

        cfg = make_target_config(
            ["1.2.3.4:8080"],
            ip_failures_until_quarantine=1,
            quarantine_time=0.05,
            min_request_interval=0.0,
        )
        raw_backend = MemoryBackend()
        await raw_backend.start()
        pool_store = IPPoolStore(raw_backend)

        p = IPPool(cfg, pool_store, sweep_interval=0.02, on_quarantine_release=on_release)
        await p.start()

        # Patch after start so the seed push_ips call is not recorded.
        original_push = pool_store.push_ips
        async def instrumented_push(target, addresses):
            call_order.append("push_ips")
            return await original_push(target, addresses)
        pool_store.push_ips = instrumented_push

        await p.record_failure("1.2.3.4:8080")
        await asyncio.sleep(0.2)
        await p.stop()
        await raw_backend.stop()

        assert call_order == ["callback", "push_ips"]

    async def test_multiple_releases_callback_called_per_address(self):
        released: list[str] = []

        async def on_release(address: str) -> None:
            released.append(address)

        cfg = make_target_config(
            ["1.2.3.4:8080", "5.6.7.8:8080"],
            ip_failures_until_quarantine=1,
            quarantine_time=0.05,
            min_request_interval=0.0,
        )
        raw_backend = MemoryBackend()
        await raw_backend.start()
        pool_store = IPPoolStore(raw_backend)
        p = IPPool(cfg, pool_store, sweep_interval=0.02, on_quarantine_release=on_release)
        await p.start()
        await p.record_failure("1.2.3.4:8080")
        await p.record_failure("5.6.7.8:8080")
        await asyncio.sleep(0.2)
        await p.stop()
        await raw_backend.stop()

        assert sorted(released) == ["1.2.3.4:8080", "5.6.7.8:8080"]
