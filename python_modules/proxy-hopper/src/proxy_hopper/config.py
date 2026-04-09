"""Configuration loading for Proxy Hopper.

Priority order (highest → lowest):
  1. CLI arguments
  2. YAML config file  (server: block + ipPools / targets)
  3. Environment variables (PROXY_HOPPER_*)

Target definitions and IP pools live in the YAML file.  Server-level settings
can live in the YAML ``server:`` block, fall back to ``PROXY_HOPPER_*`` env
vars (read automatically by ``ServerConfig`` via ``pydantic-settings``), and
can always be overridden at the CLI.

Full config file reference
--------------------------
::

    # ---------------------------------------------------------------------------
    # IP Pools (optional)
    # ---------------------------------------------------------------------------
    # Reusable, named IP address lists.  Reference them from targets with
    # `ipPool: <name>` instead of repeating IP lists across multiple targets.

    ipPools:
      - name: pool-1
        ipList:
          - "proxy-1.example.com:3128"
          - "proxy-2.example.com:3128"

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
    # ipList                    (required*) List of proxy addresses ("host:port" or "host").
    # ipPool                    (required*) Name of a shared ipPool — alternative to ipList.
    #   * Exactly one of ipList or ipPool must be provided.
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

    targets:
      - name: general
        regex: '.*'
        ipPool: pool-1              # reference a named pool …
        minRequestInterval: 2s      # hold each IP off for 2s between uses
        maxQueueWait: 30s
        numRetries: 3
        ipFailuresUntilQuarantine: 5
        quarantineTime: 10m

      - name: strict-api
        regex: 'api[.]example[.]com'
        ipList:                     # … or provide IPs inline
          - "proxy-3.example.com:3128"
          - "proxy-4.example.com:3128"
        minRequestInterval: 10s     # strict rate limit — only one req per IP per 10s
        maxQueueWait: 60s
        numRetries: 1
        ipFailuresUntilQuarantine: 2
        quarantineTime: 30m

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
      probe: false               # PROXY_HOPPER_PROBE
      probeInterval: 60          # PROXY_HOPPER_PROBE_INTERVAL  (seconds)
      probeTimeout: 10           # PROXY_HOPPER_PROBE_TIMEOUT   (seconds)
      probeUrls:                 # PROXY_HOPPER_PROBE_URLS      (comma-separated as env var)
        - https://1.1.1.1
        - https://www.google.com
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import EnvSettingsSource


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


# ---------------------------------------------------------------------------
# IP pool model
# ---------------------------------------------------------------------------

class IPPool(BaseModel):
    """A named, reusable list of proxy IP addresses."""
    name: str
    ip_list: list[str] = Field(min_length=1)


# ---------------------------------------------------------------------------
# Target config model
# ---------------------------------------------------------------------------

class TargetConfig(BaseModel):
    name: str
    regex: str
    ip_list: list[str] = Field(min_length=1)
    min_request_interval: float = Field(default=1.0)
    max_queue_wait: float = Field(default=30.0)
    num_retries: int = Field(default=3, ge=0)
    ip_failures_until_quarantine: int = Field(default=5, ge=1)
    quarantine_time: float = Field(default=120.0)
    default_proxy_port: int = Field(default=8080)

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
        """Return list of (host, port) tuples, applying default_proxy_port where needed."""
        result: list[tuple[str, int]] = []
        for entry in self.ip_list:
            if ":" in entry:
                host, _, port_str = entry.rpartition(":")
                result.append((host, int(port_str)))
            else:
                result.append((entry, self.default_proxy_port))
        return result


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
    probe: bool = False
    probe_interval: float = 60.0
    probe_timeout: float = 10.0
    probe_urls: list[str] = Field(
        default_factory=lambda: ["https://1.1.1.1", "https://www.google.com"]
    )

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
}

_POOL_CAMEL_TO_SNAKE: dict[str, str] = {
    "ipList": "ip_list",
}

_SERVER_CAMEL_TO_SNAKE: dict[str, str] = {
    "logLevel": "log_level",
    "logFormat": "log_format",
    "logFile": "log_file",
    "redisUrl": "redis_url",
    "metricsPort": "metrics_port",
    "probeInterval": "probe_interval",
    "probeTimeout": "probe_timeout",
    "probeUrls": "probe_urls",
}

_DURATION_FIELDS = {"min_request_interval", "max_queue_wait", "quarantine_time"}


def _normalise_target(raw: dict) -> dict:
    out: dict = {}
    for key, value in raw.items():
        out[_TARGET_CAMEL_TO_SNAKE.get(key, key)] = value
    for field in _DURATION_FIELDS:
        if field in out and isinstance(out[field], str):
            out[field] = _parse_duration(out[field])
    return out


def _normalise_pool(raw: dict) -> dict:
    return {_POOL_CAMEL_TO_SNAKE.get(k, k): v for k, v in raw.items()}


def _normalise_server(raw: dict) -> dict:
    out: dict = {}
    for key, value in raw.items():
        out[_SERVER_CAMEL_TO_SNAKE.get(key, key)] = value
    for field in ("probe_interval", "probe_timeout"):
        if field in out and isinstance(out[field], str):
            out[field] = _parse_duration(out[field])
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class ProxyHopperConfig(BaseModel):
    """Top-level config object returned by load_config."""
    server: ServerConfig
    targets: list[TargetConfig]


def load_config(path: Path | str) -> ProxyHopperConfig:
    """Load and return the full configuration from a YAML file.

    IP pool references are resolved before TargetConfig objects are created,
    so callers always see a flat ``ip_list`` on each target.

    ServerConfig is constructed with YAML values as explicit kwargs, which
    pydantic-settings treats as higher priority than env vars.  This gives
    us the correct chain: CLI > YAML > env vars > defaults — with the CLI
    layer applied afterwards in cli.py.
    """
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}

    # --- IP pools -----------------------------------------------------------
    pools: dict[str, list[str]] = {}
    for pool_raw in raw.get("ipPools", []):
        pool = IPPool(**_normalise_pool(pool_raw))
        pools[pool.name] = pool.ip_list

    # --- Targets ------------------------------------------------------------
    targets: list[TargetConfig] = []
    for t_raw in raw.get("targets", []):
        normalised = _normalise_target(t_raw)

        pool_ref = normalised.pop("ip_pool", None)
        if pool_ref is not None and "ip_list" not in normalised:
            if pool_ref not in pools:
                raise ValueError(
                    f"Target '{normalised.get('name')}' references unknown ipPool '{pool_ref}'. "
                    f"Defined pools: {list(pools)}"
                )
            normalised["ip_list"] = pools[pool_ref]
        elif pool_ref is not None and "ip_list" in normalised:
            raise ValueError(
                f"Target '{normalised.get('name')}' specifies both ipPool and ipList — use one."
            )

        targets.append(TargetConfig(**normalised))

    # --- Server settings ----------------------------------------------------
    # Pass YAML values as explicit kwargs — pydantic-settings gives init
    # kwargs priority over env vars, so YAML naturally wins over env vars
    # while env vars still win over field defaults.
    yaml_server = _normalise_server(raw.get("server") or {})
    server = ServerConfig(**yaml_server)

    return ProxyHopperConfig(server=server, targets=targets)
