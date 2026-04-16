"""Configuration loading for Proxy Hopper.

Priority order (highest → lowest):
  1. CLI arguments
  2. YAML config file  (server: block + proxyProviders / ipPools / targets)
  3. Environment variables (PROXY_HOPPER_*)

Target definitions, IP pools, and proxy providers live in the YAML file.
Server-level settings can live in the YAML ``server:`` block, fall back to
``PROXY_HOPPER_*`` env vars (read automatically by ``ServerConfig`` via
``pydantic-settings``), and can always be overridden at the CLI.

Full config file reference
--------------------------
::

    # ---------------------------------------------------------------------------
    # Proxy Providers (optional)
    # ---------------------------------------------------------------------------
    # Named proxy suppliers.  Each provider declares its own credentials and IP
    # list.  Providers are referenced from ipPools via `ipRequests`.
    #
    # Field reference
    # ~~~~~~~~~~~~~~~
    # name          (required) Unique identifier referenced from ipPools.
    # auth          (optional) Credentials block — omit entirely for open or IP-whitelisted proxies.
    #   type        Auth type: basic (default if omitted).
    #   username    Username for HTTP Basic auth.
    #   password    Password for HTTP Basic auth.
    # ipList        (required) List of proxy addresses provided by this supplier.
    #               Accepts "host:port", "host", or "scheme://host:port" forms.
    # regionTag     (optional) Region label attached to metrics — useful for
    #               comparing latency or failure rates across regions/providers.

    proxyProviders:
      - name: provider-au
        auth:
          type: basic
          username: user
          password: secret
        ipList:
          - "proxy-1.example.com:3128"
          - "proxy-2.example.com:3128"
        regionTag: Australia

      - name: provider-ca
        auth:
          type: basic
          username: user
          password: secret
        ipList:
          - "proxy-3.example.com:3128"
          - "proxy-4.example.com:3128"
        regionTag: Canada

    # ---------------------------------------------------------------------------
    # IP Pools (optional)
    # ---------------------------------------------------------------------------
    # Reusable, named IP address lists.  Reference them from targets with
    # `ipPool: <name>`.  IPs can come from providers via `ipRequests` (which
    # selects a random subset from a provider's list) or be listed inline.
    #
    # Field reference
    # ~~~~~~~~~~~~~~~
    # name          (required) Unique identifier referenced from targets.
    # ipRequests    Draw IPs from providers:
    #   provider    Name of a proxyProvider.
    #   count       How many IPs to randomly select from that provider's list.
    # ipList        Inline list of proxy addresses (alternative to ipRequests).
    #
    # ipRequests and ipList can be combined — all selected IPs are merged.

    ipPools:
      - name: pool-1
        ipRequests:
          - provider: provider-au
            count: 5
          - provider: provider-ca
            count: 5

      - name: pool-inline
        ipList:
          - "proxy-5.example.com:3128"

    # ---------------------------------------------------------------------------
    # Targets (required)
    # ---------------------------------------------------------------------------
    # Each target matches incoming request URLs by regex and routes them through
    # the configured proxy IPs.  Targets are evaluated top-to-bottom; the first
    # match wins.
    #
    # IP rotation and rate-limiting
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    # The purpose of proxy rotation is to respect per-IP API rate limits.
    # After each request (success or failure) an IP is held off the pool for
    # `minRequestInterval` seconds before being made available again.  If an IP
    # accumulates `ipFailuresUntilQuarantine` consecutive failures it is
    # quarantined for `quarantineTime` seconds, after which it is returned to
    # the pool with its failure counter reset.
    #
    # Field reference
    # ~~~~~~~~~~~~~~~
    # name                      (required) Human-readable label shown in logs/metrics.
    # regex                     (required) Python regex matched against the full request URL.
    # ipPool                    (required*) Name of a shared ipPool.
    # ipList                    (required*) Inline list of proxy addresses.
    #   * Exactly one of ipPool or ipList must be provided.
    # defaultProxyPort          Port applied to IPs listed without an explicit port. [default: 8080]
    # minRequestInterval        How long (seconds / duration string) an IP is held off
    #                           the pool after any request before being reused.
    #                           This is the primary rate-limit knob.             [default: 1s]
    # maxQueueWait              Maximum time (seconds / duration string) a request
    #                           will wait for a free IP before failing.          [default: 30s]
    # numRetries                How many times to retry a failed request using a
    #                           different IP before giving up.                   [default: 3]
    # ipFailuresUntilQuarantine Number of consecutive failures before an IP is
    #                           quarantined.                                     [default: 5]
    # quarantineTime            How long (seconds / duration string) a quarantined
    #                           IP is held out of the pool before being retried. [default: 2m]
    #
    # Duration strings: plain numbers are seconds; append 's', 'm', or 'h' for
    # seconds, minutes, or hours (e.g. "500ms" is not supported — use "0.5s").
    #
    # Identity (optional — disabled by default)
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    # Attaches a persistent client persona to each (IP, target) pair so that
    # consecutive requests through the same IP look like the same browser/client
    # to the upstream server.  Each identity carries a fingerprint header bundle
    # (User-Agent, Accept, Accept-Language, Accept-Encoding) and an optional
    # cookie jar that is maintained between requests.  The identity is rotated
    # automatically when the IP is quarantined, on a 429 response, or after a
    # configurable number of requests.
    #
    # identity:
    #   enabled             Master switch.                                       [default: false]
    #   cookies             Persist and replay session cookies per (IP, target). [default: true]
    #   profile             Fixed fingerprint profile name, or omit for random
    #                       selection per identity.
    #                       Valid values: chrome-windows, chrome-macos,
    #                       safari-macos, firefox-linux, firefox-windows.
    #   rotateAfterRequests Voluntarily rotate identity after N successful
    #                       requests.  Omit to disable.
    #   rotateOn429         Rotate identity immediately on a 429 response.       [default: true]
    #   warmup              Send a GET to this path through a fresh identity
    #                       before it enters service (collects session cookies).
    #     enabled           [default: true when warmup block is present]
    #     path              URL path for the warmup request.                     [default: /]

    targets:
      - name: general
        regex: '.*'
        ipPool: pool-1
        minRequestInterval: 2s
        maxQueueWait: 30s
        numRetries: 3
        ipFailuresUntilQuarantine: 5
        quarantineTime: 10m

      - name: strict-api
        regex: 'api[.]example[.]com'
        ipList:
          - "proxy-5.example.com:3128"
        minRequestInterval: 10s
        maxQueueWait: 60s
        numRetries: 1
        ipFailuresUntilQuarantine: 2
        quarantineTime: 30m
        identity:
          enabled: true
          cookies: true
          rotateAfterRequests: 50   # shed sessions before per-session quota is hit
          rotateOn429: true
          warmup:
            enabled: true
            path: /

    # ---------------------------------------------------------------------------
    # Server settings (optional — all have defaults)
    # ---------------------------------------------------------------------------
    # These can also be set via PROXY_HOPPER_* environment variables.
    # CLI flags take the highest priority and override both YAML and env vars.

    server:
      host: 0.0.0.0              # PROXY_HOPPER_HOST
      port: 8080                 # PROXY_HOPPER_PORT
      logLevel: INFO             # PROXY_HOPPER_LOG_LEVEL   (TRACE/DEBUG/INFO/WARNING/ERROR)
      logFormat: text            # PROXY_HOPPER_LOG_FORMAT  (text/json)
      logFile: null              # PROXY_HOPPER_LOG_FILE    (path, or omit for stderr)
      backend: memory            # PROXY_HOPPER_BACKEND     (memory/redis)
      redisUrl: redis://localhost:6379/0  # PROXY_HOPPER_REDIS_URL
      metrics: false             # PROXY_HOPPER_METRICS
      metricsPort: 9090          # PROXY_HOPPER_METRICS_PORT
      debugProbes: false         # PROXY_HOPPER_DEBUG_PROBES     — emit probe DEBUG/TRACE logs (requires logLevel: DEBUG)
      debugQuarantine: false     # PROXY_HOPPER_DEBUG_QUARANTINE — emit quarantine/pool DEBUG/TRACE logs (requires logLevel: DEBUG)
      debugBackend: false        # PROXY_HOPPER_DEBUG_BACKEND    — emit backend storage DEBUG/TRACE logs (requires logLevel: DEBUG)
      probe: true                # PROXY_HOPPER_PROBE
      probeInterval: 60          # PROXY_HOPPER_PROBE_INTERVAL  (seconds)
      probeTimeout: 10           # PROXY_HOPPER_PROBE_TIMEOUT   (seconds)
      probeUrls:                 # PROXY_HOPPER_PROBE_URLS      (comma-separated as env var)
        - http://1.1.1.1
        - http://www.google.com
      admin: false               # PROXY_HOPPER_ADMIN           — enable the admin REST API
      adminPort: 8081            # PROXY_HOPPER_ADMIN_PORT
      adminHost: 0.0.0.0        # PROXY_HOPPER_ADMIN_HOST

    # ---------------------------------------------------------------------------
    # Auth env var overrides (PROXY_HOPPER_AUTH_*)
    # ---------------------------------------------------------------------------
    # Selected auth fields can be injected via environment variables so that
    # secrets never need to appear in a config file or ConfigMap.
    #
    # Auth env vars take precedence over the auth: block in the YAML file.
    # This is the reverse of the server: block (where YAML beats env vars) and
    # is intentional — the typical pattern is to keep non-secret config in YAML
    # and inject secrets from the environment (Kubernetes Secret, Docker secret,
    # CI variable, etc.).
    #
    # PROXY_HOPPER_AUTH_ENABLED          — "true" / "false"
    # PROXY_HOPPER_AUTH_JWT_SECRET       — JWT signing secret
    # PROXY_HOPPER_AUTH_JWT_EXPIRY_MINUTES — token lifetime in minutes
    # PROXY_HOPPER_AUTH_ADMIN_PASSWORD_HASH — bcrypt hash for the admin user
    #                                      (ignored if auth.admin is not set in YAML)
    # PROXY_HOPPER_AUTH_OIDC_ISSUER      — OIDC issuer URL
    # PROXY_HOPPER_AUTH_OIDC_AUDIENCE    — expected 'aud' claim

    # ---------------------------------------------------------------------------
    # Auth (optional)
    # ---------------------------------------------------------------------------
    # Controls who can use the proxy and who can access the admin API.
    # When enabled, every proxy request must supply a valid credential in the
    # ``X-Proxy-Hopper-Auth: Bearer <token>`` header.
    #
    # SECURITY: when auth is enabled, store this block in a Secret (not a plain
    # ConfigMap) so credentials are not readable by unauthorised cluster users.
    # Use ``config.existingSecret`` in the Helm chart or mount a Secret volume.
    #
    # Field reference
    # ~~~~~~~~~~~~~~~
    # enabled           Master switch.  Default: false.
    # jwtSecret         HS256 signing secret for locally-issued tokens.
    #                   Omit (or leave blank) to auto-generate a random secret
    #                   at startup — tokens do not survive restarts in that case.
    # jwtExpiryMinutes  Lifetime of locally-issued tokens.  Default: 60.
    #
    # admin             Local admin user (username/password login via admin API).
    #   username        Login username.
    #   passwordHash    bcrypt hash — generate with: proxy-hopper hash-password <pw>
    #   role            Role assigned on login.  Default: admin.
    #
    # apiKeys           Static Bearer tokens for M2M proxy access.
    #                   API keys can only be used to make proxy requests — they
    #                   have no access to the admin API.
    #   name            Human-readable label shown in logs.
    #   key             The raw key value (sent as the Bearer token).
    #   targets         List of target names this key may access.
    #                   Use ["*"] (default) to allow all targets.
    #
    # oidc              Validate externally-issued JWTs (Authentik, Keycloak, etc.).
    #   issuer          OIDC issuer URL.  JWKS fetched from issuer/.well-known/…
    #   audience        Expected ``aud`` claim.  Leave blank to skip check.
    #   rolesClaim      JWT claim that carries the role name.
    #                   Default: proxy_hopper_role.
    #
    # roles             Custom role definitions (supplement the built-in roles).
    #   Built-in roles: admin (read+write+admin), operator (read+write), viewer (read).
    #   name            Role identifier referenced from apiKeys / admin / OIDC claim.
    #   permissions     List of: read, write, admin.
    #   targets         (optional) Restrict role to named targets only.
    #                   Omit to allow all targets.

    auth:
      enabled: true
      jwtSecret: "change-me-to-a-long-random-string"
      jwtExpiryMinutes: 60

      admin:
        username: admin
        passwordHash: "$2b$12$..."   # proxy-hopper hash-password <password>
        role: admin

      apiKeys:
        - name: my-service
          key: "ph_changeme"
          targets: ["*"]            # ["*"] = all targets (default), or list named targets

      oidc:
        issuer: "https://auth.example.com/application/o/proxy-hopper/"
        audience: "proxy-hopper"
        rolesClaim: proxy_hopper_role

      roles:
        - name: scraper
          permissions: [read, write]
          targets: [general]          # only this target
"""

