"""IPPool — business logic for IP rotation.

This layer owns all decisions that involve policy:
  - Should this IP be quarantined? (failures >= threshold)
  - How long to wait before returning an IP after use? (min_request_interval)
  - When does quarantine end? (quarantine_time seconds)
  - Which IPs need to be seeded on startup?

It calls the IPPoolBackend exclusively through the primitive interface
defined in backend/base.py.  The backend never sees TargetConfig.

Relationship to other layers
----------------------------
                    ┌─────────────────┐
   TargetManager    │     IPPool      │  business logic
   (dispatch only)  │                 │  one per target
                    └────────┬────────┘
                             │ uses storage primitives
                    ┌────────▼────────┐
                    │  IPPoolBackend  │  pure data ops
                    │  (Memory/Redis) │  shared across targets
                    └─────────────────┘
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from .backend.base import IPPoolBackend
from .config import TargetConfig
from .metrics import get_metrics

logger = logging.getLogger(__name__)

_QUARANTINE_SWEEP_INTERVAL = 5.0  # seconds between quarantine expiry checks


class IPPool:
    """Manages the IP rotation policy for a single target."""

    def __init__(
        self,
        config: TargetConfig,
        backend: IPPoolBackend,
        debug: bool = False,
        sweep_interval: float = _QUARANTINE_SWEEP_INTERVAL,
    ) -> None:
        self._config = config
        self._backend = backend
        self._debug = debug
        self._sweep_interval = sweep_interval
        self._addresses = [
            f"{host}:{port}" for host, port in config.resolved_ip_list()
        ]
        self._sweep_task: asyncio.Task | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Seed the pool (if we win the init race) and start the sweep task."""
        first = await self._backend.init_target(self._config.name)
        if first:
            await self._backend.push_ips(self._config.name, self._addresses)
            if self._debug:
                logger.debug(
                    "IPPool '%s': seeded %d IPs: %s",
                    self._config.name, len(self._addresses), self._addresses,
                )
        else:
            if self._debug:
                logger.debug(
                    "IPPool '%s': skipping seed — another instance already initialised this target",
                    self._config.name,
                )

        self._running = True
        self._sweep_task = asyncio.create_task(
            self._quarantine_sweep_loop(),
            name=f"ph:pool:sweep:{self._config.name}",
        )
        if self._debug:
            logger.debug("IPPool '%s': quarantine sweep task started", self._config.name)

    async def stop(self) -> None:
        self._running = False
        if self._sweep_task:
            self._sweep_task.cancel()
            await asyncio.gather(self._sweep_task, return_exceptions=True)
        if self._debug:
            logger.debug("IPPool '%s': stopped", self._config.name)

    # ------------------------------------------------------------------
    # Public interface (used by TargetManager)
    # ------------------------------------------------------------------

    async def acquire(self, timeout: float) -> Optional[str]:
        """Return the next available IP address, or None on timeout."""
        if self._debug:
            logger.trace(  # type: ignore[attr-defined]
                "IPPool '%s': waiting for IP (timeout=%.2fs)", self._config.name, timeout
            )
        address = await self._backend.pop_ip(self._config.name, timeout)
        if self._debug:
            if address is not None:
                logger.debug("IPPool '%s': acquired %s", self._config.name, address)
            else:
                logger.debug(
                    "IPPool '%s': no IP available within %.2fs", self._config.name, timeout
                )
        return address

    async def record_success(self, address: str, elapsed: float = 0.0) -> None:
        """Reset failure state and schedule the IP's return after the cooldown.

        *elapsed* is the time already spent on the request.  The cooldown delay
        is reduced by that amount (floored at 0) so that ``min_request_interval``
        is measured from when the request was *sent*, not when it *returned*.
        """
        await self._backend.reset_failures(self._config.name, address)
        get_metrics().set_ip_failure_count(self._config.name, address, 0)
        delay = max(0.0, self._config.min_request_interval - elapsed)
        if self._debug:
            logger.debug(
                "IPPool '%s': %s — success, failures reset, returning to pool in %.2fs",
                self._config.name, address, delay,
            )
        asyncio.create_task(
            self._return_after_cooldown(address, delay),
            name=f"ph:pool:cooldown:{self._config.name}:{address}",
        )

    async def record_failure(self, address: str, elapsed: float = 0.0) -> None:
        """Increment failure count; quarantine IP if threshold is reached.

        *elapsed* is the time already spent on the request — used to adjust the
        cooldown delay so that ``min_request_interval`` is measured from request
        send time rather than response receipt time.
        """
        threshold = self._config.ip_failures_until_quarantine
        failures = await self._backend.increment_failures(self._config.name, address)
        get_metrics().set_ip_failure_count(self._config.name, address, failures)
        if self._debug:
            logger.debug(
                "IPPool '%s': %s — failure %d/%d",
                self._config.name, address, failures, threshold,
            )
        if failures >= threshold:
            release_at = time.time() + self._config.quarantine_time
            await self._backend.quarantine_add(self._config.name, address, release_at)
            get_metrics().record_quarantine_event(self._config.name, address)
            logger.warning(
                "IPPool '%s': %s quarantined for %.0fs after %d consecutive failures",
                self._config.name, address, self._config.quarantine_time, failures,
            )
        else:
            delay = max(0.0, self._config.min_request_interval - elapsed)
            asyncio.create_task(
                self._return_after_cooldown(address, delay),
                name=f"ph:pool:cooldown:{self._config.name}:{address}",
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

    async def _return_after_cooldown(self, address: str, delay: float) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        await self._backend.push_ip(self._config.name, address)
        if self._debug:
            logger.trace(  # type: ignore[attr-defined]
                "IPPool '%s': %s returned to pool (after %.2fs cooldown)",
                self._config.name, address, delay,
            )

    async def _quarantine_sweep_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._sweep_interval)
            await self._sweep_quarantine()

    async def _sweep_quarantine(self) -> None:
        """Claim and release all quarantine entries whose time has come."""
        expired = await self._backend.quarantine_pop_expired(
            self._config.name, time.time()
        )
        if self._debug:
            logger.debug(
                "IPPool '%s': quarantine sweep — %d expired entr%s",
                self._config.name, len(expired), "y" if len(expired) == 1 else "ies",
            )
        if expired:
            await self._backend.push_ips(self._config.name, expired)
            for address in expired:
                logger.info(
                    "IPPool '%s': %s released from quarantine and returned to pool",
                    self._config.name, address,
                )
