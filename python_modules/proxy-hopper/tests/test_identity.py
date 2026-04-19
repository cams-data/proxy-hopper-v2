"""Unit tests for the proxy_hopper.identity package and IdentityQueue.

Covers:
  - FingerprintProfile and get_profile() (fingerprint.py)
  - Identity cookie/header management and serialisation (identity.py)
  - IdentityQueue lifecycle, acquire, record_success/failure, rotate (pool.py)
  - IdentityConfig YAML normalisation through config._normalise_target (config.py)
"""

from __future__ import annotations

import asyncio
from textwrap import dedent

import pytest

from proxy_hopper.backend.memory import MemoryBackend
from proxy_hopper.pool_store import IPPoolStore
from proxy_hopper.config import _normalise_target, load_config
from proxy_hopper.identity import (
    Identity,
    IdentityConfig,
    WarmupConfig,
    FingerprintProfile,
    get_profile,
)
from proxy_hopper.pool import IdentityQueue

from test_helpers import make_target_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_identity(
    *,
    address: str = "1.2.3.4:8080",
    cookies_enabled: bool = True,
    cookies: dict | None = None,
    headers: dict | None = None,
) -> Identity:
    identity = Identity(
        address=address,
        headers=headers or {
            "user-agent": "Mozilla/5.0 TestBrowser/1.0",
            "accept": "text/html",
            "accept-language": "en-US,en;q=0.9",
            "accept-encoding": "gzip, deflate",
        },
        cookies_enabled=cookies_enabled,
    )
    if cookies:
        identity.cookies.update(cookies)
    return identity


async def _make_queue(
    ip_list: list[str] | None = None,
    *,
    identity_enabled: bool = False,
    cookies: bool = True,
    ip_failures_until_quarantine: int = 3,
    quarantine_time: float = 60.0,
    min_request_interval: float = 0.0,
    rotate_after_requests: int | None = None,
    sweep_interval: float = 0.02,
) -> tuple[IdentityQueue, MemoryBackend]:
    cfg = make_target_config(
        ip_list or ["1.2.3.4:8080"],
        ip_failures_until_quarantine=ip_failures_until_quarantine,
        quarantine_time=quarantine_time,
        min_request_interval=min_request_interval,
        identity=IdentityConfig(
            enabled=identity_enabled,
            cookies=cookies,
            rotate_after_requests=rotate_after_requests,
        ),
    )
    raw_backend = MemoryBackend()
    await raw_backend.start()
    backend = IPPoolStore(raw_backend)
    q = IdentityQueue(cfg, backend, sweep_interval=sweep_interval)
    await q.start()
    return q, raw_backend


# ===========================================================================
# FingerprintProfile + get_profile
# ===========================================================================

class TestFingerprintProfile:
    def test_as_headers_returns_all_four_keys(self):
        profile = get_profile()
        h = profile.as_headers()
        assert set(h.keys()) == {"user-agent", "accept", "accept-language", "accept-encoding"}

    def test_as_headers_values_are_non_empty_strings(self):
        profile = get_profile()
        for key, value in profile.as_headers().items():
            assert isinstance(value, str) and value, f"{key!r} was empty"

    def test_profile_is_frozen(self):
        profile = get_profile()
        with pytest.raises((AttributeError, TypeError)):
            profile.user_agent = "tampered"  # type: ignore[misc]

    def test_get_profile_returns_fingerprint_profile(self):
        p = get_profile()
        assert isinstance(p, FingerprintProfile)

    def test_get_profile_does_not_always_return_same_ua(self):
        # With 5 platforms × rolling version range, 20 calls almost certainly
        # produce >1 distinct User-Agent string.
        uas = {get_profile().user_agent for _ in range(20)}
        assert len(uas) > 1

    def test_user_agent_contains_mozilla(self):
        # All valid browser UAs start with "Mozilla/5.0"
        for _ in range(10):
            ua = get_profile().user_agent
            assert ua.startswith("Mozilla/5.0"), f"Unexpected UA: {ua!r}"


# ===========================================================================
# Identity — serialisation
# ===========================================================================

