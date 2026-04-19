"""IdentityQueue — business logic for IP rotation via backend-stored identities.

This layer owns all decisions that involve policy:
  - Should this IP be quarantined? (failures >= threshold)
  - How long to wait before returning an identity after use? (min_request_interval)
  - When does quarantine end? (quarantine_time seconds)
  - Which addresses need identities seeded on startup?

The pool queue stores UUID strings rather than raw IP addresses.  Each UUID
maps to a full ``Identity`` object in the Backend KV, allowing all HA instances
to share consistent identity state (fingerprint headers, cookies, request count).

Key schema (owned by IPPoolStore)
----------------------------------
ph:{target}:pool              LIST    — UUID strings, BLPOP to acquire
ph:{target}:identity:{uuid}   KV      — full identity JSON
ph:{target}:ip:{address}      KV      — active UUID for this address
ph:{target}:retired:{address} KV      — "1" if address is retired
ph:{target}:init              KV      — SETNX startup race guard
ph:{target}:failures:{addr}   KV      — consecutive failure counter
ph:{target}:quarantine        ZSET    — address → release timestamp

Retirement
----------
When an IP is removed via the admin API, ``retire_address`` adds it to the
retired KV.  The next ``acquire`` call that pops that address's UUID silently
discards the identity and retires the marker.  Addresses already in quarantine
at retirement time are dropped during the sweep when they would normally return.

Null identities
---------------
When ``identity.enabled`` is False on the target, identities are still created
per IP but with empty ``headers`` and ``cookies_enabled=False``.  This keeps
the queue structure uniform — all targets use UUIDs, all state lives in the
backend — regardless of whether the identity feature is active.
"""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

from .config import TargetConfig
from .identity.fingerprint import get_profile
from .identity.identity import Identity
from .logging_config import get_logger
from .metrics import get_metrics
from .pool_store import IPPoolStore

logger = get_logger(__name__)

_QUARANTINE_SWEEP_INTERVAL = 5.0  # seconds between quarantine expiry checks


