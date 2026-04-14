"""Request handler abstraction for Proxy Hopper.

The only built-in interaction mode is ``ForwardingHandler`` — clients set
``X-Proxy-Hopper-Target`` to the real destination and send requests to
proxy-hopper as if it were the target server.  Proxy-hopper owns the full
HTTPS request, enabling retries across different IPs on 429 / 5xx responses.

Per-request control headers
----------------------------
All ``X-Proxy-Hopper-*`` headers are stripped before forwarding to upstream.

``X-Proxy-Hopper-Target: https://api.example.com``
    **Required.** The real destination scheme + host (+ optional base path).
    Proxy-hopper prepends this to the request path to reconstruct the URL::

        X-Proxy-Hopper-Target: https://api.example.com
        GET /v1/data?q=1  →  https://api.example.com/v1/data?q=1

        X-Proxy-Hopper-Target: https://api.example.com/v2
        GET /search        →  https://api.example.com/v2/search

``X-Proxy-Hopper-Tag: <string>``
    Optional free-form label propagated to Prometheus metrics as the ``tag``
    label on ``proxy_hopper_requests_total`` and ``proxy_hopper_responses_total``.
    Use this to break down metrics by API endpoint or use-case::

        X-Proxy-Hopper-Tag: endpoint=/v1/search

``X-Proxy-Hopper-Retries: <int>``
    Optional per-request retry count override.  Overrides the target's
    ``numRetries`` setting for this request only.  Must be a non-negative
    integer; invalid values are silently ignored and the target default is used.

Adding a new mode
-----------------
1. Subclass ``RequestHandler`` and implement ``can_handle`` + ``handle``.
2. Add the class to ``_MODE_REGISTRY`` with a string key.
3. Add the key to ``_HANDLER_ORDER``.

That is the complete change surface — ``ProxyServer`` and ``_dispatch`` need
no modification.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlparse

import aiohttp

from .metrics import get_metrics
from .models import PendingRequest, ProxyResponse

if TYPE_CHECKING:
    from .config import AuthConfig
    from .target_manager import TargetManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_MAX_BODY_SIZE = 65_536 * 160   # 10 MiB
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailers", "transfer-encoding", "upgrade",
})

# ---------------------------------------------------------------------------
# Per-request control headers (all stripped before forwarding upstream)
# ---------------------------------------------------------------------------

_TARGET_HEADER  = "x-proxy-hopper-target"
_TAG_HEADER     = "x-proxy-hopper-tag"
_RETRIES_HEADER = "x-proxy-hopper-retries"
_AUTH_HEADER    = "x-proxy-hopper-auth"

# ---------------------------------------------------------------------------
# Low-level I/O helpers
# ---------------------------------------------------------------------------

_REASONS: dict[int, str] = {
    200: "OK", 201: "Created", 204: "No Content",
    301: "Moved Permanently", 302: "Found", 304: "Not Modified",
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
    404: "Not Found", 405: "Method Not Allowed", 408: "Request Timeout",
    413: "Request Entity Too Large", 429: "Too Many Requests",
    500: "Internal Server Error", 502: "Bad Gateway",
    503: "Service Unavailable", 504: "Gateway Timeout",
}


def _reason(status: int) -> str:
    return _REASONS.get(status, "Unknown")


def _write_error(writer: asyncio.StreamWriter, status: int, message: str) -> None:
    body = message.encode()
    writer.write(
        (
            f"HTTP/1.1 {status} {_reason(status)}\r\n"
            f"Content-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            "\r\n"
        ).encode() + body
    )


def _write_raw(writer: asyncio.StreamWriter, data: bytes) -> None:
    writer.write(data)


def _write_http_response(
    writer: asyncio.StreamWriter,
    response: ProxyResponse,
    http_version: str,
) -> None:
    status_line = f"{http_version} {response.status} {_reason(response.status)}\r\n"
    header_lines = "".join(
        f"{k}: {v}\r\n"
        for k, v in response.headers.items()
        if k.lower() not in _HOP_BY_HOP
    )
    writer.write((status_line + header_lines + "\r\n").encode("latin-1") + response.body)


# ---------------------------------------------------------------------------
# Shared request-dispatch helper
# ---------------------------------------------------------------------------

def _find_first_manager(
    managers: list[TargetManager], url: str
) -> TargetManager | None:
    return next((m for m in managers if m.matches(url)), None)


async def _submit_and_respond(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    method: str,
    url: str,
    http_version: str,
    headers: dict[str, str],
    managers: list[TargetManager],
    *,
    tag: str = "",
    num_retries_override: int | None = None,
) -> None:
    """Read body → find manager → submit → await response → write reply."""
    # --- Read body ---
    body: bytes | None = None
    content_length = headers.get("content-length")
    if content_length:
        try:
            length = int(content_length)
            if length > _MAX_BODY_SIZE:
                _write_error(writer, 413, "Request Entity Too Large")
                await writer.drain()
                return
            body = await reader.readexactly(length)
        except (ValueError, asyncio.IncompleteReadError):
            _write_error(writer, 400, "Bad Request")
            await writer.drain()
            return

    # --- Route to manager ---
    manager = _find_first_manager(managers, url)
    if manager is None:
        logger.warning("No target matched for %s %s", method, url)
        get_metrics().record_request("unmatched", "no_match", 0.0, tag=tag)
        _write_error(writer, 502, f"No target configured for URL: {url}")
        await writer.drain()
        return

    # --- Submit and await ---
    num_retries = (
        num_retries_override
        if num_retries_override is not None
        else manager._config.num_retries
    )
    future: asyncio.Future[ProxyResponse] = asyncio.get_running_loop().create_future()
    pending = PendingRequest(
        method=method,
        url=url,
        headers=dict(headers),
        body=body,
        future=future,
        arrival_time=time.monotonic(),
        max_queue_wait=manager._config.max_queue_wait,
        num_retries=num_retries,
        tag=tag,
    )
    await manager.submit(pending)

    try:
        response: ProxyResponse = await asyncio.wait_for(
            future, timeout=manager._config.max_queue_wait + 5
        )
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning(
            "ProxyServer: %s %s — gateway timeout waiting for response (queue_wait=%.1fs)",
            method, url, manager._config.max_queue_wait,
        )
        _write_error(writer, 504, "Gateway Timeout")
        await writer.drain()
        return
    except Exception as exc:
        logger.warning(
            "ProxyServer: %s %s — unexpected error: %s",
            method, url, exc,
        )
        _write_error(writer, 502, f"Bad Gateway: {exc}")
        await writer.drain()
        return

    if response.status >= 400:
        detail = ""
        ct = response.headers.get("Content-Type", "")
        if "json" in ct and response.body:
            try:
                import json as _json
                parsed = _json.loads(response.body)
                detail = f" — {parsed.get('error', '')} {parsed.get('detail', '')}".rstrip()
            except Exception:
                pass
        logger.warning(
            "ProxyServer: %s %s → %d%s",
            method, url, response.status, detail,
        )

    get_metrics().record_response(manager._config.name, response.status, tag=tag)

    _write_http_response(writer, response, http_version)
    await writer.drain()


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class RequestHandler(ABC):
    """Abstract base for a single proxy interaction mode.

    Implementations are stateless — instantiated once at server startup and
    reused across all connections.  Any per-request state must be local to
    ``handle``.

    Registration and dispatch order are controlled by ``_build_handlers``.
    To add a new mode, subclass this, implement both methods, and register
    the class in ``_MODE_REGISTRY`` / ``_HANDLER_ORDER``.
    """

    @abstractmethod
    def can_handle(
        self,
        method: str,
        target: str,
        http_version: str,
        headers: dict[str, str],
    ) -> bool:
        """Return True if this handler should own this request.

        Called in registration order; the first True wins.
        Must be synchronous and free of I/O.
        """

    @abstractmethod
    async def handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        method: str,
        target: str,
        http_version: str,
        headers: dict[str, str],
    ) -> None:
        """Process the request end-to-end and write the response.

        The request line and headers have already been consumed from *reader*.
        Do not close *writer* — ``ProxyServer`` owns the connection lifetime.
        """


# ---------------------------------------------------------------------------
# Concrete handlers
# ---------------------------------------------------------------------------

class ForwardingHandler(RequestHandler):
    """Header-based forwarding mode — the only built-in mode.

    Clients set ``X-Proxy-Hopper-Target`` to the real destination and send
    requests to proxy-hopper as if it were the target server::

        GET /v1/data?foo=bar HTTP/1.1
        Host: proxy-hopper:8080
        X-Proxy-Hopper-Target: https://api.example.com

    Proxy-hopper strips all ``X-Proxy-Hopper-*`` control headers, rewrites
    ``Host``, reconstructs ``https://api.example.com/v1/data?foo=bar``, and
    routes through ``_submit_and_respond`` — giving full retry / IP-rotation
    behaviour.

    Optional per-request control headers:

    ``X-Proxy-Hopper-Auth: Bearer <token>``
        Required when ``auth.enabled: true``.  Accepts an API key, a
        locally-issued JWT, or an OIDC access token.  This header is always
        stripped before the request is forwarded upstream.

    ``X-Proxy-Hopper-Tag: <string>``
        Free-form label added to Prometheus metrics (``tag`` label on
        ``proxy_hopper_requests_total`` / ``proxy_hopper_responses_total``).

    ``X-Proxy-Hopper-Retries: <int>``
        Override the target's ``numRetries`` for this request only.

    Integration example (requests)::

        session = requests.Session()
        session.headers["X-Proxy-Hopper-Target"] = "https://api.example.com"
        session.headers["X-Proxy-Hopper-Auth"] = "Bearer ph_mykey"
        session.headers["X-Proxy-Hopper-Tag"] = "search"
        resp = session.get("http://proxy-hopper:8080/v1/endpoint")
    """

    def __init__(
        self,
        managers: list[TargetManager],
        auth_config: Optional["AuthConfig"] = None,
        runtime_secret: str = "",
    ) -> None:
        self._managers = managers
        self._auth_config = auth_config
        self._runtime_secret = runtime_secret

    def can_handle(self, method, target, http_version, headers) -> bool:
        return _TARGET_HEADER in headers

    async def handle(self, reader, writer, method, target, http_version, headers) -> None:
        # --- Auth check (runs before any other work) ---
        if self._auth_config is not None and self._auth_config.enabled:
            raw_auth = headers.get(_AUTH_HEADER, "")
            token = raw_auth.removeprefix("Bearer ").strip()
            if not token:
                _write_error(
                    writer, 401,
                    "Authentication required — set X-Proxy-Hopper-Auth: Bearer <token>",
                )
                await writer.drain()
                return

            from .auth import Permission, authenticate_token, can_access_target, get_permissions
            try:
                user = await authenticate_token(token, self._auth_config, self._runtime_secret)
            except ValueError as exc:
                _write_error(writer, 401, str(exc))
                await writer.drain()
                return

            if user.is_api_key:
                # API keys grant proxy access unconditionally — just check target
                pass
            else:
                perms = get_permissions(user.role, self._auth_config)
                if Permission.read not in perms:
                    _write_error(writer, 403, f"Role '{user.role}' does not have proxy access")
                    await writer.drain()
                    return

            # Check target access — find the matching manager to get the target name
            proxy_target_prefix = headers.get(_TARGET_HEADER, "").rstrip("/")
            candidate_url = proxy_target_prefix + target
            matched = _find_first_manager(self._managers, candidate_url)
            if matched is not None and not can_access_target(user, matched._config.name, self._auth_config):
                if user.is_api_key:
                    _write_error(writer, 403, f"API key '{user.sub}' is not permitted to access target '{matched._config.name}'")
                else:
                    _write_error(writer, 403, f"Role '{user.role}' cannot access target '{matched._config.name}'")
                await writer.drain()
                return

        # --- Parse optional per-request control headers ---
        proxy_target = headers[_TARGET_HEADER].rstrip("/")
        real_url = proxy_target + target   # "https://api.example.com" + "/v1/data?foo=bar"

        tag = headers.get(_TAG_HEADER, "")

        num_retries_override: int | None = None
        raw_retries = headers.get(_RETRIES_HEADER)
        if raw_retries is not None:
            try:
                n = int(raw_retries)
                if n >= 0:
                    num_retries_override = n
            except ValueError:
                pass  # Invalid value — fall back to target default

        # Rewrite Host to the target host; strip all X-Proxy-Hopper-* headers
        # so the upstream server never sees any proxy control headers.
        parsed = urlparse(real_url)
        rewritten_headers = {
            k: v for k, v in headers.items()
            if not k.startswith("x-proxy-hopper-")
        }
        rewritten_headers["host"] = parsed.netloc

        logger.trace(  # type: ignore[attr-defined]
            "ForwardingHandler: %s %s → %s%s",
            method, target, real_url,
            f" [tag={tag!r}]" if tag else "",
        )
        await _submit_and_respond(
            reader, writer, method, real_url, http_version, rewritten_headers,
            self._managers,
            tag=tag,
            num_retries_override=num_retries_override,
        )


# ---------------------------------------------------------------------------
# Handler registry and factory
# ---------------------------------------------------------------------------

_MODE_REGISTRY: dict[str, type[RequestHandler]] = {
    "forwarding": ForwardingHandler,
}

# Dispatch priority order — evaluated top-to-bottom, first match wins.
_HANDLER_ORDER = ["forwarding"]

VALID_MODES: frozenset[str] = frozenset(_MODE_REGISTRY)


def _build_handlers(
    managers: list[TargetManager],
    enabled_modes: set[str] | None = None,
    auth_config: Optional["AuthConfig"] = None,
    runtime_secret: str = "",
) -> list[RequestHandler]:
    """Instantiate enabled handlers in dispatch-priority order.

    *enabled_modes* defaults to all registered modes.  Pass a subset to
    restrict which handlers are active (useful for custom deployments or
    testing).
    """
    active = enabled_modes if enabled_modes is not None else set(VALID_MODES)
    handlers = []
    for name in _HANDLER_ORDER:
        if name not in active:
            continue
        cls = _MODE_REGISTRY[name]
        if name == "forwarding":
            handlers.append(cls(managers, auth_config=auth_config, runtime_secret=runtime_secret))
        else:
            handlers.append(cls(managers))
    return handlers