class TestIdentitySerialization:
    def test_round_trip_preserves_address(self):
        identity = _make_identity(address="10.0.0.1:3128")
        restored = Identity.from_dict(identity.to_dict())
        assert restored.address == "10.0.0.1:3128"

    def test_round_trip_preserves_headers(self):
        hdrs = {"user-agent": "TestUA", "accept": "*/*", "accept-language": "en", "accept-encoding": "gzip"}
        identity = _make_identity(headers=hdrs)
        restored = Identity.from_dict(identity.to_dict())
        assert restored.headers == hdrs

    def test_round_trip_preserves_cookies_enabled(self):
        identity = _make_identity(cookies_enabled=False)
        restored = Identity.from_dict(identity.to_dict())
        assert restored.cookies_enabled is False

    def test_round_trip_preserves_cookies(self):
        identity = _make_identity(cookies={"session": "abc", "tok": "xyz"})
        restored = Identity.from_dict(identity.to_dict())
        assert restored.cookies == {"session": "abc", "tok": "xyz"}

    def test_round_trip_preserves_request_count(self):
        identity = _make_identity()
        identity.record_request()
        identity.record_request()
        restored = Identity.from_dict(identity.to_dict())
        assert restored.request_count == 2

    def test_to_dict_is_json_serialisable(self):
        import json
        identity = _make_identity(cookies={"s": "v"})
        # Must not raise
        json.dumps(identity.to_dict())

    def test_from_dict_tolerates_missing_optional_fields(self):
        minimal = {"address": "1.2.3.4:8080", "cookies_enabled": False}
        identity = Identity.from_dict(minimal)
        assert identity.address == "1.2.3.4:8080"
        assert identity.request_count == 0
        assert identity.cookies == {}
        assert identity.headers == {}


# ===========================================================================
# Identity — apply_to_headers
# ===========================================================================

class TestIdentityApplyHeaders:
    def test_injects_fingerprint_headers(self):
        identity = _make_identity()
        result = identity.apply_to_headers({})
        assert "user-agent" in result
        assert "accept" in result
        assert "accept-language" in result
        assert "accept-encoding" in result

    def test_overrides_caller_user_agent(self):
        identity = _make_identity(headers={"user-agent": "IdentityUA", "accept": "*/*",
                                           "accept-language": "en", "accept-encoding": "gzip"})
        result = identity.apply_to_headers({"user-agent": "caller-ua"})
        assert result["user-agent"] == "IdentityUA"

    def test_preserves_non_fingerprint_headers(self):
        identity = _make_identity()
        result = identity.apply_to_headers({"x-custom": "value", "host": "example.com"})
        assert result["x-custom"] == "value"
        assert result["host"] == "example.com"

    def test_injects_cookie_header_when_cookies_stored(self):
        identity = _make_identity(cookies_enabled=True, cookies={"session": "abc", "tok": "xyz"})
        result = identity.apply_to_headers({})
        assert "cookie" in result
        assert "session=abc" in result["cookie"]
        assert "tok=xyz" in result["cookie"]

    def test_no_cookie_header_when_store_empty(self):
        identity = _make_identity(cookies_enabled=True)
        assert not identity.cookies
        result = identity.apply_to_headers({})
        assert "cookie" not in result

    def test_removes_caller_cookie_when_identity_cookies_empty(self):
        identity = _make_identity(cookies_enabled=True)
        result = identity.apply_to_headers({"cookie": "caller-session=secret"})
        assert "cookie" not in result

    def test_does_not_touch_cookie_when_cookies_disabled(self):
        identity = _make_identity(cookies_enabled=False, headers={})
        result = identity.apply_to_headers({"cookie": "caller=value"})
        assert result.get("cookie") == "caller=value"

    def test_returns_new_dict_not_mutating_original(self):
        original = {"host": "example.com"}
        identity = _make_identity()
        result = identity.apply_to_headers(original)
        assert result is not original
        assert "host" in original

    def test_null_identity_empty_headers_passes_through(self):
        """Null identity (identity disabled) — headers={}, cookies off."""
        identity = Identity(address="1.2.3.4:8080", headers={}, cookies_enabled=False)
        original = {"host": "example.com", "x-custom": "value"}
        result = identity.apply_to_headers(original)
        assert result["host"] == "example.com"
        assert result["x-custom"] == "value"
        assert "user-agent" not in result


# ===========================================================================
# Identity — update_from_response
# ===========================================================================

