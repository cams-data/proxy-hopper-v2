"""Browser fingerprint profiles — header bundles for the identity system.

Each call to ``get_profile()`` returns a ``FingerprintProfile`` with a
randomly selected browser/OS combination and a version number sampled from
a realistic rolling window.  Version numbers advance roughly in line with
the actual browser release cadence, reducing the risk of identities being
flagged for using an obviously stale UA string.

Profile headers are embedded into the ``Identity`` object at creation time
so they remain consistent for the lifetime of that identity regardless of
which instance creates or loads it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class FingerprintProfile:
    """An immutable, internally-consistent set of browser identity headers.

    These are the HTTP-layer signals that fingerprinting systems inspect.
    TLS-layer signals (JA3, AKAMAI) require a custom TLS stack and are
    outside the scope of this module.
    """

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
# Version pools — update periodically as browser releases advance
# ---------------------------------------------------------------------------

_CHROME_MAJOR_VERSIONS: list[int] = list(range(120, 128))   # Chrome 120–127
_FIREFOX_MAJOR_VERSIONS: list[int] = list(range(120, 129))  # Firefox 120–128

# (macOS version string, Safari version, WebKit build)
_SAFARI_VARIANTS: list[tuple[str, str, str]] = [
    ("14_4_1", "17.4.1", "605.1.15"),
    ("14_5",   "17.5",   "605.1.15"),
    ("15_0",   "18.0",   "618.2.12"),
    ("15_1",   "18.1",   "618.2.12"),
]


# ---------------------------------------------------------------------------
# Per-platform profile generators
# ---------------------------------------------------------------------------

def _chrome_windows() -> FingerprintProfile:
    v = random.choice(_CHROME_MAJOR_VERSIONS)
    return FingerprintProfile(
        user_agent=(
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{v}.0.0.0 Safari/537.36"
        ),
        accept=(
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        accept_language="en-US,en;q=0.9",
        accept_encoding="gzip, deflate, br, zstd",
    )


def _chrome_macos() -> FingerprintProfile:
    v = random.choice(_CHROME_MAJOR_VERSIONS)
    return FingerprintProfile(
        user_agent=(
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{v}.0.0.0 Safari/537.36"
        ),
        accept=(
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        accept_language="en-US,en;q=0.9",
        accept_encoding="gzip, deflate, br, zstd",
    )


def _safari_macos() -> FingerprintProfile:
    os_ver, safari_ver, webkit_ver = random.choice(_SAFARI_VARIANTS)
    return FingerprintProfile(
        user_agent=(
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X {os_ver}) "
            f"AppleWebKit/{webkit_ver} (KHTML, like Gecko) "
            f"Version/{safari_ver} Safari/{webkit_ver}"
        ),
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        accept_language="en-US,en;q=0.9",
        accept_encoding="gzip, deflate, br",
    )


def _firefox_windows() -> FingerprintProfile:
    v = random.choice(_FIREFOX_MAJOR_VERSIONS)
    return FingerprintProfile(
        user_agent=(
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{v}.0) "
            f"Gecko/20100101 Firefox/{v}.0"
        ),
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        accept_language="en-US,en;q=0.5",
        accept_encoding="gzip, deflate, br, zstd",
    )


def _firefox_linux() -> FingerprintProfile:
    v = random.choice(_FIREFOX_MAJOR_VERSIONS)
    return FingerprintProfile(
        user_agent=(
            f"Mozilla/5.0 (X11; Linux x86_64; rv:{v}.0) "
            f"Gecko/20100101 Firefox/{v}.0"
        ),
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        accept_language="en-US,en;q=0.5",
        accept_encoding="gzip, deflate, br, zstd",
    )


_GENERATORS = [
    _chrome_windows,
    _chrome_macos,
    _safari_macos,
    _firefox_windows,
    _firefox_linux,
]


def get_profile() -> FingerprintProfile:
    """Return a randomly generated fingerprint profile.

    Each call picks a random browser/OS platform and samples a version
    number from a realistic rolling window so identities never all present
    the same stale UA string.
    """
    return random.choice(_GENERATORS)()
