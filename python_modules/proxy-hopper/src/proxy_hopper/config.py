"""Configuration loading for Proxy Hopper.

Only target definitions live in the YAML file.  Server-level settings
(host, port, log level, metrics) are passed via CLI flags or environment
variables using the PROXY_HOPPER_ prefix.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, Field, field_validator


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
# YAML loading — normalises camelCase keys and duration strings
# ---------------------------------------------------------------------------

_CAMEL_TO_SNAKE: dict[str, str] = {
    "ipList": "ip_list",
    "minRequestInterval": "min_request_interval",
    "maxRequestTimeInQueue": "max_queue_wait",
    "maxQueueWait": "max_queue_wait",
    "numRetries": "num_retries",
    "ipFailuresUntilQuarantine": "ip_failures_until_quarantine",
    "quarantineTime": "quarantine_time",
    "defaultProxyPort": "default_proxy_port",
}

_DURATION_FIELDS = {"min_request_interval", "max_queue_wait", "quarantine_time"}


def _normalise_target(raw: dict) -> dict:
    out: dict = {}
    for key, value in raw.items():
        out[_CAMEL_TO_SNAKE.get(key, key)] = value
    for field in _DURATION_FIELDS:
        if field in out and isinstance(out[field], str):
            out[field] = _parse_duration(out[field])
    return out


def load_config(path: Path | str) -> list[TargetConfig]:
    """Load and return the list of TargetConfig objects from a YAML file."""
    with open(path) as fh:
        raw = yaml.safe_load(fh)
    targets_raw: list[dict] = raw.get("targets", [])
    return [TargetConfig(**_normalise_target(t)) for t in targets_raw]
