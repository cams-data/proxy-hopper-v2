"""Strawberry output types and domain-object → GraphQL type converters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import strawberry

if TYPE_CHECKING:
    from ..config import IpPool, ProxyProvider, TargetConfig


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------

@strawberry.type
class ResolvedIPType:
    host: str
    port: int
    provider: Optional[str] = None


@strawberry.type
class TargetType:
    name: str
    regex: str
    pool_name: str
    resolved_ips: list[ResolvedIPType]
    min_request_interval: float
    max_queue_wait: float
    num_retries: int
    ip_failures_until_quarantine: int
    quarantine_time: float
    default_proxy_port: int
    spoof_user_agent: bool
    mutable: bool


@strawberry.type
class IpRequestType:
    provider: str
    count: int


@strawberry.type
class IpPoolType:
    name: str
    ip_requests: list[IpRequestType]
    mutable: bool


@strawberry.type
class ProviderType:
    name: str
    ip_list: list[str]
    region_tag: Optional[str]
    mutable: bool
    #: True when Basic Auth credentials are stored — credentials are never returned.
    has_auth: bool


@strawberry.type
class StatusType:
    auth_enabled: bool
    user_sub: str
    user_role: str


# ---------------------------------------------------------------------------
# Converters
# ---------------------------------------------------------------------------

def target_to_gql(config: "TargetConfig") -> TargetType:
    return TargetType(
        name=config.name,
        regex=config.regex,
        pool_name=config.pool_name,
        resolved_ips=[
            ResolvedIPType(
                host=ip.host,
                port=ip.port,
                provider=ip.provider if ip.provider else None,
            )
            for ip in config.resolved_ips
        ],
        min_request_interval=config.min_request_interval,
        max_queue_wait=config.max_queue_wait,
        num_retries=config.num_retries,
        ip_failures_until_quarantine=config.ip_failures_until_quarantine,
        quarantine_time=config.quarantine_time,
        default_proxy_port=config.default_proxy_port,
        spoof_user_agent=config.spoof_user_agent,
        mutable=config.mutable,
    )


def pool_to_gql(pool: "IpPool") -> IpPoolType:
    return IpPoolType(
        name=pool.name,
        ip_requests=[
            IpRequestType(provider=req.provider, count=req.count)
            for req in pool.ip_requests
        ],
        mutable=pool.mutable,
    )


def provider_to_gql(provider: "ProxyProvider") -> ProviderType:
    return ProviderType(
        name=provider.name,
        ip_list=list(provider.ip_list),
        region_tag=provider.region_tag,
        mutable=provider.mutable,
        has_auth=provider.auth is not None,
    )