from __future__ import annotations

import random
import re
from enum import Enum
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import EnvSettingsSource

from .identity.config import IdentityConfig, WarmupConfig  # noqa: F401  (re-exported for callers)


# ---------------------------------------------------------------------------
# Duration parsing
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

    def resolved_ip_list(self, default_port: int = 8080) -> list[tuple[str, int]]:
        """Return list of (host, port) tuples."""
        return [_parse_address(entry, default_port) for entry in self.ip_list]


# ---------------------------------------------------------------------------
# IP pool model (internal — resolved before TargetConfig is created)
# ---------------------------------------------------------------------------

class _ResolvedIPPool(BaseModel):
    """Internal: a pool after ipRequests have been resolved to a flat IP list."""
    name: str
    ip_list: list[str] = Field(min_length=1)


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
    resolved_ips: list[ResolvedIP] = Field(min_length=1)
    min_request_interval: float = Field(default=1.0)
    max_queue_wait: float = Field(default=30.0)
    num_retries: int = Field(default=3, ge=0)
    ip_failures_until_quarantine: int = Field(default=5, ge=1)
    quarantine_time: float = Field(default=120.0)
    default_proxy_port: int = Field(default=8080)
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    mutable: bool = Field(default=False)

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
# YAML normalisation helpers
# ---------------------------------------------------------------------------

