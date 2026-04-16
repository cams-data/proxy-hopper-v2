"""Backend package — storage primitive implementations."""

from .base import Backend, IPPoolBackend
from .memory import MemoryBackend, MemoryIPPoolBackend

__all__ = [
    "Backend",
    "MemoryBackend",
    # Deprecated aliases
    "IPPoolBackend",
    "MemoryIPPoolBackend",
]
