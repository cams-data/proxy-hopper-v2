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
# Start the proxy server
proxy-hopper run --config config.yaml

# Start the admin server (GraphQL API + web UI) as a separate process/deployment.
# Only sees live state when backend=redis — a separate process gets its own
# private in-memory backend when backend=memory, so this is a no-op for
# runtime CRUD/live IP state in that case (requires proxy-hopper-webserver).
proxy-hopper admin --config config.yaml

# Single-process mode: proxy + admin server together, sharing one backend
# directly. This is the only way to get a *live* admin API with the memory
# backend (requires proxy-hopper-webserver).
proxy-hopper run --config config.yaml --admin

# All proxy settings via environment variables (Docker / Kubernetes)
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


# Load the admin command from proxy-hopper-webserver if installed.
try:
    from proxy_hopper_webserver.cli import admin as _admin_cmd
    main.add_command(_admin_cmd, name="admin")
except ImportError:
    pass

# Load the migrate command from proxy-hopper-sql if installed.
try:
    from proxy_hopper_sql.cli import migrate as _migrate_cmd
    main.add_command(_migrate_cmd, name="migrate")
except ImportError:
    pass


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
@click.option("--config-store-url", default=None,
              envvar="PROXY_HOPPER_CONFIG_STORE_URL",
              help="SQLAlchemy URL for durable provider/pool/target config "
                   "(e.g. sqlite+aiosqlite:///./data/config.db or "
                   "postgresql+asyncpg://user:pass@host/db). Requires "
                   "proxy-hopper-sql. [default: unset — in-process "
                   "MemoryConfigStore, config does not survive a restart]")
@click.option("--probe/--no-probe", default=None,
              help="Enable background IP health prober.")
@click.option("--probe-interval", default=None, type=float,
              help="Seconds between probe rounds. [default: 60]")
@click.option("--probe-timeout", default=None, type=float,
              help="Per-probe HTTP timeout in seconds. [default: 10]")
@click.option("--probe-urls", default=None, metavar="URL[,URL...]",
              help="Comma-separated probe endpoints.")
@click.option("--admin/--no-admin", default=None,
              help="Run the admin server (GraphQL API + web UI) embedded in this "
                   "process, sharing its backend/repository directly instead of "
                   "connecting to a separately-deployed admin server. Requires "
                   "proxy-hopper-webserver. This is the only way to get a live "
                   "admin API with the memory backend, since a separately-run "
                   "'proxy-hopper admin' process cannot see another process's "
                   "in-memory state.")
@click.option("--admin-host", default=None,
              help="Interface to bind the embedded admin server. [default: 0.0.0.0]")
@click.option("--admin-port", default=None, type=int,
              help="Port for the embedded admin server. [default: 8081]")
@click.option("--admin-read-only/--no-admin-read-only", default=None,
              help="Reject GraphQL mutations on the admin API — all config comes "
                   "from the YAML file only. Queries (status, targets, pools, "
                   "providers, metrics) are unaffected. Independent of "
                   "--config-store-url. [default: false]")