_TARGET_CAMEL_TO_SNAKE: dict[str, str] = {
    "ipList": "ip_list",
    "ipPool": "ip_pool",
    "minRequestInterval": "min_request_interval",
    "maxRequestTimeInQueue": "max_queue_wait",
    "maxQueueWait": "max_queue_wait",
    "numRetries": "num_retries",
    "ipFailuresUntilQuarantine": "ip_failures_until_quarantine",
    "quarantineTime": "quarantine_time",
    "defaultProxyPort": "default_proxy_port",
    "mutable": "mutable",
}

_IDENTITY_CAMEL_TO_SNAKE: dict[str, str] = {
    "rotateAfterRequests": "rotate_after_requests",
    "rotateOn429": "rotate_on_429",
}

_IDENTITY_WARMUP_CAMEL_TO_SNAKE: dict[str, str] = {}  # no camelCase fields currently

_POOL_CAMEL_TO_SNAKE: dict[str, str] = {
    "ipList": "ip_list",
    "ipRequests": "ip_requests",
}

_PROVIDER_CAMEL_TO_SNAKE: dict[str, str] = {
    "ipList": "ip_list",
    "regionTag": "region_tag",
}

_AUTH_CAMEL_TO_SNAKE: dict[str, str] = {}

_SERVER_CAMEL_TO_SNAKE: dict[str, str] = {
    "logLevel": "log_level",
    "logFormat": "log_format",
    "logFile": "log_file",
    "redisUrl": "redis_url",
    "metricsPort": "metrics_port",
    "proxyReadTimeout": "proxy_read_timeout",
    "debugProbes": "debug_probes",
    "debugQuarantine": "debug_quarantine",
    "debugBackend": "debug_backend",
    "probeInterval": "probe_interval",
    "probeTimeout": "probe_timeout",
    "probeUrls": "probe_urls",
    "adminPort": "admin_port",
    "adminHost": "admin_host",
}