class TestIdentityUpdateFromResponse:
    def test_stores_cookie_from_dict_input(self):
        identity = _make_identity(cookies_enabled=True)
        identity.update_from_response({"set-cookie": "session=abc123; Path=/"})
        assert identity.cookies["session"] == "abc123"

    def test_stores_cookie_from_raw_headers_list(self):
        identity = _make_identity(cookies_enabled=True)
        identity.update_from_response([(b"set-cookie", b"session=abc123; Path=/")])
        assert identity.cookies["session"] == "abc123"

    def test_stores_multiple_cookies_from_list(self):
        identity = _make_identity(cookies_enabled=True)
        identity.update_from_response([(b"set-cookie", b"a=1"), (b"set-cookie", b"b=2")])
        assert identity.cookies["a"] == "1"
        assert identity.cookies["b"] == "2"

    def test_removes_cookie_on_max_age_zero(self):
        identity = _make_identity(cookies_enabled=True, cookies={"session": "abc"})
        identity.update_from_response({"set-cookie": "session=; Max-Age=0"})
        assert "session" not in identity.cookies

    def test_removes_cookie_on_epoch_expires(self):
        identity = _make_identity(cookies_enabled=True, cookies={"session": "abc"})
        identity.update_from_response({"set-cookie": "session=; Expires=Thu, 01 Jan 1970 00:00:00 GMT"})
        assert "session" not in identity.cookies

    def test_no_op_when_cookies_disabled(self):
        identity = _make_identity(cookies_enabled=False)
        identity.update_from_response({"set-cookie": "session=abc"})
        assert not identity.cookies


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
# IdentityQueue — basic acquire / success / failure
# ===========================================================================

class TestIdentityQueueAcquire:
    async def test_acquire_returns_uuid_and_identity(self):
        q, backend = await _make_queue()
        try:
            result = await q.acquire(1.0)
            assert result is not None
            uuid, identity = result
            assert isinstance(uuid, str) and len(uuid) == 36  # UUIDv4
            assert isinstance(identity, Identity)
            assert identity.address == "1.2.3.4:8080"
        finally:
            await q.stop()
            await backend.stop()

    async def test_acquire_returns_none_on_empty_queue(self):
        q, backend = await _make_queue()
        try:
            # Drain the one identity
            await q.acquire(0.1)
            # Queue is now empty
            result = await q.acquire(0.05)
            assert result is None
        finally:
            await q.stop()
            await backend.stop()

    async def test_multiple_addresses_each_get_identity(self):
        q, backend = await _make_queue(["1.1.1.1:8080", "2.2.2.2:8080", "3.3.3.3:8080"])
        try:
            addresses = set()
            for _ in range(3):
                result = await q.acquire(1.0)
                assert result is not None
                _, identity = result
                addresses.add(identity.address)
            assert addresses == {"1.1.1.1:8080", "2.2.2.2:8080", "3.3.3.3:8080"}
        finally:
            await q.stop()
            await backend.stop()

    async def test_null_identity_when_disabled(self):
        """When identity disabled, headers={} and cookies_enabled=False."""
        q, backend = await _make_queue(identity_enabled=False)
        try:
            result = await q.acquire(1.0)
            assert result is not None
            _, identity = result
            assert identity.headers == {}
            assert identity.cookies_enabled is False
        finally:
            await q.stop()
            await backend.stop()

    async def test_identity_enabled_has_fingerprint_headers(self):
        q, backend = await _make_queue(identity_enabled=True)
        try:
            result = await q.acquire(1.0)
            assert result is not None
            _, identity = result
            assert "user-agent" in identity.headers
        finally:
            await q.stop()
            await backend.stop()


class TestIdentityQueueRecordSuccess:
    async def test_uuid_returned_to_queue_after_success(self):
        q, backend = await _make_queue(min_request_interval=0.0)
        try:
            result = await q.acquire(1.0)
            assert result is not None
            uuid, identity = result

            await q.record_success(uuid, identity, elapsed=0.0)
            await asyncio.sleep(0.05)  # let cooldown task run

            # UUID should be back in queue
            result2 = await q.acquire(0.1)
            assert result2 is not None
            uuid2, _ = result2
            assert uuid2 == uuid
        finally:
            await q.stop()
            await backend.stop()

    async def test_updated_cookies_persisted_after_success(self):
        q, backend = await _make_queue(identity_enabled=True, cookies=True, min_request_interval=0.0)
        try:
            result = await q.acquire(1.0)
            assert result is not None
            uuid, identity = result

            identity.update_from_response({"set-cookie": "tok=abc"})
            await q.record_success(uuid, identity, elapsed=0.0)
            await asyncio.sleep(0.05)

            # Re-acquire same UUID — cookies should be preserved
            result2 = await q.acquire(0.1)
            assert result2 is not None
            _, identity2 = result2
            assert identity2.cookies.get("tok") == "abc"
        finally:
            await q.stop()
            await backend.stop()


