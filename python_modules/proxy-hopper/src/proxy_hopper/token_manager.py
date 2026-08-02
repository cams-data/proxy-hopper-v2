"""TokenManager — per-(target, IP) auth token lifecycle management.

Responsibilities
----------------
- On startup: pre-warm tokens for all auth-managed (target, ip) pairs.
- Before each proxied request: return current valid headers for (target, ip),
  blocking briefly if a refresh is in progress.
- Lock coordination: Redis SET NX prevents multiple replicas from refreshing
  the same token simultaneously; waiting replicas piggyback on the result.
- Failure tracking: increments a broken counter on each token server error;
  quarantines the IP after ``max_retries`` consecutive failures.
- Background scheduler: proactively refreshes tokens nearing expiry and
  retries broken IPs after ``retry_interval_seconds``.

Redis key layout (all under ``ph:auth:``)
------------------------------------------
ph:auth:token:{target}:{addr}   KV  JSON {headers, expires_at, cursor}  TTL=expires+10m
ph:auth:lock:{target}:{addr}    KV  lock holder UUID                     TTL=30s
ph:auth:broken:{target}:{addr}  KV  int string (consecutive failures)    no TTL
ph:auth:retry_at:{target}:{addr} KV ISO8601 UTC timestamp                no TTL

where ``addr`` is ``{ip}:{port}`` (e.g. ``1.2.3.4:8080``).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid as _uuid_mod
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import aiohttp

from .logging_config import get_logger
from .metrics import get_metrics

if TYPE_CHECKING:
    from .backend.base import Backend
    from .config.models import AuthServerConfig, TargetConfig
    from .identity.identity import Identity

logger = get_logger(__name__)

_AUTH_PREFIX = "ph:auth"
_LOCK_TTL = 45          # seconds — must exceed token server timeout
_POLL_INTERVAL = 0.2    # seconds between piggyback polls
_POLL_TIMEOUT = 8.0     # max seconds to wait for another replica's refresh


def _token_key(target: str, addr: str) -> str:
    return f"{_AUTH_PREFIX}:token:{target}:{addr}"


def _lock_key(target: str, addr: str) -> str:
    return f"{_AUTH_PREFIX}:lock:{target}:{addr}"


def _broken_key(target: str, addr: str) -> str:
    return f"{_AUTH_PREFIX}:broken:{target}:{addr}"


def _retry_at_key(target: str, addr: str) -> str:
    return f"{_AUTH_PREFIX}:retry_at:{target}:{addr}"


class TokenBrokenError(Exception):
    """Raised when an IP is in AUTH_BROKEN state and cannot provide a token."""


class TokenPendingError(Exception):
    """Raised when no token has been fetched yet (PENDING state)."""


class TokenManager:
    """Manages auth token lifecycle for all auth-managed (target, ip) pairs."""

    def __init__(
        self,
        config: AuthServerConfig,
        backend: Backend,
        proxy_url: str | None = None,
    ) -> None:
        self._config = config
        self._backend = backend
        self._proxy_url = proxy_url
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._scheduler_task: asyncio.Task | None = None
        # Tracks all (target, addr) pairs seen; used by the refresh scheduler.
        self._known: set[tuple[str, str]] = set()
        # Local best-effort counter for the auth_broken_ips gauge (per-replica).
        self._broken_ips: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, targets: list[TargetConfig]) -> None:
        """Initialise the HTTP session and kick off pre-warm + scheduler."""
        self._session = aiohttp.ClientSession()
        self._running = True

        # Register all auth-managed (target, addr) pairs.
        for tc in targets:
            if tc.auth_managed:
                for ip in tc.resolved_ips:
                    self._known.add((tc.name, ip.address))

        # Health check probe — non-fatal; just warns if unreachable.
        await self._probe_health()

        # Pre-warm in background — proxy starts accepting requests immediately.
        if self._known:
            logger.info(
                "TokenManager: pre-warm started for %d auth-managed pairs",
                len(self._known),
                extra={"event": "token_prewarm_started", "pair_count": len(self._known)},
            )
            asyncio.create_task(
                self._prewarm(list(self._known)),
                name="ph:token:prewarm",
            )

        self._scheduler_task = asyncio.create_task(
            self._refresh_scheduler(),
            name="ph:token:scheduler",
        )
        logger.info(
            "TokenManager: started (url=%s, %d auth-managed pairs)",
            self._config.url, len(self._known),
        )

    async def stop(self) -> None:
        self._running = False
        if self._scheduler_task:
            self._scheduler_task.cancel()
            await asyncio.gather(self._scheduler_task, return_exceptions=True)
            self._scheduler_task = None
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None
        logger.info("TokenManager: stopped")

    async def _probe_health(self) -> None:
        """GET /health on the token server at startup. Non-fatal — logs only."""
        url = f"{self._config.url}/health"
        try:
            async with self._get_session().get(
                url,
                timeout=aiohttp.ClientTimeout(total=self._config.timeout_seconds),
            ) as resp:
                if resp.status < 500:
                    logger.info(
                        "TokenManager: token server health check OK (status=%d, url=%s)",
                        resp.status, url,
                    )
                else:
                    logger.warning(
                        "TokenManager: token server health check returned %d (url=%s)",
                        resp.status, url,
                    )
        except Exception as exc:
            logger.warning(
                "TokenManager: token server health check failed (url=%s): %s",
                url, exc,
            )

    # ------------------------------------------------------------------
    # Public interface (called by TargetManager per request)
    # ------------------------------------------------------------------

    async def ensure_token(
        self,
        target: str,
        identity: Identity,
    ) -> dict[str, str]:
        """Return headers to inject for (target, ip). May block briefly.

        Raises TokenBrokenError when the IP is in AUTH_BROKEN state.
        Raises TokenPendingError when no token has been fetched yet.
        """
        addr = identity.address
        self._known.add((target, addr))

        # Fast path: token exists and is not near expiry.
        stored = await self._read_token(target, addr)
        if stored is not None:
            headers, expires_at, _ = stored
            threshold = self._config.refresh_threshold_seconds
            remaining = (expires_at - datetime.now(UTC)).total_seconds()
            if remaining > threshold:
                return headers
            # Token near expiry — fall through to refresh.
            logger.debug(
                "TokenManager: token refresh triggered for %s/%s (%.0fs until expiry)",
                target, addr, remaining,
                extra={
                    "event": "token_refresh_triggered",
                    "target": target, "ip": addr,
                    "seconds_until_expiry": remaining,
                },
            )

        # Check broken state.
        broken_count = await self._backend.counter_get(_broken_key(target, addr))
        if broken_count >= self._config.max_retries:
            # Check if retry window has elapsed.
            retry_at_raw = await self._backend.kv_get(_retry_at_key(target, addr))
            if retry_at_raw is None or datetime.now(UTC) < datetime.fromisoformat(retry_at_raw):
                raise TokenBrokenError(
                    f"IP {addr!r} is in AUTH_BROKEN state for target {target!r}"
                )
            # Retry window elapsed — attempt recovery below.

        # Try to acquire the refresh lock.
        lock_id = str(_uuid_mod.uuid4())
        acquired = await self._backend.lock_acquire(
            _lock_key(target, addr), lock_id, _LOCK_TTL
        )

        if acquired:
            try:
                return await self._fetch_and_store(target, identity)
            finally:
                await self._backend.lock_release(_lock_key(target, addr), lock_id)
        else:
            # Another replica holds the lock — piggyback on their result.
            return await self._poll_for_token(target, addr, stored)

    # ------------------------------------------------------------------
    # Token fetch + storage
    # ------------------------------------------------------------------

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _fetch_and_store(
        self, target: str, identity: Identity
    ) -> dict[str, str]:
        """Call the token server and persist the result. Lock must be held by caller."""
        addr = identity.address
        host, port_str = addr.rsplit(":", 1)
        metrics = get_metrics()
        refresh_start = time.monotonic()

        # Load the existing cursor (if any).
        stored = await self._read_token(target, addr)
        cursor = stored[2] if stored is not None else {}

        # Build the request body.
        profile_headers = identity.headers or {}
        body = {
            "target": target,
            "ip": host,
            "port": int(port_str),
            "cursor": cursor,
            "profile": {
                "user_agent": profile_headers.get("user-agent", ""),
                "accept": profile_headers.get("accept", ""),
                "accept_language": profile_headers.get("accept-language", ""),
                "accept_encoding": profile_headers.get("accept-encoding", ""),
                "extra": {
                    k: v for k, v in profile_headers.items()
                    if k not in {"user-agent", "accept", "accept-language", "accept-encoding"}
                },
            },
        }
        if self._config.expose_proxy_url:
            body["proxy_url"] = self._proxy_url

        try:
            server_start = time.monotonic()
            async with self._get_session().post(
                f"{self._config.url}/token",
                json=body,
                timeout=aiohttp.ClientTimeout(total=self._config.timeout_seconds),
            ) as resp:
                server_duration = time.monotonic() - server_start
                metrics.record_auth_server_request(server_duration)
                if resp.status != 200:
                    raise RuntimeError(
                        f"token server returned {resp.status}: {await resp.text()}"
                    )
                data = await resp.json()

        except asyncio.TimeoutError as exc:
            refresh_duration = time.monotonic() - refresh_start
            await self._record_failure(target, addr)
            metrics.record_auth_token_refresh(target, addr, "failure", refresh_duration)
            logger.warning(
                "TokenManager: token server timeout for %s/%s after %.1fs",
                target, addr, self._config.timeout_seconds,
                extra={
                    "event": "token_server_timeout",
                    "target": target, "ip": addr,
                    "timeout_seconds": self._config.timeout_seconds,
                },
            )
            raise
        except Exception as exc:
            refresh_duration = time.monotonic() - refresh_start
            await self._record_failure(target, addr)
            metrics.record_auth_token_refresh(target, addr, "failure", refresh_duration)
            logger.warning(
                "TokenManager: token fetch failed for %s/%s: %s",
                target, addr, exc,
                extra={"event": "token_server_error", "target": target, "ip": addr, "error": str(exc)},
            )
            raise

        headers: dict[str, str] = data["headers"]
        expires_at = datetime.fromisoformat(data["expires_at"])
        new_cursor: dict = data.get("cursor", cursor)

        await self._write_token(target, addr, headers, expires_at, new_cursor)
        # Clear broken state on success.
        was_broken = await self._backend.counter_get(_broken_key(target, addr))
        await self._backend.counter_set(_broken_key(target, addr), 0)
        await self._backend.kv_delete(_retry_at_key(target, addr))
        if (target, addr) in self._broken_ips:
            self._broken_ips.discard((target, addr))
            broken_count = sum(1 for (t, _) in self._broken_ips if t == target)
            get_metrics().set_auth_broken_ips(target, broken_count)

        refresh_duration = time.monotonic() - refresh_start
        metrics.record_auth_token_refresh(target, addr, "success", refresh_duration)

        if was_broken and was_broken >= self._config.max_retries:
            logger.info(
                "TokenManager: %s/%s recovered from AUTH_BROKEN state",
                target, addr,
                extra={"event": "ip_recovered_auth_broken", "target": target, "ip": addr},
            )
        logger.info(
            "TokenManager: token acquired for %s/%s (expires %s, duration=%.3fs)",
            target, addr, expires_at.isoformat(), refresh_duration,
            extra={
                "event": "token_acquired",
                "target": target, "ip": addr,
                "expires_at": expires_at.isoformat(),
                "duration_seconds": refresh_duration,
            },
        )
        return headers

    # ------------------------------------------------------------------
    # Piggyback polling — wait for another replica's refresh
    # ------------------------------------------------------------------

    async def _poll_for_token(
        self,
        target: str,
        addr: str,
        stale: tuple[dict, datetime, dict] | None,
    ) -> dict[str, str]:
        """Poll Redis until a fresh token appears (up to _POLL_TIMEOUT seconds)."""
        deadline = time.monotonic() + _POLL_TIMEOUT
        refresh_threshold = self._config.refresh_threshold_seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(_POLL_INTERVAL)
            stored = await self._read_token(target, addr)
            if stored is not None:
                headers, expires_at, _ = stored
                if (expires_at - datetime.now(UTC)).total_seconds() > refresh_threshold:
                    return headers
        # Timed out waiting — use stale token if available, else error.
        if stale is not None:
            logger.warning(
                "TokenManager: piggyback timeout for %s/%s — using stale token",
                target, addr,
            )
            return stale[0]
        raise TokenPendingError(
            f"Timed out waiting for token refresh for {target!r}/{addr!r}"
        )

    # ------------------------------------------------------------------
    # Failure tracking
    # ------------------------------------------------------------------

    async def _record_failure(self, target: str, addr: str) -> None:
        """Increment broken counter and set retry_at. Quarantine if threshold reached."""
        failures = await self._backend.counter_increment(_broken_key(target, addr))
        retry_at = datetime.now(UTC) + timedelta(seconds=self._config.retry_interval_seconds)
        await self._backend.kv_set(_retry_at_key(target, addr), retry_at.isoformat())
        if failures >= self._config.max_retries:
            newly_broken = (target, addr) not in self._broken_ips
            self._broken_ips.add((target, addr))
            broken_count = sum(1 for (t, _) in self._broken_ips if t == target)
            get_metrics().set_auth_broken_ips(target, broken_count)
            if newly_broken:
                logger.error(
                    "TokenManager: %s/%s has %d consecutive failures — marked AUTH_BROKEN",
                    target, addr, failures,
                    extra={
                        "event": "ip_marked_auth_broken",
                        "target": target, "ip": addr,
                        "failure_count": failures,
                        "retry_at": retry_at.isoformat(),
                    },
                )
        else:
            logger.warning(
                "TokenManager: %s/%s failure %d/%d, retry at %s",
                target, addr, failures, self._config.max_retries, retry_at.isoformat(),
            )

    # ------------------------------------------------------------------
    # Background scheduler
    # ------------------------------------------------------------------

    async def _prewarm(self, pairs: list[tuple[str, str]]) -> None:
        """Attempt token pre-fetch for all known pairs at startup."""
        for target, addr in pairs:
            try:
                stored = await self._read_token(target, addr)
                if stored is None:
                    # No token yet — we can't pre-warm without an Identity.
                    # The first real request will trigger a fetch.
                    logger.debug(
                        "TokenManager: pre-warm skipped for %s/%s (no existing token; "
                        "will fetch on first request)",
                        target, addr,
                    )
            except Exception as exc:
                logger.warning("TokenManager: pre-warm error for %s/%s: %s", target, addr, exc)

    async def _refresh_scheduler(self) -> None:
        """Periodically refresh tokens near expiry and retry broken IPs."""
        interval = max(10.0, self._config.refresh_threshold_seconds / 2)
        while self._running:
            await asyncio.sleep(interval)
            if not self._known:
                continue
            for target, addr in list(self._known):
                try:
                    await self._maybe_refresh(target, addr)
                except Exception as exc:
                    logger.debug(
                        "TokenManager: scheduler error for %s/%s: %s", target, addr, exc
                    )

    async def _maybe_refresh(self, target: str, addr: str) -> None:
        """Proactively refresh if near expiry or retry if broken and window elapsed."""
        stored = await self._read_token(target, addr)
        now = datetime.now(UTC)
        threshold = self._config.refresh_threshold_seconds

        # Proactive refresh path.
        if stored is not None:
            _, expires_at, _ = stored
            if (expires_at - now).total_seconds() <= threshold:
                lock_id = str(_uuid_mod.uuid4())
                if await self._backend.lock_acquire(_lock_key(target, addr), lock_id, _LOCK_TTL):
                    try:
                        # We need an Identity to call the token server.  Without one
                        # (background task, no in-flight request), we skip and let the
                        # next real request trigger the refresh.
                        logger.debug(
                            "TokenManager: scheduler — token for %s/%s near expiry "
                            "(no Identity for background refresh; will refresh on next request)",
                            target, addr,
                        )
                    finally:
                        await self._backend.lock_release(_lock_key(target, addr), lock_id)
            return

        # Broken retry path.
        retry_at_raw = await self._backend.kv_get(_retry_at_key(target, addr))
        if retry_at_raw and now >= datetime.fromisoformat(retry_at_raw):
            logger.info(
                "TokenManager: auth recovery attempt for %s/%s — retry window elapsed",
                target, addr,
                extra={"event": "auth_recovery_attempt", "target": target, "ip": addr},
            )
            # Reset broken count to allow the next real request to retry.
            await self._backend.counter_set(_broken_key(target, addr), 0)
            await self._backend.kv_delete(_retry_at_key(target, addr))
            self._broken_ips.discard((target, addr))
            broken_count = sum(1 for (t, _) in self._broken_ips if t == target)
            get_metrics().set_auth_broken_ips(target, broken_count)

    # ------------------------------------------------------------------
    # Storage helpers
    # ------------------------------------------------------------------

    async def _write_token(
        self,
        target: str,
        addr: str,
        headers: dict[str, str],
        expires_at: datetime,
        cursor: dict,
    ) -> None:
        payload = json.dumps({
            "headers": headers,
            "expires_at": expires_at.isoformat(),
            "cursor": cursor,
        })
        # TTL = time until expiry + 10 minute safety margin.
        ttl = max(60, int((expires_at - datetime.now(UTC)).total_seconds()) + 600)
        await self._backend.kv_set_with_ttl(_token_key(target, addr), payload, ttl)

    async def _read_token(
        self, target: str, addr: str
    ) -> tuple[dict[str, str], datetime, dict] | None:
        raw = await self._backend.kv_get(_token_key(target, addr))
        if raw is None:
            return None
        data = json.loads(raw)
        headers: dict[str, str] = data["headers"]
        expires_at = datetime.fromisoformat(data["expires_at"])
        cursor: dict = data.get("cursor", {})
        return headers, expires_at, cursor
