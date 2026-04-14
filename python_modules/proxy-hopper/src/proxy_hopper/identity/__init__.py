"""Identity system — per-(IP, target) client persona management.

Public surface
--------------
``IdentityConfig``   — configuration model embedded in TargetConfig
``IdentityStore``    — per-target registry; the only class TargetManager imports
``Identity``         — the persona object (imported for type annotations)
``FingerprintProfile`` — immutable header bundle (imported for type annotations)
``VALID_PROFILE_NAMES`` — frozenset of recognised profile name strings
"""

from .config import IdentityConfig, WarmupConfig
from .fingerprint import VALID_PROFILE_NAMES, FingerprintProfile, get_profile
from .identity import Identity
from .store import IdentityStore

__all__ = [
    "IdentityConfig",
    "WarmupConfig",
    "FingerprintProfile",
    "VALID_PROFILE_NAMES",
    "get_profile",
    "Identity",
    "IdentityStore",
]
