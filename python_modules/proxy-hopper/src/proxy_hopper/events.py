"""Request event model and EventBus for live log streaming.

Design
------
* Fire-and-forget: ``publish()`` is always called inside
  ``asyncio.create_task()`` by TargetManager so backend I/O never adds
  latency to request handling.
* HA-compatible: with Redis every instance writes to the shared pub/sub
  channel and rolling log, so any admin instance can serve the SSE stream.
  With MemoryBackend each process is self-contained (acceptable — Memory is
  single-instance by nature).
* Non-invasive: TargetManager accepts ``event_bus=None`` (default) so tests
  never need to supply one.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Optional
from uuid import uuid4

if TYPE_CHECKING:
    from .backend.base import Backend

_CHANNEL = "ph:events:stream"
_LOG_KEY = "ph:events:log"
_LOG_MAX = 500


@dataclass
class RequestEvent:
    id: str
    timestamp: float          # unix epoch
    target: str               # target name
    method: str               # HTTP method
    url: str                  # full URL proxied
    proxy_ip: str             # host:port of the proxy used
    provider: Optional[str]   # provider name, if known
    status_code: Optional[int]
    success: bool             # True = non-retriable response
    attempt: int              # 0 = first try, 1 = first retry, …
    elapsed_ms: float
    error: Optional[str]      # exception string on connection failure
    request_headers: dict[str, str] = field(default_factory=dict)
    response_headers: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def create(
        target: str,
        method: str,
        url: str,
        proxy_ip: str,
        provider: Optional[str],
        status_code: Optional[int],
        success: bool,
        attempt: int,
        elapsed_ms: float,
        error: Optional[str],
        request_headers: dict[str, str],
        response_headers: dict[str, str],
    ) -> "RequestEvent":
        return RequestEvent(
            id=str(uuid4()),
            timestamp=time.time(),
            target=target,
            method=method,
            url=url,
            proxy_ip=proxy_ip,
            provider=provider,
            status_code=status_code,
            success=success,
            attempt=attempt,
            elapsed_ms=elapsed_ms,
            error=error,
            request_headers=request_headers,
            response_headers=response_headers,
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "RequestEvent":
        return cls(**json.loads(raw))


class EventBus:
    """Publish and subscribe to RequestEvents via the shared Backend."""

    def __init__(self, backend: "Backend") -> None:
        self._backend = backend

    async def publish(self, event: RequestEvent) -> None:
        payload = event.to_json()
        await self._backend.log_append(_LOG_KEY, payload, _LOG_MAX)
        await self._backend.publish(_CHANNEL, payload)

    async def recent(self, limit: int = 100) -> list[RequestEvent]:
        """Return up to *limit* recent events, oldest first (ready to stream)."""
        rows = await self._backend.log_read(_LOG_KEY, limit)
        events: list[RequestEvent] = []
        for raw in reversed(rows):  # log_read is newest-first; reverse for chrono
            try:
                events.append(RequestEvent.from_json(raw))
            except Exception:
                pass
        return events

    async def subscribe(self):
        """Async generator — yields RequestEvents as they are published live."""
        async with self._backend.subscribe(_CHANNEL) as messages:
            async for raw in messages:
                try:
                    yield RequestEvent.from_json(raw)
                except Exception:
                    pass