class TestIdentityQueueRecordFailure:
    async def test_returns_false_before_threshold(self):
        q, backend = await _make_queue(ip_failures_until_quarantine=3, min_request_interval=0.0)
        try:
            result = await q.acquire(1.0)
            assert result is not None
            uuid, identity = result
            was_quarantined = await q.record_failure(uuid, identity, 0.0)
            assert was_quarantined is False
        finally:
            await q.stop()
            await backend.stop()

    async def test_returns_false_one_before_threshold(self):
        q, backend = await _make_queue(ip_failures_until_quarantine=3, min_request_interval=0.0)
        try:
            for _ in range(2):
                result = await q.acquire(1.0)
                assert result is not None
                uuid, identity = result
                was_quarantined = await q.record_failure(uuid, identity, 0.0)
            assert was_quarantined is False
        finally:
            await q.stop()
            await backend.stop()

    async def test_returns_true_at_threshold(self):
        q, backend = await _make_queue(ip_failures_until_quarantine=3, min_request_interval=0.0)
        try:
            was_quarantined = False
            for _ in range(3):
                result = await q.acquire(1.0)
                assert result is not None
                uuid, identity = result
                was_quarantined = await q.record_failure(uuid, identity, 0.0)
            assert was_quarantined is True
        finally:
            await q.stop()
            await backend.stop()

    async def test_uuid_not_returned_when_quarantined(self):
        q, backend = await _make_queue(
            ip_failures_until_quarantine=1,
            quarantine_time=60.0,
            min_request_interval=0.0,
        )
        try:
            result = await q.acquire(1.0)
            assert result is not None
            uuid, identity = result
            await q.record_failure(uuid, identity, 0.0)
            # Queue should be empty — UUID was not returned
            result2 = await q.acquire(0.05)
            assert result2 is None
        finally:
            await q.stop()
            await backend.stop()

    async def test_uuid_returned_when_not_quarantined(self):
        q, backend = await _make_queue(
            ip_failures_until_quarantine=3,
            min_request_interval=0.0,
        )
        try:
            result = await q.acquire(1.0)
            assert result is not None
            uuid, identity = result
            await q.record_failure(uuid, identity, 0.0)
            await asyncio.sleep(0.05)

            result2 = await q.acquire(0.1)
            assert result2 is not None
            uuid2, _ = result2
            assert uuid2 == uuid
        finally:
            await q.stop()
            await backend.stop()


class TestIdentityQueueQuarantineRelease:
    async def test_fresh_identity_pushed_on_release(self):
        q, backend = await _make_queue(
            ip_failures_until_quarantine=1,
            quarantine_time=0.05,
            min_request_interval=0.0,
            sweep_interval=0.02,
        )
        try:
            result = await q.acquire(1.0)
            assert result is not None
            uuid_orig, identity = result
            await q.record_failure(uuid_orig, identity, 0.0)

            # Wait for quarantine to expire and sweep to run
            await asyncio.sleep(0.2)

            # A new identity should be in the queue
            result2 = await q.acquire(0.5)
            assert result2 is not None
            uuid_new, identity2 = result2
            # New UUID — fresh identity
            assert uuid_new != uuid_orig
            assert identity2.address == "1.2.3.4:8080"
        finally:
            await q.stop()
            await backend.stop()

    async def test_retired_address_not_returned_after_quarantine(self):
        q, backend = await _make_queue(
            ip_failures_until_quarantine=1,
            quarantine_time=0.05,
            min_request_interval=0.0,
            sweep_interval=0.02,
        )
        try:
            result = await q.acquire(1.0)
            assert result is not None
            uuid, identity = result
            await q.record_failure(uuid, identity, 0.0)
            # Retire before quarantine releases
            await q.retire_address("1.2.3.4:8080")
            # Wait for sweep
            await asyncio.sleep(0.2)
            # Queue should remain empty — retired address discarded
            result2 = await q.acquire(0.05)
            assert result2 is None
        finally:
            await q.stop()
            await backend.stop()


