"""Strawberry input types and input → domain-object converters."""

from __future__ import annotations

from typing import Optional

import strawberry


# ---------------------------------------------------------------------------
# Input types
# ---------------------------------------------------------------------------

@strawberry.input
class BasicAuthInput:
    username: str
    password: str


@strawberry.input
class TargetInput:
    name: str
    regex: str
    ip_list: list[str]
    min_request_interval: float = 1.0
    max_queue_wait: float = 30.0
    num_retries: int = 3
    ip_failures_until_quarantine: int = 5
    quarantine_time: float = 120.0
    default_proxy_port: int = 8080
    mutable: bool = True


@strawberry.input
class ProviderInput:
    name: str
    ip_list: list[str]
    region_tag: Optional[str] = None
    mutable: bool = True
    auth: Optional[BasicAuthInput] = None


# ---------------------------------------------------------------------------
# Converters
# ---------------------------------------------------------------------------

def target_input_to_config(inp: TargetInput):
    """Convert a TargetInput to a TargetConfig."""
    from ..repository import _build_target
    return _build_target(
        name=inp.name,
        regex=inp.regex,
        ip_list=inp.ip_list,
        default_proxy_port=inp.default_proxy_port,
        min_request_interval=inp.min_request_interval,
        max_queue_wait=inp.max_queue_wait,
        num_retries=inp.num_retries,
        ip_failures_until_quarantine=inp.ip_failures_until_quarantine,
        quarantine_time=inp.quarantine_time,
        mutable=inp.mutable,
    )


def provider_input_to_model(inp: ProviderInput):
    """Convert a ProviderInput to a ProxyProvider."""
    from ..config import BasicAuth, ProxyProvider
    auth = None
    if inp.auth is not None:
        auth = BasicAuth(username=inp.auth.username, password=inp.auth.password)
    return ProxyProvider(
        name=inp.name,
        ip_list=inp.ip_list,
        region_tag=inp.region_tag,
        mutable=inp.mutable,
        auth=auth,
    )