_AUTH_CAMEL_TO_SNAKE: dict[str, str] = {
    "apiKeys": "api_keys",
    "jwtSecret": "jwt_secret",
    "jwtExpiryMinutes": "jwt_expiry_minutes",
}

_AUTH_ADMIN_CAMEL_TO_SNAKE: dict[str, str] = {
    "passwordHash": "password_hash",
}

_AUTH_OIDC_CAMEL_TO_SNAKE: dict[str, str] = {
    "rolesClaim": "roles_claim",
}

_DURATION_FIELDS = {"min_request_interval", "max_queue_wait", "quarantine_time"}


def _normalise_identity(raw: dict) -> IdentityConfig:
    """Normalise and parse an ``identity:`` YAML block into an ``IdentityConfig``."""
    out = {_IDENTITY_CAMEL_TO_SNAKE.get(k, k): v for k, v in raw.items()}
    if "warmup" in out and isinstance(out["warmup"], dict):
        warmup_raw = {_IDENTITY_WARMUP_CAMEL_TO_SNAKE.get(k, k): v for k, v in out["warmup"].items()}
        out["warmup"] = WarmupConfig(**warmup_raw)
    return IdentityConfig(**out)


def _normalise_target(raw: dict) -> dict:
    out: dict = {}
    for key, value in raw.items():
        out[_TARGET_CAMEL_TO_SNAKE.get(key, key)] = value
    for field in _DURATION_FIELDS:
        if field in out and isinstance(out[field], str):
            out[field] = _parse_duration(out[field])
    if "identity" in out and isinstance(out["identity"], dict):
        out["identity"] = _normalise_identity(out["identity"])
    return out


