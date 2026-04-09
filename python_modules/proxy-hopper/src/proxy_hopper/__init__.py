"""Proxy Hopper — rotating proxy server."""

import logging
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("proxy-hopper")
except PackageNotFoundError:
    __version__ = "unknown"

# Registers the TRACE level (5) on logging.Logger so that logger.trace()
# is available throughout the package.  Must be imported here so the level
# is active whenever proxy_hopper is imported (including in tests).
from .logging_config import TRACE as _TRACE  # noqa: F401

# Library best practice: add NullHandler so callers that don't configure
# logging don't get "No handlers could be found for logger" warnings.
logging.getLogger(__name__).addHandler(logging.NullHandler())
