"""Shared Backend + ConfigStore + ProxyRepository construction.

Both entry points that need a live ProxyRepository — core `cli.py`'s
`_run` and the webserver's `cli.py`'s `_run_admin` — used to duplicate this
block verbatim. Extracted here once both call sites needed to grow the same
config_store_url branch (see CONFIG_STORE_SCOPE.md Phase 4).

proxy-hopper-redis and proxy-hopper-sql are both optional packages — this
module only imports them lazily, inside build_repo(), exactly like each
call site already did for Redis before this extraction. Core proxy-hopper
does not (and must not) depend on either at the package level.
"""

from __future__ import annotations

import logging
from typing import Optional

from .backend.base import Backend
from .config import ServerConfig
from .config_store.base import ConfigStore
from .repository import ProxyRepository

logger = logging.getLogger(__name__)


async def build_repo(
    server: ServerConfig,
) -> Optional[tuple[Backend, ConfigStore, ProxyRepository]]:
    """Construct and start a Backend + ConfigStore, wrapped in a ProxyRepository.

    Returns None (after logging an error) if server.backend or
    server.config_store_url requests an optional package that isn't
    installed — callers should treat that the same as their own previous
    early-return-on-ImportError.
    """
    if server.backend == "redis":
        try:
            from proxy_hopper_redis import RedisBackend
        except ImportError:
            logger.error(
                "Redis backend requested but proxy-hopper-redis is not installed. "
                "Run: pip install proxy-hopper-redis"
            )
            return None
        backend: Backend = RedisBackend(server.redis_url)
    else:
        from .backend.memory import MemoryBackend
        backend = MemoryBackend()

    await backend.start()

    if server.config_store_url:
        try:
            from proxy_hopper_sql import SqlConfigStore
        except ImportError:
            logger.error(
                "config_store_url is set but proxy-hopper-sql is not installed. "
                "Run: pip install proxy-hopper-sql"
            )
            return None
        config_store: ConfigStore = SqlConfigStore(server.config_store_url)
    else:
        from .config_store.memory import MemoryConfigStore
        config_store = MemoryConfigStore()

    await config_store.start()

    repo = ProxyRepository(config_store=config_store, backend=backend)
    return backend, config_store, repo
