"""Redis-backed storage backend for Proxy Hopper."""

from .backend import RedisBackend, RedisIPPoolBackend

__all__ = [
    "RedisBackend",
    # Deprecated alias
    "RedisIPPoolBackend",
]