@click.option("--prometheus-url", default=None,
              help="URL of an external Prometheus server the admin API can query "
                   "for the per-target metrics panel. When set, lightweight "
                   "in-process request counters (the alternative source for that "
                   "panel) are not recorded at all, to avoid instrumenting the "
                   "request path twice. [default: unset — use in-process counters]")
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
    config_store_url: Optional[str],
    probe: Optional[bool],
    probe_interval: Optional[float],
    probe_timeout: Optional[float],
    probe_urls: Optional[str],
    admin: Optional[bool],
    admin_host: Optional[str],
    admin_port: Optional[int],
    admin_read_only: Optional[bool],
    prometheus_url: Optional[str],
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
    if config_store_url is not None:
        server.config_store_url = config_store_url
    if probe is not None:
        server.probe = probe
    if probe_interval is not None:
        server.probe_interval = probe_interval
    if probe_timeout is not None:
        server.probe_timeout = probe_timeout
    if probe_urls is not None:
        server.probe_urls = [u.strip() for u in probe_urls.split(",") if u.strip()]
    if admin is not None:
        server.admin = admin
    if admin_host is not None:
        server.admin_host = admin_host
    if admin_port is not None:
        server.admin_port = admin_port
    if admin_read_only is not None:
        server.admin_read_only = admin_read_only
    if prometheus_url is not None:
        server.prometheus_url = prometheus_url

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
        if cfg.pools:
            click.echo(f"Pools: {len(cfg.pools)} defined.")
            for pool in cfg.pools:
                providers_in_pool = [req.provider for req in pool.ip_requests]
                click.echo(f"  {pool.name!r}: {len(pool.ip_requests)} request(s)"
                           + (f", providers={providers_in_pool}"))
        click.echo(f"Config OK — {len(cfg.targets)} target(s) defined.")
        for t in cfg.targets:
            ips = t.resolved_ips
            click.echo(f"  {t.name!r}: {len(ips)} IP(s), pool={t.pool_name!r}, regex={t.regex!r}")
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

    from .wiring import build_repo

    result = await build_repo(server)
    if result is None:
        return
    backend, config_store, repo = result

    from .pool_store import IPPoolStore
    from .events import EventBus

    pool_store = IPPoolStore(backend)
    event_bus = EventBus(backend)

    # Lightweight in-process per-target request counters, for the admin UI's
    # metrics panel. Skipped entirely when server.prometheus_url is set — the
    # admin API queries Prometheus server-side instead in that case, and
    # recording both would be pure overhead on the request hot path.
    app_metrics = None
    if not server.prometheus_url:
        from .app_metrics import AppMetricsStore
        app_metrics = AppMetricsStore(backend)

    # Per-IP reachability status from the background prober, for the admin
    # UI's Providers/Pools pages. Unlike app_metrics, always constructed —
    # the prober runs once per probe interval (tens of seconds), not once per
    # request, so recording here alongside Prometheus is negligible cost.
    from .ip_health import IpHealthStore
    ip_health_store = IpHealthStore(backend, probe_interval=server.probe_interval)

    # Seed providers, pools, and targets from YAML (write-if-not-exists).
    # Repository is the source of truth; YAML is only applied on first run.
    for p in providers:
        await repo.seed_provider(p)
    for pool in cfg.pools:
        await repo.seed_pool(pool)
    for t in targets:
        await repo.seed_target(t)

    # Build managers from the full repository state (YAML seeds + any prior
    # runtime mutations that survived across restarts in the backend).
    all_targets = await repo.list_targets()

    # Build TokenManager if auth_server is configured.
    token_manager = None
    if server.auth_server is not None:
        from .token_manager import TokenManager
        proxy_url = f"http://{server.host}:{server.port}"
        token_manager = TokenManager(
            config=server.auth_server,
            backend=backend,
            proxy_url=proxy_url if server.auth_server.expose_proxy_url else None,
        )

    managers = [
        TargetManager(
            t,
            pool_store,
            providers=providers,
            proxy_read_timeout=server.proxy_read_timeout,
            debug_quarantine=server.debug_quarantine,
            event_bus=event_bus,
            token_manager=token_manager,
            app_metrics=app_metrics,
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
        event_bus=event_bus,
        token_manager=token_manager,
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
            health_store=ip_health_store,
        )
        await prober.start()

    # --- Optionally build the embedded admin server ---
    # Runs in this same process/event loop, sharing `repo`/`event_bus`/`backend`
    # directly — the only way the admin API sees live state when backend=memory,
    # since a separately-run `proxy-hopper admin` process gets its own private
    # MemoryBackend that the proxy process can never write to.
    admin_uvicorn_server = None
    if server.admin:
        try:
            from proxy_hopper_webserver.app import create_admin_app
        except ImportError:
            log.error(
                "server.admin is enabled but proxy-hopper-webserver is not "
                "installed. Run: pip install proxy-hopper-webserver"
            )
            await backend.stop()
            await config_store.stop()
            if prober:
                await prober.stop()
            return
        import uvicorn
        admin_app = create_admin_app(
            cfg, runtime_secret, repo=repo, event_bus=event_bus, app_metrics=app_metrics,
            ip_health=ip_health_store,
        )
        admin_uvicorn_server = uvicorn.Server(uvicorn.Config(
            admin_app,
            host=server.admin_host,
            port=server.admin_port,
            log_level="error",
            access_log=False,
        ))
        admin_uvicorn_server.install_signal_handlers = lambda: None

    admin_task = None
    if admin_uvicorn_server is not None:
        admin_task = asyncio.create_task(
            admin_uvicorn_server.serve(), name="ph:cli:embedded-admin-server"
        )

        def _log_admin_task_failure(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                log.error("Embedded admin server failed: %s", exc, exc_info=exc)

        admin_task.add_done_callback(_log_admin_task_failure)

    try:
        await proxy.start()
        log.info(
            "Proxy Hopper running on %s:%d (backend=%s, auth=%s)",
            server.host, server.port, server.backend,
            "enabled" if cfg.auth.enabled else "disabled",
        )
        if admin_task is not None:
            log.info(
                "Admin server running on %s:%d (embedded — shares this process' backend)",
                server.admin_host, server.admin_port,
            )
        await proxy.serve_forever()
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutting down…")
    finally:
        await proxy.stop()
        if admin_task is not None:
            admin_uvicorn_server.should_exit = True
            admin_task.cancel()
            await asyncio.gather(admin_task, return_exceptions=True)
        if prober:
            await prober.stop()
        await backend.stop()
        await config_store.stop()


