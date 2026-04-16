"""CLI entry point for Proxy Hopper.

Config priority (highest → lowest):
  1. CLI arguments
  2. YAML config file  (server: block)
  3. Environment variables (PROXY_HOPPER_*)

Only settings that are operationally useful to override at the command line
have explicit CLI flags.  Everything else can be set in the YAML ``server:``
block or via environment variables.

Usage examples
--------------
# Minimal — all settings from env vars or YAML server: block
proxy-hopper run --config config.yaml

# Override specific settings at the command line
proxy-hopper run --config config.yaml --port 9000 --log-level DEBUG

# All server settings via environment variables (Docker / Kubernetes)
PROXY_HOPPER_CONFIG=/etc/proxy-hopper/config.yaml \\
PROXY_HOPPER_PORT=8080 \\
PROXY_HOPPER_LOG_LEVEL=INFO \\
PROXY_HOPPER_LOG_FORMAT=json \\
PROXY_HOPPER_BACKEND=redis \\
PROXY_HOPPER_REDIS_URL=redis://redis:6379/0 \\
PROXY_HOPPER_METRICS=true \\
PROXY_HOPPER_PROBE=true \\
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

# Note: we do NOT use auto_envvar_prefix here — env vars are read inside
# ServerConfig.from_yaml_and_env() so the priority chain is preserved.
_CTX: dict = {}


@click.group()
def main() -> None:
    """Proxy Hopper — rotating proxy server."""


@main.command("hash-password")
@click.argument("password")
def hash_password_cmd(password: str) -> None:
    """Hash PASSWORD for use in auth.admin.passwordHash config."""
    from .auth import hash_password
    click.echo(hash_password(password))


@main.command()
@click.option("--config", "-c", required=False, default=None,
              envvar="PROXY_HOPPER_CONFIG",
              type=click.Path(exists=True, path_type=Path),
              help="Path to targets YAML config file.")
@click.option("--host", default=None,
              help="Interface to bind the proxy server. [default: 0.0.0.0]")
@click.option("--port", default=None, type=int,
              help="Port for the proxy server. [default: 8080]")
@click.option("--log-level", default=None,
              type=click.Choice(["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"],
                                case_sensitive=False),
              help="Log verbosity level. [default: INFO]")
@click.option("--log-format", default=None,
              type=click.Choice(["text", "json"], case_sensitive=False),
              help="Log output format. [default: text]")
@click.option("--log-file", default=None, metavar="PATH",
              help="Write logs to this file instead of stderr.")
@click.option("--metrics/--no-metrics", default=None,
              help="Enable Prometheus /metrics endpoint.")
@click.option("--metrics-port", default=None, type=int,
              help="Port for the Prometheus metrics HTTP server. [default: 9090]")
@click.option("--backend", default=None,
              type=click.Choice(["memory", "redis"], case_sensitive=False),
              help="IP pool backend. [default: memory]")
@click.option("--redis-url", default=None,
              envvar="PROXY_HOPPER_REDIS_URL",
              help="Redis connection URL. [default: redis://localhost:6379/0]")
@click.option("--probe/--no-probe", default=None,
              help="Enable background IP health prober.")
@click.option("--probe-interval", default=None, type=float,
              help="Seconds between probe rounds. [default: 60]")
@click.option("--probe-timeout", default=None, type=float,
              help="Per-probe HTTP timeout in seconds. [default: 10]")
@click.option("--probe-urls", default=None, metavar="URL[,URL...]",
              help="Comma-separated probe endpoints.")
def run(
    config: Optional[Path],
    host: Optional[str],
    port: Optional[int],
    log_level: Optional[str],
    log_format: Optional[str],
    log_file: Optional[str],
    metrics: Optional[bool],
    metrics_port: Optional[int],
    backend: Optional[str],
    redis_url: Optional[str],
    probe: Optional[bool],
    probe_interval: Optional[float],
    probe_timeout: Optional[float],
    probe_urls: Optional[str],
) -> None:
    """Start the proxy server."""
    # --- Load config (YAML > env vars) ---
    if config is None:
        click.echo(
            "Error: --config / PROXY_HOPPER_CONFIG is required.", err=True
        )
        sys.exit(1)

    cfg = load_config(config)
    server = cfg.server

    # --- Apply CLI overrides (highest priority) ---
    if host is not None:
        server.host = host
    if port is not None:
        server.port = port
    if log_level is not None:
        server.log_level = log_level
    if log_format is not None:
        server.log_format = log_format
    if log_file is not None:
        server.log_file = log_file
    if metrics is not None:
        server.metrics = metrics
    if metrics_port is not None:
        server.metrics_port = metrics_port
    if backend is not None:
        server.backend = backend
    if redis_url is not None:
        server.redis_url = redis_url
    if probe is not None:
        server.probe = probe
    if probe_interval is not None:
        server.probe_interval = probe_interval
    if probe_timeout is not None:
        server.probe_timeout = probe_timeout
    if probe_urls is not None:
        server.probe_urls = [u.strip() for u in probe_urls.split(",") if u.strip()]

    # --- Start logging ---
    configure_logging(
        level=server.log_level,
        log_file=server.log_file,
        log_format=server.log_format,
    )

    # Suppress backend storage-level logs unless explicitly requested.
    # INFO and above always pass through; DEBUG/TRACE are suppressed by default
    # because they are only useful when diagnosing backend implementation issues.
    if not server.debug_backend:
        for _backend_logger in ("proxy_hopper.backend.memory", "proxy_hopper_redis.backend"):
            logging.getLogger(_backend_logger).setLevel(logging.WARNING)

    # --- Start metrics server ---
    if server.metrics:
        from .metrics import start_metrics_server
        start_metrics_server(server.metrics_port)

    # --- Run ---
    try:
        import uvloop
        uvloop.run(_run(cfg.targets, cfg.providers, server, cfg))
    except ImportError:
        asyncio.run(_run(cfg.targets, cfg.providers, server, cfg))


@main.command()
@click.option("--config", "-c", required=True, envvar="PROXY_HOPPER_CONFIG",
              type=click.Path(exists=True, path_type=Path))
def validate(config: Path) -> None:
    """Validate a configuration file and exit."""
    try:
        cfg = load_config(config)
        if cfg.providers:
            click.echo(f"Providers: {len(cfg.providers)} defined.")
            for p in cfg.providers:
                click.echo(f"  {p.name!r}: {len(p.ip_list)} IP(s)"
                           + (f", region={p.region_tag!r}" if p.region_tag else "")
                           + (", auth=basic" if p.auth else ", auth=none"))
        click.echo(f"Config OK — {len(cfg.targets)} target(s) defined.")
        for t in cfg.targets:
            ips = t.resolved_ips
            providers_used = {ip.provider for ip in ips if ip.provider}
            click.echo(f"  {t.name!r}: {len(ips)} IP(s), regex={t.regex!r}"
                       + (f", providers={sorted(providers_used)}" if providers_used else ""))
        click.echo(f"Server defaults: host={cfg.server.host}, port={cfg.server.port}, "
                   f"backend={cfg.server.backend}")
    except Exception as exc:
        click.echo(f"Config error: {exc}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Async run helper
# ---------------------------------------------------------------------------

async def _run(targets, providers, server, cfg=None) -> None:
    from .auth import make_runtime_secret
    from .config import AuthConfig, ProxyHopperConfig
    from .server import ProxyServer
    from .target_manager import TargetManager

    log = logging.getLogger(__name__)

    # Build a minimal cfg if called without one (e.g. from tests or legacy callers).
    if cfg is None:
        cfg = ProxyHopperConfig(server=server, targets=targets, auth=AuthConfig())

    # Resolve JWT signing secret once; shared between proxy auth and admin API.
    runtime_secret = make_runtime_secret(cfg.auth.jwt_secret)

    if server.backend == "redis":
        try:
            from proxy_hopper_redis import RedisBackend
        except ImportError:
            log.error(
                "Redis backend requested but proxy-hopper-redis is not installed. "
                "Run: pip install proxy-hopper-redis"
            )
            return
        backend = RedisBackend(server.redis_url)
    else:
        from .backend.memory import MemoryBackend
        backend = MemoryBackend()

    await backend.start()

    from .pool_store import IPPoolStore
    from .repository import ProxyRepository

    pool_store = IPPoolStore(backend)
    repo = ProxyRepository(backend)

    # Seed providers and targets from YAML (write-if-not-exists).
    # Repository is the source of truth; YAML is only applied on first run.
    for p in providers:
        await repo.seed_provider(p)
    for t in targets:
        await repo.seed_target(t)

    # Build managers from the full repository state (YAML seeds + any prior
    # runtime mutations that survived across restarts in the backend).
    all_targets = await repo.list_targets()

    managers = [
        TargetManager(
            t,
            pool_store,
            providers=providers,
            proxy_read_timeout=server.proxy_read_timeout,
            debug_quarantine=server.debug_quarantine,
        )
        for t in all_targets
    ]
    proxy = ProxyServer(
        managers,
        host=server.host,
        port=server.port,
        auth_config=cfg.auth if cfg.auth.enabled else None,
        runtime_secret=runtime_secret,
        pool_store=pool_store,
        repository=repo,
        providers=providers,
        proxy_read_timeout=server.proxy_read_timeout,
        debug_quarantine=server.debug_quarantine,
    )

    prober = None
    if server.probe:
        from .prober import IPProber
        prober = IPProber(
            providers=providers,
            targets=targets,
            probe_urls=server.probe_urls,
            interval=server.probe_interval,
            timeout=server.probe_timeout,
            debug=server.debug_probes,
        )
        await prober.start()

    # Start admin API if enabled
    admin_task = None
    if server.admin:
        from .auth.admin import run_admin_server
        admin_task = asyncio.create_task(
            run_admin_server(cfg, runtime_secret, repo=repo),
            name="ph:admin",
        )

    try:
        await proxy.start()
        log.info(
            "Proxy Hopper running on %s:%d (backend=%s, auth=%s)",
            server.host, server.port, server.backend,
            "enabled" if cfg.auth.enabled else "disabled",
        )
        await proxy.serve_forever()
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutting down…")
    finally:
        await proxy.stop()
        if admin_task is not None:
            admin_task.cancel()
            await asyncio.gather(admin_task, return_exceptions=True)
        if prober:
            await prober.stop()
        await backend.stop()
