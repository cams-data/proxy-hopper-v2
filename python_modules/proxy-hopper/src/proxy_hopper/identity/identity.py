"""The Identity class — per-(IP, target) client persona.

An ``Identity`` bundles the proxy address it is bound to, a set of browser
fingerprint headers (User-Agent, Accept-*), and an optional cookie jar.  It
is the unit that is created at startup (and on quarantine release) and
rotated (discarded + replaced) when a 429 fires or a request-count limit
is hit.

Serialisation
-------------
``Identity`` can be round-tripped through a plain JSON-serialisable dict via
``to_dict()`` / ``from_dict()``.  This is the form stored in the Backend KV
so that all running instances share the same state.

Cookie handling
---------------
Cookies are stored as a plain ``dict[str, str]`` (name → value).  This
deliberately ignores domain, path, and expiry metadata — Proxy Hopper is
forwarding to a known upstream, so domain scoping adds no value.  Deleted
cookies (``Max-Age: 0`` or ``Expires`` in the past) are removed from the
store on the next ``update_from_response`` call.

Thread safety
-------------
``Identity`` is not thread-safe.  It is only ever accessed from within a
single asyncio event-loop context, so no locking is required.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie

logger = logging.getLogger(__name__)

# Sentinel date string used by servers to signal cookie deletion.
_EPOCH_DATES = frozenset({
    "Thu, 01 Jan 1970 00:00:00 GMT",
    "Thu, 01-Jan-1970 00:00:00 GMT",
})

# Safety limits — prevent unbounded memory growth from a misbehaving upstream.
_MAX_COOKIES = 50             # max number of cookies stored per identity
_MAX_COOKIE_VALUE_LEN = 4096  # max byte length of a single cookie value


@dataclass
class Identity:
    """A client persona bound to a single (IP address, target) pair.

    Attributes
    ----------
    address
        The proxy address (``host:port``) this identity is bound to.
    headers
        Fingerprint headers (User-Agent, Accept, Accept-Language,
        Accept-Encoding) pre-computed at creation time.  Empty dict when the
        identity system is disabled for the target.
    cookies_enabled
        Whether this identity should persist and replay cookies.  Controlled
        by ``IdentityConfig.cookies``; stored here so ``Identity`` is
        self-contained.
    request_count
        Number of requests dispatched through this identity.  Incremented by
        ``record_request``; compared against ``rotate_after_requests`` by the
        caller.
    cookies
        Active cookie store.  Only populated when ``cookies_enabled`` is True.
    created_at
        Monotonic timestamp of creation — used for logging.  Not serialised;
        reset to the current time on ``from_dict``.
    """

    address: str
    headers: dict[str, str]
    cookies_enabled: bool
    request_count: int = 0
    cookies: dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.monotonic)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dict suitable for Backend KV storage."""
        return {
            "address": self.address,
            "headers": self.headers,
            "cookies_enabled": self.cookies_enabled,
            "request_count": self.request_count,
            "cookies": self.cookies,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Identity":
        """Reconstruct an ``Identity`` from a dict returned by ``to_dict``."""
        return cls(
            address=data["address"],
            headers=data.get("headers", {}),
            cookies_enabled=data["cookies_enabled"],
            request_count=data.get("request_count", 0),
            cookies=dict(data.get("cookies", {})),
        )

    # ------------------------------------------------------------------
    # Outgoing request helpers
    # ------------------------------------------------------------------

    def apply_to_headers(self, headers: dict[str, str]) -> dict[str, str]:
        """Return a copy of *headers* with fingerprint and cookie headers merged in.

        Fingerprint headers (User-Agent, Accept-*) are set unconditionally —
        they override whatever the upstream client sent so the identity is
        consistent regardless of the caller's own headers.

        Cookie header is appended only when ``cookies_enabled`` is True and
        the store is non-empty.  If the caller already sent a ``cookie``
        header it is replaced (the identity's cookies are authoritative).
        """
        merged = {**headers, **self.headers}
        if self.cookies_enabled and self.cookies:
            merged["cookie"] = "; ".join(
                f"{k}={v}" for k, v in self.cookies.items()
            )
        elif "cookie" in merged and self.cookies_enabled:
            # No stored cookies yet — drop any client-supplied cookie so we
            # don't leak the caller's session into the identity's jar.
            del merged["cookie"]
        return merged

    # ------------------------------------------------------------------
    # Incoming response helpers
    # ------------------------------------------------------------------

    def update_from_response(self, response_headers: dict[str, str] | list[tuple[str, str]]) -> None:
        """Parse ``Set-Cookie`` headers from *response_headers* and update the store.

        Accepts either a plain dict (single ``Set-Cookie`` value) or a list of
        ``(name, value)`` pairs, which is the form aiohttp exposes for
        multi-value headers via ``resp.raw_headers``.

        Deleted cookies (``Max-Age: 0`` or epoch ``Expires``) are removed.
        This is a no-op when ``cookies_enabled`` is False.
        """
        if not self.cookies_enabled:
            return

        if isinstance(response_headers, dict):
            set_cookie_values = (
                [response_headers["set-cookie"]]
                if "set-cookie" in response_headers
                else []
            )
        else:
            # list of (header_name_bytes_or_str, value) tuples from aiohttp
            set_cookie_values = [
                v if isinstance(v, str) else v.decode("latin-1")
                for k, v in response_headers
                if (k if isinstance(k, str) else k.decode("latin-1")).lower() == "set-cookie"
            ]

        for raw in set_cookie_values:
            c: SimpleCookie = SimpleCookie()
            c.load(raw)
            for name, morsel in c.items():
                if morsel["max-age"] == "0" or morsel["expires"] in _EPOCH_DATES:
                    self.cookies.pop(name, None)
                    logger.debug("Identity: removed cookie %r (deleted by server)", name)
                else:
                    if len(morsel.value) > _MAX_COOKIE_VALUE_LEN:
                        logger.warning(
                            "Identity: discarding cookie %r — value exceeds %d bytes (%d bytes)",
                            name, _MAX_COOKIE_VALUE_LEN, len(morsel.value),
                        )
                        continue
                    if name not in self.cookies and len(self.cookies) >= _MAX_COOKIES:
                        logger.warning(
                            "Identity: cookie jar full (%d entries) — discarding new cookie %r",
                            _MAX_COOKIES, name,
                        )
                        continue
                    self.cookies[name] = morsel.value
                    logger.debug("Identity: stored cookie %r", name)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def record_request(self) -> None:
        """Increment the request counter.  Called after every dispatch."""
        self.request_count += 1
