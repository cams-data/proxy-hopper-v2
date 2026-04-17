"""Pydantic models for Proxy Hopper configuration.

Contains all model classes plus low-level parsing helpers (_parse_duration,
_parse_address) that the models reference directly.
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
    """Top-level auth configuration block."""
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
    """A named proxy supplier with credentials, IPs, and optional region tag."""
    name: str
    auth: Optional[BasicAuth] = None
    ip_list: list[str] = Field(min_length=1)
    region_tag: Optional[str] = None
    mutable: bool = True

    def resolved_ip_list(self, default_port: int = 8080) -> list[tuple[str, int]]:
        """Return list of (host, port) tuples."""
        return [_parse_address(entry, default_port) for entry in self.ip_list]


# ---------------------------------------------------------------------------
# IP pool models — first-class runtime entities
# ---------------------------------------------------------------------------

class IpRequest(BaseModel):
    """A request for IPs from a named provider — declares count only, not which IPs."""
    provider: str
    count: int = Field(ge=1)


class IpPool(BaseModel):
    """A named pool of IPs assembled from one or more provider requests."""
    name: str
    ip_requests: list[IpRequest] = Field(min_length=1)
    mutable: bool = True


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
