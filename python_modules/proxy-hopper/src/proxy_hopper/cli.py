"""CLI entry point for Proxy Hopper.

All server-level settings are available as both CLI flags and environment
variables (PROXY_HOPPER_* prefix), following the 12-factor app convention.
Only the targets configuration (which changes per deployment) stays in a YAML
file.

Usage examples
--------------
# Minimal
proxy-hopper run --config config.yaml

# All options via flags
proxy-hopper run --config config.yaml --host 0.0.0.0 --port 8080 \\
    --log-level DEBUG --log-format json --metrics --metrics-port 9090

# All options via environment variables (Docker / Kubernetes)
PROXY_HOPPER_CONFIG=/etc/proxy-hopper/config.yaml \\
PROXY_HOPPER_PORT=8080 \\
PROXY_HOPPER_LOG_LEVEL=INFO \\
PROXY_HOPPER_LOG_FORMAT=json \\
PROXY_HOPPER_LOG_FILE=/var/log/proxy-hopper.log \\
PROXY_HOPPER_METRICS=true \\
PROXY_HOPPER_METRICS_PORT=9090 \\
proxy-hopper run

# Validate config only
proxy-hopper validate --config config.yaml
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

import click

from .config import load_config
from .logging_config import configure_logging

# auto_envvar_prefix causes Click to read PROXY_HOPPER_<OPTION_NAME> for every
# option, providing the Docker / K8s-friendly env-var interface for free.
_CTX = {"auto_envvar_prefix": "PROXY_HOPPER"}


@click.group(context_settings=_CTX)
def main() -> None:
    """Proxy Hopper — rotating proxy server."""


@main.command(context_settings=_CTX)
@click.option("--config", "-c", required=True, envvar="PROXY_HOPPER_CONFIG",
              type=click.Path(exists=True, path_type=Path),
              help="Path to targets YAML config file.")
@click.option("--host", default="0.0.0.0", show_default=True,
              help="Interface to bind the proxy server.")
@click.option("--port", default=8080, show_default=True, type=int,
              help="Port for the proxy server.")
@click.option("--log-level", default="INFO", show_default=True,
              type=click.Choice(["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"],
                                case_sensitive=False),
              help="Log verbosity level. TRACE is extremely verbose.")
@click.option("--log-format", default="text", show_default=True,
              type=click.Choice(["text", "json"], case_sensitive=False),
              help="Log output format. Use 'json' for log aggregators (Fluentd, Datadog, etc.).")
@click.option("--log-file", default=None, metavar="PATH",
              help="Write logs to this file instead of stderr.")
@click.option("--metrics/--no-metrics", default=False, show_default=True,
              help="Enable Prometheus /metrics endpoint.")
@click.option("--metrics-port", default=9090, show_default=True, type=int,
              help="Port for the Prometheus metrics HTTP server.")
@click.option("--backend", default="memory",
              type=click.Choice(["memory", "redis"], case_sensitive=False),
              show_default=True,
              help="IP pool backend. Use 'redis' for HA multi-instance deployments.")
@click.option("--redis-url", default="redis://localhost:6379/0", show_default=True,
              envvar="PROXY_HOPPER_REDIS_URL",
              help="Redis connection URL (required when --backend=redis).")
def run(
    config: Path,
    host: str,
    port: int,
    log_level: str,
    log_format: str,
    log_file: Optional[str],
    metrics: bool,
    metrics_port: int,
    backend: str,
    redis_url: str,
) -> None:
    """Start the proxy server."""
    configure_logging(level=log_level, log_file=log_file, log_format=log_format)

    if metrics:
        from .metrics import start_metrics_server
        start_metrics_server(metrics_port)

    targets = load_config(config)
    asyncio.run(_run(targets, host, port, backend, redis_url))


@main.command(context_settings=_CTX)
@click.option("--config", "-c", required=True, envvar="PROXY_HOPPER_CONFIG",
              type=click.Path(exists=True, path_type=Path))
def validate(config: Path) -> None:
    """Validate a configuration file and exit."""
    try:
        targets = load_config(config)
        click.echo(f"Config OK — {len(targets)} target(s) defined.")
        for t in targets:
            ips = t.resolved_ip_list()
            click.echo(f"  {t.name!r}: {len(ips)} IP(s), regex={t.regex!r}")
    except Exception as exc:
        click.echo(f"Config error: {exc}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Async run helper
# ---------------------------------------------------------------------------

async def _run(targets, host: str, port: int, backend_type: str, redis_url: str) -> None:
    from .backend.memory import MemoryIPPoolBackend
    from .server import ProxyServer
    from .target_manager import TargetManager

    log = logging.getLogger(__name__)

    if backend_type == "redis":
        try:
            from proxy_hopper_redis import RedisIPPoolBackend
        except ImportError:
            log.error(
                "Redis backend requested but proxy-hopper-redis is not installed. "
                "Run: pip install proxy-hopper-redis"
            )
            return
        pool_backend = RedisIPPoolBackend(redis_url)
    else:
        pool_backend = MemoryIPPoolBackend()

    await pool_backend.start()

    managers = [TargetManager(t, pool_backend) for t in targets]
    proxy = ProxyServer(managers, host=host, port=port)

    try:
        await proxy.start()
        log.info("Proxy Hopper running on %s:%d (backend=%s)", host, port, backend_type)
        await proxy.serve_forever()
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutting down…")
    finally:
        await proxy.stop()
        await pool_backend.stop()
