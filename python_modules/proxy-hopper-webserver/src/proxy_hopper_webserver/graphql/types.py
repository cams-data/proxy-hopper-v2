"""Strawberry output types and domain-object → GraphQL type converters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import strawberry

if TYPE_CHECKING:
    from proxy_hopper.app_metrics import TargetMetricsSnapshot
    from proxy_hopper.config import IpPool, ProxyProvider, TargetConfig


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
    static: bool


@strawberry.type
class IpRequestType:
    provider: str
    count: int


@strawberry.type
class IpPoolType:
    name: str
    ip_requests: list[IpRequestType]
    mutable: bool
    static: bool


@strawberry.type
class ProviderType:
    name: str
    ip_list: list[str]
    region_tag: Optional[str]
    mutable: bool
    static: bool
    has_auth: bool


@strawberry.type
class KeyValueType:
    name: str
    value: str


@strawberry.type
class IpRuntimeStateType:
    address: str
    host: str
    port: int
    provider: Optional[str]
    failures: int
    quarantined: bool
    release_at: Optional[float]
    user_agent: Optional[str]
    request_count: int
    cookies_enabled: bool
    profile_headers: list[KeyValueType]
    cookies: list[KeyValueType]
    identity_enabled: bool


@strawberry.type
class StatusType:
    auth_enabled: bool
    user_sub: str
    user_role: str
    read_only: bool


@strawberry.type
class TargetMetricsType:
    name: str
    total_requests: int
    success_requests: int
    failed_requests: int
    avg_latency_ms: float
    last_request_at: Optional[str]


def target_to_gql(config: "TargetConfig") -> TargetType:
    return TargetType(
        name=config.name,
        regex=config.regex,
        pool_name=config.pool_name,
        resolved_ips=[
            ResolvedIPType(host=ip.host, port=ip.port, provider=ip.provider or None)
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
        static=config.static,
    )


def pool_to_gql(pool: "IpPool") -> IpPoolType:
    return IpPoolType(
        name=pool.name,
        ip_requests=[
            IpRequestType(provider=req.provider, count=req.count)
            for req in pool.ip_requests
        ],
        mutable=pool.mutable,
        static=pool.static,
    )


def provider_to_gql(provider: "ProxyProvider") -> ProviderType:
    return ProviderType(
        name=provider.name,
        ip_list=list(provider.ip_list),
        region_tag=provider.region_tag,
        mutable=provider.mutable,
        static=provider.static,
        has_auth=provider.auth is not None,
    )


def target_metrics_to_gql(snapshot: "TargetMetricsSnapshot") -> TargetMetricsType:
    return TargetMetricsType(
        name=snapshot.name,
        total_requests=snapshot.total_requests,
        success_requests=snapshot.success_requests,
        failed_requests=snapshot.failed_requests,
        avg_latency_ms=snapshot.avg_latency_ms,
        last_request_at=snapshot.last_request_at,
    )