class TestIdentityQueueRotate:
    async def test_rotate_creates_new_uuid(self):
        q, backend = await _make_queue(identity_enabled=True, min_request_interval=0.0)
        try:
            result = await q.acquire(1.0)
            assert result is not None
            uuid_orig, identity = result

            await q.rotate(uuid_orig, identity, elapsed=0.0)
            await asyncio.sleep(0.05)

            result2 = await q.acquire(0.5)
            assert result2 is not None
            uuid_new, _ = result2
            assert uuid_new != uuid_orig
        finally:
            await q.stop()
            await backend.stop()

    async def test_rotate_creates_fresh_cookies(self):
        q, backend = await _make_queue(identity_enabled=True, cookies=True, min_request_interval=0.0)
        try:
            result = await q.acquire(1.0)
            assert result is not None
            uuid_orig, identity = result

            identity.cookies["session"] = "old-session"
            await q.rotate(uuid_orig, identity, elapsed=0.0)
            await asyncio.sleep(0.05)

            result2 = await q.acquire(0.5)
            assert result2 is not None
            _, new_identity = result2
            assert not new_identity.cookies
        finally:
            await q.stop()
            await backend.stop()


class TestIdentityQueueRetire:
    async def test_retire_discards_identity_on_next_pop(self):
        q, backend = await _make_queue(["1.1.1.1:8080", "2.2.2.2:8080"], min_request_interval=0.0)
        try:
            # Retire one address before anyone acquires it
            await q.retire_address("1.1.1.1:8080")
            # Drain queue — retired address should be skipped
            addresses = set()
            for _ in range(2):
                result = await q.acquire(0.5)
                if result is not None:
                    _, identity = result
                    addresses.add(identity.address)
                    await q.record_success(result[0], identity, 0.0)
                    await asyncio.sleep(0.05)
            assert "1.1.1.1:8080" not in addresses
        finally:
            await q.stop()
            await backend.stop()

    async def test_add_address_creates_identity_and_enqueues(self):
        q, backend = await _make_queue(["1.1.1.1:8080"])
        try:
            # Drain existing identity
            r1 = await q.acquire(1.0)
            assert r1 is not None

            # Add a new address at runtime
            await q.add_address("5.5.5.5:9090")

            result = await q.acquire(0.5)
            assert result is not None
            _, identity = result
            assert identity.address == "5.5.5.5:9090"
        finally:
            await q.stop()
            await backend.stop()


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

    def test_defaults_no_warmup(self):
        cfg = IdentityConfig()
        assert cfg.warmup is None

    def test_no_profile_field(self):
        """profile field has been removed — passing it should be silently ignored."""
        # Pydantic v2 BaseModel ignores unknown fields by default
        cfg = IdentityConfig()
        assert not hasattr(cfg, "profile")

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
            "ipPool": "pool-a",
        }
        if identity_overrides:
            raw["identity"] = identity_overrides
        return raw

    def test_identity_block_absent_gives_default_config(self):
        raw = {"name": "t", "regex": ".*", "ipPool": "pool-a"}
        out = _normalise_target(raw)
        assert "identity" not in out

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

    def test_warmup_block_parsed(self):
        raw = self._raw_target(enabled=True, warmup={"enabled": True, "path": "/ping"})
        out = _normalise_target(raw)
        warmup = out["identity"].warmup
        assert isinstance(warmup, WarmupConfig)
        assert warmup.path == "/ping"

    def test_profile_field_silently_ignored(self):
        """profile is no longer a supported field — should not cause an error."""
        raw = self._raw_target(enabled=True, profile="firefox-linux")
        out = _normalise_target(raw)
        assert isinstance(out["identity"], IdentityConfig)
        assert not hasattr(out["identity"], "profile")


# ===========================================================================
# TargetConfig — identity field defaults
# ===========================================================================

class TestTargetConfigIdentityField:
    def test_default_identity_is_disabled(self):
        cfg = make_target_config(["1.2.3.4:8080"])
        assert cfg.identity.enabled is False

    def test_identity_config_can_be_set(self):
        id_cfg = IdentityConfig(enabled=True, cookies=False)
        cfg = make_target_config(["1.2.3.4:8080"], identity=id_cfg)
        assert cfg.identity.enabled is True
        assert cfg.identity.cookies is False
