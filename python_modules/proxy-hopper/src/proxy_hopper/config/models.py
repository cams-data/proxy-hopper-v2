"""Pydantic models for Proxy Hopper configuration.

Contains all model classes plus low-level parsing helpers (_parse_duration,
_parse_address) that the models reference directly.

YAML schema overview
--------------------
A config file has five top-level keys:

  server:         ServerConfig — operational settings (port, backend, logging…)
  auth:           AuthConfig  — access control (API keys, JWT, OIDC, admin user)
  proxyProviders: list[ProxyProvider] — upstream proxy suppliers with IP lists
  ipPools:        list[IpPool]        — named pools that draw IPs from providers
  targets:        list[TargetConfig]  — routing rules that reference an ipPool

All duration fields (minRequestInterval, maxQueueWait, quarantineTime, …)
accept a string with suffix: ``30s``, ``5m``, ``1h``, or a bare number (seconds).

All camelCase YAML keys are normalised to snake_case before model construction;
both forms are accepted in config files.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import EnvSettingsSource

from ..identity.config import IdentityConfig, WarmupConfig  # noqa: F401  (re-exported for callers)


# ---------------------------------------------------------------------------
# Duration / address parsing helpers
# ---------------------------------------------------------------------------

def _parse_duration(value: str | int | float) -> float:
    """Parse a duration string like '2s', '5m', '1h' into seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    value = value.strip()
    if value.endswith("h"):
        return float(value[:-1]) * 3600
    if value.endswith("m"):
        return float(value[:-1]) * 60
    if value.endswith("s"):
        return float(value[:-1])
    return float(value)


def _parse_address(entry: str, default_port: int) -> tuple[str, int]:
    """Parse a proxy address string into (host, port).

    Accepts:
      - "host:port"
      - "host"
      - "scheme://host:port"  (scheme is discarded)
    """
    if "://" in entry:
        parsed = urlparse(entry)
        host = parsed.hostname or entry
        port = parsed.port or default_port
        return host, port
    if ":" in entry:
        host, _, port_str = entry.rpartition(":")
        return host, int(port_str)
    return entry, default_port


# ---------------------------------------------------------------------------
# Auth models — proxy provider credentials (upstream proxy auth)
# ---------------------------------------------------------------------------

class BasicAuth(BaseModel):
    type: str = "basic"
    username: str
    password: str = ""


# ---------------------------------------------------------------------------
# Auth config — proxy-hopper access control
# ---------------------------------------------------------------------------

class Permission(str, Enum):
    """Permissions that can be assigned to roles."""
    read = "read"
    write = "write"
    admin = "admin"


class RoleConfig(BaseModel):
    """Custom role definition. If not defined, built-in roles apply."""
    permissions: list[str] = Field(default_factory=list)
    targets: list[str] = Field(default_factory=lambda: ["*"])


class ApiKeyConfig(BaseModel):
    """A named API key for machine-to-machine proxy access."""
    name: str
    key: str
    targets: list[str] = Field(default_factory=lambda: ["*"])


class AdminUserConfig(BaseModel):
    """Local admin user credentials for the management UI."""
    username: str
    password_hash: str
    role: str = "admin"


class OidcConfig(BaseModel):
    """OIDC provider configuration for SSO."""
    issuer: str
    audience: Optional[str] = None
    roles_claim: str = "proxy_hopper_role"


