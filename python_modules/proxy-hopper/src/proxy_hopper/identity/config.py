"""Configuration models for the identity system.

An ``IdentityConfig`` is embedded in each ``TargetConfig`` under the
``identity:`` YAML key.  All fields default to off so the feature is
completely inert unless explicitly enabled.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class WarmupConfig(BaseModel):
    """Optional HTTP request sent through a fresh identity before it enters service.

    The warmup fires when a new identity is created for an IP that is returning
    from quarantine (or on first use).  Its purpose is to collect any session
    cookies the target issues on first contact, reducing the chance that the
    first real request is treated as a cold/unknown client.

    ``path`` is a relative URL path sent as a GET to the target host.  The
    target host is inferred from the request URL at warmup time — Proxy Hopper
    does not need explicit configuration of the upstream hostname here.
    """

    enabled: bool = True
    path: str = "/"


class IdentityConfig(BaseModel):
    """Per-target identity persistence and rotation settings.

    When ``enabled`` is False (the default) the identity system is completely
    bypassed for that target — no headers are injected, no cookies are stored,
    and no rotation logic runs.

    Fields
    ------
    enabled
        Master switch.  Default: False.
    cookies
        Persist and replay session cookies per (IP, target) pair.  Cookies
        received from the upstream are stored and sent on subsequent requests
        through the same IP.  Default: True (when identity is enabled).
    profile
        Name of a built-in fingerprint profile to use for all identities on
        this target.  When omitted, each new identity picks a random profile
        from the built-in set so different IPs present as different clients.
        Valid values: ``chrome-windows``, ``chrome-macos``, ``safari-macos``,
        ``firefox-linux``, ``firefox-windows``.
    rotate_after_requests
        Voluntarily rotate the identity after this many successful requests,
        independently of failures or quarantine.  Useful for shedding sessions
        before an API's per-session request quota is hit.  Omit to disable.
    rotate_on_429
        When True, receiving a 429 response causes the identity to rotate
        immediately, even if the IP is not yet quarantined.  The assumption is
        that a 429 means the session is rate-limited at the identity level, not
        just the IP level.  Default: True.
    warmup
        Configuration for the optional warmup request.  Omit to disable
        warmup entirely.
    """

    enabled: bool = False
    cookies: bool = True
    profile: Optional[str] = None
    rotate_after_requests: Optional[int] = Field(default=None, ge=1)
    rotate_on_429: bool = True
    warmup: Optional[WarmupConfig] = None