class IdentityQueue:
    """Manages the IP rotation policy for a single target via backend-stored identities."""

    def __init__(
        self,
        config: TargetConfig,
        backend: IPPoolStore,
        debug: bool = False,
        sweep_interval: float = _QUARANTINE_SWEEP_INTERVAL,
    ) -> None:
        self._config = config
        self._backend = backend
        self._debug = debug
        self._sweep_interval = sweep_interval
        # Map address → (provider, region_tag) for enriched metric labels
        self._ip_meta: dict[str, tuple[str, str]] = {
            ip.address: (ip.provider, ip.region_tag)
            for ip in config.resolved_ips
        }
        self._sweep_task: asyncio.Task | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Seed identities (if we win the init race) and start the sweep task."""
        first = await self._backend.claim_init(self._config.name)
        if first:
            for ip in self._config.resolved_ips:
                await self._create_identity(ip.address)
            if self._debug:
                logger.debug(
                    "IdentityQueue '%s': seeded %d identit%s",
                    self._config.name,
                    len(self._config.resolved_ips),
                    "y" if len(self._config.resolved_ips) == 1 else "ies",
                )
        else:
            if self._debug:
                logger.debug(
                    "IdentityQueue '%s': skipping seed — another instance already initialised",
                    self._config.name,
                )

        self._running = True
        self._sweep_task = asyncio.create_task(
            self._quarantine_sweep_loop(),
            name=f"ph:pool:sweep:{self._config.name}",
        )

    async def stop(self) -> None:
        self._running = False
        if self._sweep_task:
            self._sweep_task.cancel()
            await asyncio.gather(self._sweep_task, return_exceptions=True)
        if self._debug:
            logger.debug("IdentityQueue '%s': stopped", self._config.name)

    # ------------------------------------------------------------------
    # Public interface (used by TargetManager)
    # ------------------------------------------------------------------

    async def acquire(self, timeout: float) -> tuple[str, Identity] | None:
        """Return the next available (uuid, identity), or None on timeout.

        Silently discards retired addresses: if the popped UUID belongs to a
        retired address, the identity is deleted from KV and the loop retries
        until a live identity is found or the timeout is reached.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if self._debug:
                    logger.debug(
                        "IdentityQueue '%s': no identity available within timeout",
                        self._config.name,
                    )
                return None

            uuid = await self._backend.pop_identity_uuid(self._config.name, remaining)
            if uuid is None:
                return None  # timed out

            data = await self._backend.identity_read(self._config.name, uuid)
            if data is None:
                # Orphaned UUID (identity deleted externally) — skip and retry.
                logger.warning(
                    "IdentityQueue '%s': UUID %s has no identity data — discarding",
                    self._config.name, uuid,
                )
                continue

            address = data["address"]

            if await self._backend.retire_check(self._config.name, address):
                # Address was retired — discard identity and clean up.
                await self._backend.retire_remove(self._config.name, address)
                await self._backend.identity_delete(self._config.name, uuid)
                await self._backend.ip_delete(self._config.name, address)
                logger.info(
                    "IdentityQueue '%s': discarded retired identity for %s",
                    self._config.name, address,
                )
                continue

            if self._debug:
                logger.debug(
                    "IdentityQueue '%s': acquired identity %s for %s",
                    self._config.name, uuid, address,
                )
            return uuid, Identity.from_dict(data)

    async def record_success(self, uuid: str, identity: Identity, elapsed: float) -> None:
        """Persist updated identity state, reset failures, schedule return after cooldown."""
        await self._backend.reset_failures(self._config.name, identity.address)
        provider, region = self._ip_meta.get(identity.address, ("", ""))
        get_metrics().set_ip_failure_count(
            self._config.name, identity.address, 0,
            provider=provider, region=region,
        )
        # Write updated identity (cookies may have changed) before returning UUID.
        await self._backend.identity_write(self._config.name, uuid, identity.to_dict())
        delay = max(0.0, self._config.min_request_interval - elapsed)
        if self._debug:
            logger.debug(
                "IdentityQueue '%s': %s — success, returning in %.2fs",
                self._config.name, identity.address, delay,
            )
        asyncio.create_task(
            self._return_after_cooldown(uuid, delay),
            name=f"ph:pool:cooldown:{self._config.name}:{identity.address}",
        )

    async def record_failure(
        self, uuid: str, identity: Identity, elapsed: float, *, return_uuid: bool = True
    ) -> bool:
        """Increment failure count; quarantine IP if threshold is reached.

        Returns True if the IP was quarantined (UUID is NOT returned to the queue).
        Returns False if not yet at threshold.

        When *return_uuid* is False the UUID is not scheduled for return even
        if below the quarantine threshold — the caller is responsible for
        disposing of the UUID (e.g. via ``rotate``).  Pass False when you plan
        to call ``rotate`` immediately after to avoid pushing a zombie UUID.
        """
        threshold = self._config.ip_failures_until_quarantine
        failures = await self._backend.increment_failures(self._config.name, identity.address)
        provider, region = self._ip_meta.get(identity.address, ("", ""))
        get_metrics().set_ip_failure_count(
            self._config.name, identity.address, failures,
            provider=provider, region=region,
        )
        if self._debug:
            logger.debug(
                "IdentityQueue '%s': %s — failure %d/%d",
                self._config.name, identity.address, failures, threshold,
            )
        if failures >= threshold:
            release_at = time.time() + self._config.quarantine_time
            await self._backend.quarantine_add(self._config.name, identity.address, release_at)
            get_metrics().record_quarantine_event(
                self._config.name, identity.address,
                provider=provider, region=region,
            )
            logger.warning(
                "IdentityQueue '%s': %s quarantined for %.0fs after %d consecutive failures",
                self._config.name, identity.address, self._config.quarantine_time, failures,
            )
            # Delete stale identity — sweep will create a fresh one on release.
            await self._backend.identity_delete(self._config.name, uuid)
            # UUID is NOT returned to the queue.
            return True
        else:
            if return_uuid:
                delay = max(0.0, self._config.min_request_interval - elapsed)
                asyncio.create_task(
                    self._return_after_cooldown(uuid, delay),
                    name=f"ph:pool:cooldown:{self._config.name}:{identity.address}",
                )
            return False

    async def rotate(
        self, uuid: str, identity: Identity, elapsed: float, *, reset_failures: bool = False
    ) -> None:
        """Replace the identity for *address* — called on 429 or request-count limit.

        Deletes the current identity, creates a fresh one, and schedules the
        new UUID for return to the queue after the cooldown.  The current
        request continues with the old identity until it completes.

        *reset_failures* should be True only for voluntary rotation triggered by
        ``rotate_after_requests`` (the IP was healthy).  For 429-triggered rotation
        pass False (the default) so failure counts continue to accumulate toward
        the quarantine threshold.
        """
        if reset_failures:
            await self._backend.reset_failures(self._config.name, identity.address)
            provider, region = self._ip_meta.get(identity.address, ("", ""))
            get_metrics().set_ip_failure_count(
                self._config.name, identity.address, 0,
                provider=provider, region=region,
            )
        await self._backend.identity_delete(self._config.name, uuid)
        new_uuid, _ = await self._create_identity(identity.address, push=False)
        delay = max(0.0, self._config.min_request_interval - elapsed)
        logger.info(
            "IdentityQueue '%s': rotated identity for %s (new uuid=%s, cooldown=%.2fs)",
            self._config.name, identity.address, new_uuid, delay,
        )
        asyncio.create_task(
            self._return_after_cooldown(new_uuid, delay),
            name=f"ph:pool:cooldown:{self._config.name}:{identity.address}",
        )

    async def add_address(
        self, address: str, provider: str = "", region_tag: str = ""
    ) -> None:
        """Add a new IP address — create an identity and push its UUID to the queue."""
        self._ip_meta[address] = (provider, region_tag)
        await self._create_identity(address)
        logger.info(
            "IdentityQueue '%s': added new address %s", self._config.name, address
        )

    async def retire_address(self, address: str) -> None:
        """Mark *address* as retired.

        Its UUID will be discarded the next time it is popped from the queue
        (or when its quarantine expires, whichever comes first).
        """
        await self._backend.retire_add(self._config.name, address)
        logger.info(
            "IdentityQueue '%s': address %s marked for retirement",
            self._config.name, address,
        )

    async def get_status(self) -> dict:
        size, quarantined = await self._backend.pool_size_and_quarantine(self._config.name)
        return {
            "name": self._config.name,
            "available_ips": size,
            "quarantined_ips": quarantined,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _create_identity(
        self, address: str, *, push: bool = True
    ) -> tuple[str, Identity]:
        """Create a new identity for *address*, write to KV, optionally push UUID.

        Returns ``(uuid, identity)``.  When *push* is False the UUID is written
        to KV but not enqueued — the caller is responsible for scheduling its
        return (used by ``rotate``).
        """
        new_uuid = str(uuid4())
        cookies_enabled = self._config.identity.enabled and self._config.identity.cookies
        headers = get_profile().as_headers() if self._config.identity.enabled else {}
        identity = Identity(
            address=address,
            headers=headers,
            cookies_enabled=cookies_enabled,
        )
        await self._backend.identity_write(self._config.name, new_uuid, identity.to_dict())
        await self._backend.ip_set(self._config.name, address, new_uuid)
        if push:
            await self._backend.push_identity_uuid(self._config.name, new_uuid)
        if self._debug:
            logger.debug(
                "IdentityQueue '%s': created identity %s for %s (cookies=%s, fingerprint=%s)",
                self._config.name, new_uuid, address,
                cookies_enabled, bool(headers),
            )
        return new_uuid, identity

    async def _return_after_cooldown(self, uuid: str, delay: float) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        await self._backend.push_identity_uuid(self._config.name, uuid)
        if self._debug:
            logger.trace(
                "IdentityQueue '%s': UUID %s returned to pool (after %.2fs cooldown)",
                self._config.name, uuid, delay,
            )

    async def _quarantine_sweep_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._sweep_interval)
            await self._sweep_quarantine()

    async def _sweep_quarantine(self) -> None:
        """Release expired quarantine entries — retire or create fresh identities."""
        expired = await self._backend.quarantine_pop_expired(
            self._config.name, time.time()
        )
        if self._debug and expired:
            logger.debug(
                "IdentityQueue '%s': quarantine sweep — %d expired entr%s",
                self._config.name, len(expired), "y" if len(expired) == 1 else "ies",
            )
        for address in expired:
            if await self._backend.retire_check(self._config.name, address):
                # Address was retired while in quarantine — clean up and skip.
                await self._backend.retire_remove(self._config.name, address)
                await self._backend.ip_delete(self._config.name, address)
                logger.info(
                    "IdentityQueue '%s': quarantine expired for retired address %s — discarding",
                    self._config.name, address,
                )
                continue
            # Create a fresh identity and push it to the queue.
            await self._create_identity(address)
            logger.info(
                "IdentityQueue '%s': %s released from quarantine with fresh identity",
                self._config.name, address,
            )
