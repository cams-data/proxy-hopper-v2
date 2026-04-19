"""YAML normalisation helpers — camelCase → snake_case and type coercion.

All ``_normalise_*`` functions accept raw dicts from ``yaml.safe_load`` and
return dicts ready to be passed to the corresponding Pydantic model constructors.
"""

from __future__ import annotations

from .models import (
    AuthConfig,
    IdentityConfig,
    IpPool,
    IpRequest,
    WarmupConfig,
    _parse_duration,
)


# ---------------------------------------------------------------------------
# camelCase → snake_case mapping tables
# ---------------------------------------------------------------------------

_TARGET_CAMEL_TO_SNAKE: dict[str, str] = {
    "ipPool": "ip_pool",
    "poolName": "pool_name",
    "minRequestInterval": "min_request_interval",
    "maxRequestTimeInQueue": "max_queue_wait",
    "maxQueueWait": "max_queue_wait",
    "numRetries": "num_retries",
    "ipFailuresUntilQuarantine": "ip_failures_until_quarantine",
    "quarantineTime": "quarantine_time",
    "defaultProxyPort": "default_proxy_port",
    "spoofUserAgent": "spoof_user_agent",
    "mutable": "mutable",
    "static": "static",
}

_IDENTITY_CAMEL_TO_SNAKE: dict[str, str] = {
    "rotateAfterRequests": "rotate_after_requests",
    "rotateOn429": "rotate_on_429",
}

_IDENTITY_WARMUP_CAMEL_TO_SNAKE: dict[str, str] = {}  # no camelCase fields currently

_POOL_CAMEL_TO_SNAKE: dict[str, str] = {
    "ipRequests": "ip_requests",
    "static": "static",
}

_IP_REQUEST_CAMEL_TO_SNAKE: dict[str, str] = {}  # no camelCase fields currently

_PROVIDER_CAMEL_TO_SNAKE: dict[str, str] = {
    "ipList": "ip_list",
    "regionTag": "region_tag",
    "static": "static",
}

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


# ---------------------------------------------------------------------------
# Normalisation functions
# ---------------------------------------------------------------------------

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
    out = {_POOL_CAMEL_TO_SNAKE.get(k, k): v for k, v in raw.items()}
    # Normalise each ip_request sub-dict (currently no-op, but future-proof)
    if "ip_requests" in out and isinstance(out["ip_requests"], list):
        out["ip_requests"] = [
            {_IP_REQUEST_CAMEL_TO_SNAKE.get(k, k): v for k, v in req.items()}
            if isinstance(req, dict) else req
            for req in out["ip_requests"]
        ]
    return out


def _normalise_pool_to_model(raw: dict) -> IpPool:
    """Normalise a raw ipPool YAML block and return an IpPool model."""
    normalised = _normalise_pool(raw)
    requests = [
        IpRequest(**req) if isinstance(req, dict) else req
        for req in normalised.get("ip_requests", [])
    ]
    return IpPool(
        name=normalised["name"],
        ip_requests=requests,
        mutable=normalised.get("mutable", True),
        static=normalised.get("static", True),
    )


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


def _parse_auth(raw: dict) -> AuthConfig:
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
