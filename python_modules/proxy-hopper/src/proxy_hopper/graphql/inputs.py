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
class IpRequestInput:
    provider: str
    count: int


@strawberry.input
class IpPoolInput:
    name: str
    ip_requests: list[IpRequestInput]
    mutable: bool = True


@strawberry.input
class TargetInput:
    name: str
    regex: str
    pool_name: str
    min_request_interval: float = 1.0
    max_queue_wait: float = 30.0
    num_retries: int = 3
    ip_failures_until_quarantine: int = 5
    quarantine_time: float = 120.0
    default_proxy_port: int = 8080
    spoof_user_agent: bool = True
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

def pool_input_to_model(inp: IpPoolInput):
    """Convert an IpPoolInput to an IpPool."""
    from ..config import IpPool, IpRequest
    return IpPool(
        name=inp.name,
        ip_requests=[IpRequest(provider=r.provider, count=r.count) for r in inp.ip_requests],
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