def _normalise_pool(raw: dict) -> dict:
    return {_POOL_CAMEL_TO_SNAKE.get(k, k): v for k, v in raw.items()}


def _normalise_provider(raw: dict) -> dict:
    return {_PROVIDER_CAMEL_TO_SNAKE.get(k, k): v for k, v in raw.items()}


def _normalise_server(raw: dict) -> dict:
    out: dict = {}
    for key, value in raw.items():
        out[_SERVER_CAMEL_TO_SNAKE.get(key, key)] = value
    for field in ("probe_interval", "probe_timeout"):
        if field in out and isinstance(out[field], str):
            out[field] = _parse_duration(out[field])
    return out


# ---------------------------------------------------------------------------
# Auth normalisation
# ---------------------------------------------------------------------------

def _parse_auth(raw: dict) -> "AuthConfig":
    """Normalise and parse the top-level auth: YAML block."""
    if not raw:
        return AuthConfig()

    out = {_AUTH_CAMEL_TO_SNAKE.get(k, k): v for k, v in raw.items()}

    if "admin" in out and isinstance(out["admin"], dict):
        out["admin"] = {_AUTH_ADMIN_CAMEL_TO_SNAKE.get(k, k): v for k, v in out["admin"].items()}

    if "oidc" in out and isinstance(out["oidc"], dict):
        out["oidc"] = {_AUTH_OIDC_CAMEL_TO_SNAKE.get(k, k): v for k, v in out["oidc"].items()}

    # api_keys: list of dicts — no camelCase fields currently, but normalise for future safety
    if "api_keys" in out and isinstance(out["api_keys"], list):
        out["api_keys"] = [
            {k: v for k, v in ak.items()} for ak in out["api_keys"]
        ]

    return AuthConfig(**out)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class ProxyHopperConfig(BaseModel):
    """Top-level config object returned by load_config."""
    server: ServerConfig
    targets: list[TargetConfig]
    providers: list[ProxyProvider] = Field(default_factory=list)
    auth: AuthConfig = Field(default_factory=AuthConfig)


