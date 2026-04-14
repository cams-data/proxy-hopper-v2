"""Browser fingerprint profiles for the identity system.

Each ``FingerprintProfile`` is an internally-consistent bundle of HTTP
headers that a particular browser/OS combination would send.  Consistency
matters more than realism: a single identity must never mix headers from
different profiles (e.g. a Chrome User-Agent with a Firefox Accept header).

All profiles are registered in ``PROFILES``.  ``get_profile`` resolves a
name to a profile, or picks a random one when no name is given.

Adding a profile
----------------
Add a new ``FingerprintProfile`` entry to ``PROFILES`` with a descriptive
key.  No other changes are needed — ``get_profile(None)`` will include it
in the random selection pool automatically.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class FingerprintProfile:
    """An immutable, internally-consistent set of browser identity headers.

    These are the HTTP-layer signals that fingerprinting systems inspect.
    TLS-layer signals (JA3, AKAMAI) require a custom TLS stack and are
    outside the scope of this module.
    """

    name: str
    user_agent: str
    accept: str
    accept_language: str
    accept_encoding: str

    def as_headers(self) -> dict[str, str]:
        """Return a dict of headers to merge into an outgoing request."""
        return {
            "user-agent": self.user_agent,
            "accept": self.accept,
            "accept-language": self.accept_language,
            "accept-encoding": self.accept_encoding,
        }


# ---------------------------------------------------------------------------
# Built-in profiles
# ---------------------------------------------------------------------------

PROFILES: dict[str, FingerprintProfile] = {
    "chrome-windows": FingerprintProfile(
        name="chrome-windows",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        accept=(
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        accept_language="en-US,en;q=0.9",
        accept_encoding="gzip, deflate, br, zstd",
    ),
    "chrome-macos": FingerprintProfile(
        name="chrome-macos",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        accept=(
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        accept_language="en-US,en;q=0.9",
        accept_encoding="gzip, deflate, br, zstd",
    ),
    "safari-macos": FingerprintProfile(
        name="safari-macos",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.4.1 Safari/605.1.15"
        ),
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        accept_language="en-US,en;q=0.9",
        accept_encoding="gzip, deflate, br",
    ),
    "firefox-windows": FingerprintProfile(
        name="firefox-windows",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
            "Gecko/20100101 Firefox/125.0"
        ),
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        accept_language="en-US,en;q=0.5",
        accept_encoding="gzip, deflate, br, zstd",
    ),
    "firefox-linux": FingerprintProfile(
        name="firefox-linux",
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) "
            "Gecko/20100101 Firefox/125.0"
        ),
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        accept_language="en-US,en;q=0.5",
        accept_encoding="gzip, deflate, br, zstd",
    ),
}

VALID_PROFILE_NAMES: frozenset[str] = frozenset(PROFILES)

_PROFILE_LIST: list[FingerprintProfile] = list(PROFILES.values())


def get_profile(name: Optional[str]) -> FingerprintProfile:
    """Return the named profile, or a random one if *name* is None.

    Raises ``KeyError`` for an unrecognised name so config validation catches
    it early rather than silently falling back to random.
    """
    if name is None:
        return random.choice(_PROFILE_LIST)
    try:
        return PROFILES[name]
    except KeyError:
        valid = ", ".join(sorted(PROFILES))
        raise KeyError(
            f"Unknown fingerprint profile {name!r}. Valid profiles: {valid}"
        ) from None
