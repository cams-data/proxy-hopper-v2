"""Logging configuration for Proxy Hopper.

Defines a custom TRACE level (below DEBUG) and provides configure_logging(),
which the CLI calls on startup.  Library users should configure logging
themselves via the standard logging module; this file does nothing until
configure_logging() is explicitly called.

Environment variables (all honoured by the CLI via auto_envvar_prefix):
    PROXY_HOPPER_LOG_LEVEL   — TRACE | DEBUG | INFO | WARNING | ERROR
    PROXY_HOPPER_LOG_FILE    — path to write log output (default: stderr)
    PROXY_HOPPER_LOG_FORMAT  — text | json  (default: text)
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Optional

# ---------------------------------------------------------------------------
# TRACE level  (numeric 5 — below DEBUG=10)
# ---------------------------------------------------------------------------

TRACE: int = 5
logging.addLevelName(TRACE, "TRACE")


def _trace(self: logging.Logger, msg: object, *args: object, **kwargs: object) -> None:
    if self.isEnabledFor(TRACE):
        self._log(TRACE, msg, args, **kwargs)  # type: ignore[arg-type]


logging.Logger.trace = _trace  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

class _TextFormatter(logging.Formatter):
    _FMT = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
    _DATE = "%Y-%m-%dT%H:%M:%S"

    def __init__(self) -> None:
        super().__init__(fmt=self._FMT, datefmt=self._DATE)


class _JsonFormatter(logging.Formatter):
    """Newline-delimited JSON — one object per log record.

    Suitable for Fluentd, Datadog, GCP Cloud Logging, etc.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(payload)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def configure_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
    log_format: str = "text",
) -> None:
    """Configure the root logger for the Proxy Hopper process.

    Parameters
    ----------
    level:
        One of TRACE, DEBUG, INFO, WARNING, ERROR (case-insensitive).
    log_file:
        Filesystem path to write logs to.  ``None`` (default) writes to
        stderr, which is the correct target for Docker / Kubernetes.
    log_format:
        ``"text"`` — human-readable colum-aligned lines.
        ``"json"`` — one JSON object per line for log aggregators.
    """
    numeric = logging.getLevelName(level.upper())
    if not isinstance(numeric, int):
        numeric = logging.INFO

    formatter: logging.Formatter = (
        _JsonFormatter() if log_format.lower() == "json" else _TextFormatter()
    )

    handler: logging.Handler
    if log_file:
        handler = logging.FileHandler(log_file, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stderr)

    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(numeric)
    # Replace any handlers added by earlier basicConfig calls (e.g. in tests)
    root.handlers.clear()
    root.addHandler(handler)