def load_config(path: Path | str) -> ProxyHopperConfig:
    """Load and return the full configuration from a YAML file.

    Resolution order:
      1. proxyProviders are parsed and indexed by name.
      2. ipPools resolve their ipRequests against providers, randomly sampling
         the requested count of IPs from each provider's list.
      3. Targets reference pools or inline ipLists; the result is a flat list
         of ResolvedIP objects that carry provider/region metadata.
      4. ServerConfig is constructed with YAML values as explicit kwargs, which
         pydantic-settings treats as higher priority than env vars — giving the
         correct chain: CLI > YAML > env vars > defaults.
    """
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}

    default_port = 8080  # used when parsing addresses without explicit ports

    # --- Proxy providers ----------------------------------------------------
    providers: list[ProxyProvider] = []
    provider_map: dict[str, ProxyProvider] = {}
    for p_raw in raw.get("proxyProviders", []):
        normalised = _normalise_provider(p_raw)
        # Normalise auth sub-block if present
        if "auth" in normalised and isinstance(normalised["auth"], dict):
            normalised["auth"] = BasicAuth(**normalised["auth"])
        provider = ProxyProvider(**normalised)
        if provider.name in provider_map:
            raise ValueError(f"Duplicate proxyProvider name: '{provider.name}'")
        provider_map[provider.name] = provider
        providers.append(provider)

    # --- IP pools -----------------------------------------------------------
    # Each pool resolves to a list of ResolvedIP (carries provider metadata).
    pool_map: dict[str, list[ResolvedIP]] = {}
    for pool_raw in raw.get("ipPools", []):
        normalised = _normalise_pool(pool_raw)
        pool_name = normalised.get("name")
        if not pool_name:
            raise ValueError("ipPool entry is missing a 'name' field")

        resolved: list[ResolvedIP] = []

        # ipRequests — draw from providers
        for req in normalised.get("ip_requests", []):
            provider_name = req.get("provider")
            count = req.get("count")
            if provider_name not in provider_map:
                raise ValueError(
                    f"ipPool '{pool_name}' references unknown provider '{provider_name}'. "
                    f"Defined providers: {list(provider_map)}"
                )
            provider = provider_map[provider_name]
            available = provider.resolved_ip_list(default_port)
            if count is not None and count > len(available):
                raise ValueError(
                    f"ipPool '{pool_name}' requests {count} IPs from provider "
                    f"'{provider_name}' but only {len(available)} are available."
                )
            selected = random.sample(available, count) if count is not None else list(available)
            for host, port in selected:
                resolved.append(ResolvedIP(
                    host=host,
                    port=port,
                    provider=provider.name,
                    region_tag=provider.region_tag or "",
                ))

        # ipList — inline IPs with no provider metadata
        for entry in normalised.get("ip_list", []):
            host, port = _parse_address(entry, default_port)
            resolved.append(ResolvedIP(host=host, port=port))

        if not resolved:
            raise ValueError(
                f"ipPool '{pool_name}' has no IPs — add ipRequests or ipList."
            )

        if pool_name in pool_map:
            raise ValueError(f"Duplicate ipPool name: '{pool_name}'")
        pool_map[pool_name] = resolved

    # --- Targets ------------------------------------------------------------
    targets: list[TargetConfig] = []
    for t_raw in raw.get("targets", []):
        normalised = _normalise_target(t_raw)
        target_name = normalised.get("name", "<unnamed>")
        default_proxy_port = normalised.get("default_proxy_port", default_port)

        pool_ref = normalised.pop("ip_pool", None)
        inline_ip_list = normalised.pop("ip_list", None)

        if pool_ref is not None and inline_ip_list is not None:
            raise ValueError(
                f"Target '{target_name}' specifies both ipPool and ipList — use one."
            )
        if pool_ref is None and inline_ip_list is None:
            raise ValueError(
                f"Target '{target_name}' must specify either ipPool or ipList."
            )

        if pool_ref is not None:
            if pool_ref not in pool_map:
                raise ValueError(
                    f"Target '{target_name}' references unknown ipPool '{pool_ref}'. "
                    f"Defined pools: {list(pool_map)}"
                )
            resolved_ips = pool_map[pool_ref]
        else:
            resolved_ips = [
                ResolvedIP(host=h, port=p)
                for h, p in (
                    _parse_address(entry, default_proxy_port)
                    for entry in inline_ip_list
                )
            ]

        targets.append(TargetConfig(resolved_ips=resolved_ips, **normalised))

    # --- Server settings ----------------------------------------------------
    yaml_server = _normalise_server(raw.get("server") or {})
    server = ServerConfig(**yaml_server)

    # --- Auth config --------------------------------------------------------
    auth = _parse_auth(raw.get("auth") or {})

    return ProxyHopperConfig(server=server, targets=targets, providers=providers, auth=auth)
