"""IdentityStore — manages Identity lifecycle for a single target.

One ``IdentityStore`` is created per ``TargetManager`` (i.e. per target).
It maps each IP address to the ``Identity`` currently assigned to it.

Responsibilities
----------------
- Create a fresh ``Identity`` on first use of an address.
- Rotate (discard + replace) an identity on demand:
    - When an IP is quarantined (called by TargetManager after pool signals it)
    - When a 429 is received and ``rotate_on_429`` is True
    - When ``request_count`` reaches ``rotate_after_requests``
- Provide ``needs_rotation`` so callers can check the count threshold without
  reaching inside the Identity object.

What IdentityStore does NOT do
-------------------------------
- It does not know about the pool, quarantine, or HTTP.  Those concerns stay
  in TargetManager / IPPool.
- It does not perform warmup.  Warmup is async and requires an HTTP client;
  TargetManager orchestrates it after calling ``rotate``.
- It is not thread-safe across event loops.  It is only ever accessed from
  one TargetManager's asyncio coroutines.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .fingerprint import get_profile
from .identity import Identity

if TYPE_CHECKING:
    from .config import IdentityConfig

logger = logging.getLogger(__name__)


class IdentityStore:
    """Per-target registry of (address → Identity) with rotation support.

    Parameters
    ----------
    target_name
        Used only for log messages.
    config
        The ``IdentityConfig`` from the target's YAML definition.
    """

    def __init__(self, target_name: str, config: "IdentityConfig") -> None:
        self._target = target_name
        self._config = config
        self._store: dict[str, Identity] = {}

    # ------------------------------------------------------------------
    # Primary interface (called from TargetManager)
    # ------------------------------------------------------------------

    def get_or_create(self, address: str) -> Identity:
        """Return the existing identity for *address*, creating one if absent."""
        if address not in self._store:
            self._store[address] = self._create(address, reason="new")
        return self._store[address]

    def rotate(self, address: str, reason: str = "manual") -> Identity:
        """Discard the current identity for *address* and create a fresh one.

        *reason* is a short string for log messages (e.g. ``"quarantine"``,
        ``"429"``, ``"request_limit"``).

        Returns the new ``Identity`` so callers can use it immediately.
        """
        old = self._store.pop(address, None)
        new = self._create(address, reason=reason)
        if old is not None:
            logger.info(
                "IdentityStore '%s': rotated identity for %s "
                "(reason=%s, previous requests=%d, cookies=%d)",
                self._target, address, reason,
                old.request_count, len(old.cookies),
            )
        return new

    def needs_rotation(self, address: str) -> bool:
        """Return True if the identity has hit the ``rotate_after_requests`` limit.

        Returns False when the limit is not configured or the address has no
        identity yet.
        """
        limit = self._config.rotate_after_requests
        if limit is None:
            return False
        identity = self._store.get(address)
        if identity is None:
            return False
        return identity.request_count >= limit

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _create(self, address: str, reason: str) -> Identity:
        profile = get_profile(self._config.profile)
        identity = Identity(
            profile=profile,
            cookies_enabled=self._config.cookies,
        )
        logger.debug(
            "IdentityStore '%s': created identity for %s "
            "(reason=%s, profile=%s, cookies=%s)",
            self._target, address, reason, profile.name, self._config.cookies,
        )
        self._store[address] = identity
        return identity
