"""Server-Sent Events endpoint for the live request log.

Mounted at ``/events`` on the admin FastAPI app when an EventBus is provided.

Authentication
--------------
SSE uses the browser's ``EventSource`` API which cannot send custom headers.
We therefore accept the JWT as a ``?token=`` query parameter as an alternative
to the ``Authorization: Bearer`` header.  When auth is disabled the parameter
is ignored.

Endpoints
---------
GET /events/stream?token=<jwt>&limit=<n>
    Streams RequestEvent JSON payloads as SSE ``data:`` frames.
    Sends up to ``limit`` historical events first (oldest → newest), then
    streams live events in real time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from fastapi import APIRouter, Query
from fastapi.responses import Response, StreamingResponse

if TYPE_CHECKING:
    from proxy_hopper.config import AuthConfig
    from proxy_hopper.events import EventBus


def create_events_router(
    event_bus: "EventBus",
    auth_config: Optional["AuthConfig"],
    runtime_secret: str,
) -> APIRouter:
    router = APIRouter()

    @router.get("/stream", summary="Live request event stream (SSE)")
    async def stream_events(
        token: Optional[str] = Query(None),
        limit: int = Query(100, ge=1, le=500),
    ):
        if auth_config is not None and auth_config.enabled:
            if not token:
                return Response(
                    "Authentication required — pass ?token=<jwt>",
                    status_code=401,
                    media_type="text/plain",
                )
            try:
                from proxy_hopper.auth import authenticate_token
                await authenticate_token(token, auth_config, runtime_secret)
            except ValueError as exc:
                return Response(str(exc), status_code=401, media_type="text/plain")

        async def generator():
            try:
                # Historical events first (oldest → newest so the table reads top-to-bottom)
                events = await event_bus.recent(limit)
                for event in events:
                    yield f"data: {event.to_json()}\n\n"

                # Live stream — runs until client disconnects
                async for event in event_bus.subscribe():
                    yield f"data: {event.to_json()}\n\n"

            except (GeneratorExit, Exception):
                return

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",   # disable nginx buffering
                "Connection": "keep-alive",
            },
        )

    return router
