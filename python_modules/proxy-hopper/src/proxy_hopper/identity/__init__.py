"""Identity system — per-(IP, target) client persona management.

Public surface
--------------
``IdentityConfig``    — configuration model embedded in TargetConfig
``WarmupConfig``      — optional warmup request configuration
``Identity``          — the persona object; stored in Backend KV, not in-process
``FingerprintProfile`` — immutable header bundle generated at identity creation
``get_profile``       — returns a randomly generated FingerprintProfile
"""

from .config import IdentityConfig, WarmupConfig
from .fingerprint import FingerprintProfile, get_profile
from .identity import Identity

__all__ = [
    "IdentityConfig",
    "WarmupConfig",
    "FingerprintProfile",
    "get_profile",
    "Identity",
]