class AuthConfig(BaseModel):
    """Top-level access-control configuration.

    YAML key: ``auth``

    Fields
    ------
    enabled
        Master switch.  When False all auth checks are skipped.  Default: False.
    jwtSecret (jwt_secret)
        Secret used to sign locally-issued JWTs (login via ``POST /auth/login``).
        Use a long random string.  If omitted, a secret is auto-generated at
        startup — tokens will not survive a restart in that case.
    jwtExpiryMinutes (jwt_expiry_minutes)
        Lifetime of issued JWTs in minutes.  Default: ``60``.
    admin
        Local admin user for the management UI / admin API.
        Sub-keys: ``username``, ``passwordHash`` (bcrypt, use
        ``proxy-hopper hash-password``), ``role`` (default ``"admin"``).
    apiKeys (api_keys)
        List of static API keys for machine-to-machine proxy access.
        Each key has ``name``, ``key``, and ``targets`` (list of target names
        or ``["*"]`` for all targets).
    oidc
        OIDC provider for SSO.  Sub-keys: ``issuer``, ``audience`` (optional),
        ``rolesClaim`` (JWT claim that maps to a role, default
        ``"proxy_hopper_role"``).
    roles
        Map of role name → ``RoleConfig`` for custom role definitions.
        Built-in roles (``read``, ``write``, ``admin``) apply when not
        overridden here.
    """
    enabled: bool = False
    admin: Optional[AdminUserConfig] = None
    api_keys: list[ApiKeyConfig] = Field(default_factory=list)
    oidc: Optional[OidcConfig] = None
    jwt_secret: str = ""
    jwt_expiry_minutes: int = 60
    roles: dict[str, RoleConfig] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Proxy provider model
# ---------------------------------------------------------------------------

class ProxyProvider(BaseModel):
    """A named upstream proxy supplier.

    YAML key: ``proxyProviders``

    Fields
    ------
    name
        Unique identifier referenced by ``ipPools[].ipRequests[].provider``.
    ipList (ip_list)
        List of proxy addresses as ``"host:port"`` strings.  At least one
        address is required.
    regionTag (region_tag)
        Optional free-form region label (e.g. ``"US"``, ``"AU"``).  Exposed
        on resolved IPs for routing or logging purposes.
    auth
        Optional Basic Auth credentials sent to the upstream proxy
        (``Proxy-Authorization: Basic …``).  Sub-keys: ``username``, ``password``.
    mutable
        When False, the provider cannot be modified or deleted via the API.
        Default: True.
    static
        When True (the default for YAML-defined providers), the provider is
        always overwritten from config on startup and rejects API mutations.
        Set ``static: false`` to allow the API to manage this provider at
        runtime while still seeding it on first run.  Default: False for
        API-created providers; True for YAML-defined providers.
    """
    name: str
    auth: Optional[BasicAuth] = None
    ip_list: list[str] = Field(min_length=1)
    region_tag: Optional[str] = None
    mutable: bool = True
    static: bool = False

    def resolved_ip_list(self, default_port: int = 8080) -> list[tuple[str, int]]:
        """Return list of (host, port) tuples."""
        return [_parse_address(entry, default_port) for entry in self.ip_list]


# ---------------------------------------------------------------------------
# IP pool models — first-class runtime entities
# ---------------------------------------------------------------------------

class IpRequest(BaseModel):
    """A single provider draw within an ipPool.

    Fields
    ------
    provider
        Name of the ``proxyProvider`` to draw IPs from.
    count
        Maximum number of IPs to take from that provider.  If the provider
        has fewer IPs than requested, all available IPs are used without error.
    """
    provider: str
    count: int = Field(ge=1)


class IpPool(BaseModel):
    """A named pool of IPs assembled from one or more provider draws.

    YAML key: ``ipPools``

    Pools are first-class runtime entities — they can be created, updated, and
    deleted via the admin API (subject to ``mutable`` / ``static`` flags).

    Fields
    ------
    name
        Unique identifier referenced by ``targets[].ipPool``.
    ipRequests (ip_requests)
        List of provider draws.  Each draw names a provider and a count; the
        pool's resolved IP list is the union of all draws in order.
    mutable
        When False, the pool cannot be modified or deleted via the API.
        Default: True.
    static
        Same semantics as ``ProxyProvider.static``.  Default: False for
        API-created pools; True for YAML-defined pools.
    """
    name: str
    ip_requests: list[IpRequest] = Field(min_length=1)
    mutable: bool = True
    static: bool = False


# ---------------------------------------------------------------------------
# IP address entry — carries provider metadata alongside host/port
# ---------------------------------------------------------------------------

class ResolvedIP(BaseModel):
    """A single proxy IP address with its origin provider metadata."""
    host: str
    port: int
    provider: str = ""       # provider name, or "" for inline IPs
    region_tag: str = ""     # provider region tag, or "" for inline IPs

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


