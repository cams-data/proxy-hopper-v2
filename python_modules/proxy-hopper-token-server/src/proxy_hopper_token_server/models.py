"""Data models for the Proxy Hopper token server protocol.

TokenRequest and TokenResponse define the JSON wire format for the
POST /token endpoint. These are the only shared contract between Proxy
Hopper core and user-provided token server implementations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Profile:
    """Identity fingerprint for the upstream proxy IP, as tracked by Proxy Hopper."""

    user_agent: str
    accept: str
    accept_language: str
    accept_encoding: str
    # Additional fingerprint headers (e.g. sec-ch-ua, sec-ch-ua-platform).
    extra: dict[str, str] = field(default_factory=dict)


@dataclass
class TokenRequest:
    """Payload sent by Proxy Hopper to POST /token."""

    target: str          # target name as configured in Proxy Hopper
    ip: str              # upstream proxy host
    port: int            # upstream proxy port
    cursor: dict         # opaque state blob; {} on first call for this (target, ip)
    profile: Profile     # full identity fingerprint for this IP
    # Proxy Hopper's own proxy listener URL, for IP-pinned token fetching via
    # ProxyHopperClient. None when exposeProxyUrl is false in server config.
    proxy_url: str | None = None


@dataclass
class TokenResponse:
    """Response returned by a TokenProvider to Proxy Hopper."""

    # Headers Proxy Hopper injects into every request through this (target, ip).
    # e.g. {"Authorization": "Bearer abc123"}
    headers: dict[str, str]
    # UTC datetime after which the token must be refreshed.
    expires_at: datetime
    # Updated opaque state stored and forwarded on the next call.
    # Return the incoming cursor unchanged if no state update is needed.
    cursor: dict
