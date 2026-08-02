"""Backend package — storage primitive implementations."""

from .base import Backend
from .memory import MemoryBackend

__all__ = [
    "Backend",
    "MemoryBackend",
]