# ---------------------------------------------------------------------------
# Target config model
# ---------------------------------------------------------------------------

class TargetConfig(BaseModel):
    """Routing rule that matches requests by URL regex and forwards them via an IP pool.

    YAML key: ``targets``

    Fields
    ------
    name
        Unique identifier for this target.
    regex
        Regular expression matched against the full request URL.  The first
        target whose regex matches is used.
    ipPool (pool_name)
        Name of the ``ipPool`` that supplies proxy IPs for this target.
    minRequestInterval (min_request_interval)
        Minimum seconds between two consecutive requests through the same IP.
        Accepts a duration string (``"1s"``, ``"20s"``).  Default: ``1.0``.
    maxQueueWait (max_queue_wait)
        Maximum seconds a request will wait for a free IP before giving up.
        Default: ``30.0``.
    numRetries (num_retries)
        Number of times a failed request is retried on a different IP before
        the error is returned to the client.  Default: ``3``.
    ipFailuresUntilQuarantine (ip_failures_until_quarantine)
        Consecutive failures on one IP before it is quarantined.  Default: ``5``.
    quarantineTime (quarantine_time)
        Seconds a quarantined IP is held back before re-entering the pool.
        Accepts a duration string.  Default: ``120.0``.
    defaultProxyPort (default_proxy_port)
        Port used when an IP address in the pool has no explicit port.
        Default: ``8080``.
    spoofUserAgent (spoof_user_agent)
        When True, replaces the ``User-Agent`` header with a random browser UA.
        Can be overridden per-request with ``X-Proxy-Hopper-User-Agent``.
        Default: True.
    identity
        Identity persistence and rotation settings.  See ``IdentityConfig``.
        All sub-options default to off; set ``identity.enabled: true`` to
        activate cookie persistence and session rotation for this target.
    mutable
        When False, the target cannot be modified or deleted via the API.
        Default: True.
    static
        Same semantics as ``ProxyProvider.static``.  Default: False for
        API-created targets; True for YAML-defined targets.
    resolved_ips
        Snapshot of the pool's IP list at load time.  Set automatically by
        the loader and repository; do not set this in YAML.
    """
    name: str
    regex: str
    pool_name: str
    resolved_ips: list[ResolvedIP] = Field(min_length=1)
    min_request_interval: float = Field(default=1.0)
    max_queue_wait: float = Field(default=30.0)
    num_retries: int = Field(default=3, ge=0)
    ip_failures_until_quarantine: int = Field(default=5, ge=1)
    quarantine_time: float = Field(default=120.0)
    default_proxy_port: int = Field(default=8080)
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    spoof_user_agent: bool = Field(default=True)
    mutable: bool = Field(default=True)
    static: bool = Field(default=False)

    @field_validator("regex")
    @classmethod
    def validate_regex(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"Invalid regex '{v}': {exc}") from exc
        return v

    def compiled_regex(self) -> re.Pattern:
        return re.compile(self.regex)

    def resolved_ip_list(self) -> list[tuple[str, int]]:
        """Return list of (host, port) tuples — for backward compat with pool/prober."""
        return [(ip.host, ip.port) for ip in self.resolved_ips]

    def ip_list(self) -> list[str]:
        """Return flat list of 'host:port' strings."""
        return [ip.address for ip in self.resolved_ips]


# ---------------------------------------------------------------------------
# Server config — BaseSettings reads PROXY_HOPPER_* env vars automatically.
#
# Priority is enforced in load_config():
#   CLI args (applied in cli.py) > YAML server: block > env vars > defaults
#
# pydantic-settings resolves: init kwargs > env vars > field defaults.
# Passing the YAML server: block as init kwargs therefore puts it above env
# vars — exactly the priority order we want for the YAML/env layer.
# ---------------------------------------------------------------------------

