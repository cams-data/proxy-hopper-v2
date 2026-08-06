"""CLI for proxy-hopper-webserver — admin server entrypoint."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

import click

from proxy_hopper.config import load_config
from proxy_hopper.logging_config import configure_logging


@click.group()
def main() -> None:
    """Proxy Hopper web server — admin API and UI."""


@main.command()
@click.option("--config", "-c", required=False, default=None,
              envvar="PROXY_HOPPER_CONFIG",
              type=click.Path(exists=True, path_type=Path),
              help="Path to config file.")
@click.option("--host", default=None, envvar="PROXY_HOPPER_ADMIN_HOST",
              help="Interface to bind the admin server. [default: 0.0.0.0]")
@click.option("--port", default=None, type=int, envvar="PROXY_HOPPER_ADMIN_PORT",
              help="Port for the admin server. [default: 8081]")
@click.option("--log-level", default=None, envvar="PROXY_HOPPER_LOG_LEVEL",
              type=click.Choice(["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"],
                                case_sensitive=False))
@click.option("--log-format", default=None, envvar="PROXY_HOPPER_LOG_FORMAT",
              type=click.Choice(["text", "json"], case_sensitive=False))
@click.option("--log-file", default=None, metavar="PATH", envvar="PROXY_HOPPER_LOG_FILE")
@click.option("--backend", default=None, envvar="PROXY_HOPPER_BACKEND",
              type=click.Choice(["memory", "redis"], case_sensitive=False))
@click.option("--redis-url", default=None, envvar="PROXY_HOPPER_REDIS_URL")
@click.option("--config-store-url", default=None, envvar="PROXY_HOPPER_CONFIG_STORE_URL")
@click.option("--admin-read-only/--no-admin-read-only", default=None,
              envvar="PROXY_HOPPER_ADMIN_READ_ONLY")
def admin(
    config: Optional[Path],
    host: Optional[str],
    port: Optional[int],
    log_level: Optional[str],
    log_format: Optional[str],
    log_file: Optional[str],
    backend: Optional[str],
    redis_url: Optional[str],
    config_store_url: Optional[str],
    admin_read_only: Optional[bool],
) -> None:
    """Start the admin server (GraphQL API + web UI).

    With ``backend: redis``, connects to the same Redis instance as the proxy
    runners and sees live operational state — deploy as a separate pod with a
    single replica. With ``backend: memory``, this process gets its own
    private in-process backend the proxy process cannot write to, so it will
    only ever show YAML-seeded operational state, never live runtime state —
    use ``proxy-hopper run --admin`` instead for a memory-backend deployment,
    which runs both in one process sharing one backend directly.

    ``config_store_url`` is independent of the above: pointing this and the
    proxy runners at the same SQLite/Postgres URL makes admin-API-created
    provider/pool/target config visible to both, regardless of ``backend`` —
    durable config sharing no longer requires the redis backend.

    ``admin_read_only`` is independent of both: it rejects every GraphQL
    mutation regardless of ``config_store_url``, leaving the admin API
    usable for monitoring only (status, targets, pools, providers,
    metrics) — the "no database, YAML-only config" deployment kind.
    """
    if config is None:
        click.echo("Error: --config / PROXY_HOPPER_CONFIG is required.", err=True)
        sys.exit(1)

    cfg = load_config(config)
    server = cfg.server

    if host is not None:
        server.admin_host = host
    if port is not None:
        server.admin_port = port
    if log_level is not None:
        server.log_level = log_level
    if log_format is not None:
        server.log_format = log_format
    if log_file is not None:
        server.log_file = log_file
    if backend is not None:
        server.backend = backend
    if redis_url is not None:
        server.redis_url = redis_url
    if config_store_url is not None:
        server.config_store_url = config_store_url
    if admin_read_only is not None:
        server.admin_read_only = admin_read_only

    configure_logging(
        level=server.log_level,
        log_file=server.log_file,
        log_format=server.log_format,
    )

    if not server.debug_backend:
        for _logger in ("proxy_hopper.backend.memory", "proxy_hopper_redis.backend"):
            logging.getLogger(_logger).setLevel(logging.WARNING)

    try:
        import uvloop
        uvloop.run(_run_admin(cfg))
    except ImportError:
        asyncio.run(_run_admin(cfg))


async def _run_admin(cfg) -> None:
    from proxy_hopper.auth import make_runtime_secret
    from proxy_hopper.pool_store import IPPoolStore
    from proxy_hopper.wiring import build_repo

    from .app import run_admin_server

    log = logging.getLogger(__name__)
    server = cfg.server
    runtime_secret = make_runtime_secret(cfg.auth.jwt_secret)

    result = await build_repo(server)
    if result is None:
        return
    backend, config_store, repo = result

    from proxy_hopper.events import EventBus

    event_bus = EventBus(backend)

    # Only meaningful when backend=redis — this process shares that Redis
    # instance with the proxy runners, so it sees their counters. With
    # backend=memory this reads an empty, disconnected store (see the core
    # README's Admin API section); harmless, just always zero.
    app_metrics = None
    if not server.prometheus_url:
        from proxy_hopper.app_metrics import AppMetricsStore
        app_metrics = AppMetricsStore(backend)

    for p in cfg.providers:
        await repo.seed_provider(p)
    for pool in cfg.pools:
        await repo.seed_pool(pool)
    for t in cfg.targets:
        await repo.seed_target(t)

    log.info(
        "Admin server starting on %s:%d (backend=%s)",
        server.admin_host, server.admin_port, server.backend,
    )

    try:
        await run_admin_server(cfg, runtime_secret, repo=repo, event_bus=event_bus, app_metrics=app_metrics)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Admin server shutting down…")
    finally:
        await backend.stop()
        await config_store.stop()