class ServerConfig(BaseSettings):
    """Operational settings for the proxy server process.

    YAML key: ``server``
    Env var prefix: ``PROXY_HOPPER_``  (e.g. ``PROXY_HOPPER_PORT=8085``)

    Fields
    ------
    host
        Interface to bind.  Default: ``"0.0.0.0"``.
    port
        Port the proxy server listens on.  Default: ``8080``.
    log_level (logLevel)
        Verbosity: ``TRACE``, ``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``.
        Default: ``INFO``.
    log_format (logFormat)
        ``text`` (human-readable) or ``json`` (structured).  Default: ``text``.
    log_file (logFile)
        Optional path to write logs to instead of stderr.
    backend
        Storage backend for IP pool and identity state.
        ``memory`` (single-process, lost on restart) or ``redis``.
        Default: ``memory``.
    redis_url (redisUrl)
        Redis connection URL.  Used when ``backend=redis``.
        Default: ``"redis://localhost:6379/0"``.
    proxy_read_timeout (proxyReadTimeout)
        Optional timeout in seconds for reading the upstream response body.
        Omit to use aiohttp's default (no explicit read timeout).
    admin
        Enable the admin API / GraphQL endpoint.  Default: False.
    admin_port (adminPort)
        Port the admin API listens on.  Default: ``8081``.
    admin_host (adminHost)
        Interface the admin API binds to.  Default: ``"0.0.0.0"``.
    metrics
        Enable Prometheus ``/metrics`` endpoint.  Default: False.
    metrics_port (metricsPort)
        Port for the Prometheus metrics HTTP server.  Default: ``9090``.
    probe
        Enable background IP health prober.  Default: True.
    probe_interval (probeInterval)
        Seconds between probe rounds.  Default: ``60``.
    probe_timeout (probeTimeout)
        Per-probe HTTP timeout in seconds.  Default: ``10``.
    probe_urls (probeUrls)
        Comma-separated list of URLs used by the prober to test IPs.
        Default: ``["http://1.1.1.1", "http://www.google.com"]``.
    debug_quarantine (debugQuarantine)
        Log verbose quarantine / cooldown events at DEBUG level.  Default: False.
    debug_probes (debugProbes)
        Log verbose probe results at DEBUG level.  Default: False.
    debug_backend (debugBackend)
        Log verbose backend storage operations at DEBUG level.  Default: False.
    """
    model_config = SettingsConfigDict(
        env_prefix="PROXY_HOPPER_",
        env_ignore_empty=True,
        case_sensitive=False,
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"
    log_format: str = "text"
    log_file: Optional[str] = None
    backend: str = "memory"
    redis_url: str = "redis://localhost:6379/0"
    metrics: bool = False
    metrics_port: int = 9090
    proxy_read_timeout: Optional[float] = None
    debug_probes: bool = False
    debug_quarantine: bool = False
    debug_backend: bool = False
    probe: bool = True
    probe_interval: float = 60.0
    probe_timeout: float = 10.0
    probe_urls: list[str] = Field(
        default_factory=lambda: ["http://1.1.1.1", "http://www.google.com"]
    )
    admin: bool = False
    admin_port: int = 8081
    admin_host: str = "0.0.0.0"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Swap in a subclass of EnvSettingsSource that handles comma-separated
        # list[str] env vars (e.g. PROXY_HOPPER_PROBE_URLS=a,b,c) instead of
        # requiring JSON array syntax (["a","b","c"]).
        class _CommaSplitEnvSource(EnvSettingsSource):
            _COMMA_FIELDS = {"probe_urls"}

            def prepare_field_value(self, field_name, field, value, value_is_complex):
                if field_name in self._COMMA_FIELDS and isinstance(value, str):
                    return [u.strip() for u in value.split(",") if u.strip()]
                return super().prepare_field_value(field_name, field, value, value_is_complex)

        return (
            init_settings,
            _CommaSplitEnvSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )


# ---------------------------------------------------------------------------
# Top-level config object
# ---------------------------------------------------------------------------

class ProxyHopperConfig(BaseModel):
    """Top-level config object returned by load_config."""
    server: ServerConfig
    targets: list[TargetConfig]
    providers: list[ProxyProvider] = Field(default_factory=list)
    pools: list[IpPool] = Field(default_factory=list)
    auth: AuthConfig = Field(default_factory=AuthConfig)
