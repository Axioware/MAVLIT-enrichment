"""
api/concurrency.py

A shared limit on the slow routes, so a burst of slow requests (LLM, Apify, scraping) cannot use up the server's worker threads and starve
login and /health. The slow routes share ONE counter, SLOW_ROUTE_CONCURRENCY (default 12). When it is full a new slow request is answered at
once with HTTP 503 and `Retry-After: 5`; it is never queued. Every other route (login, /auth/*, /health, /health/db, admin, reads) is not counted.

A slot is released as soon as the response has been sent completely, or when the request ends in any other way (an error, the browser
leaving, a cancelled stream), whichever comes first, and never twice. Work that continues after the response (background tasks) is NOT
counted here. At startup the AnyIO worker-thread limit is raised to SLOW_ROUTE_CONCURRENCY + 52 (api/app.py), so the slow routes can never hold
every thread.

SLOW_ROUTE_CONCURRENCY must be a whole number >= 1; anything else stops the application at start.
"""

import json
import os
import re

from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_SLOW_ROUTE_CONCURRENCY = 12
THREAD_RESERVE = 52   # worker threads kept for everything that is not a slow route (limit = SLOW_ROUTE_CONCURRENCY + this)

_SLOW_POST = {"/pitches", "/pitches/stream", "/rate-intelligence", "/contract-advice", "/creator-profile/me/posts",
              "/creator-profile/me/generate-description", "/matches/me/v2/refresh", "/matches/me/v3/refresh", "/own-brands"}
_THUMBNAIL = re.compile(r"^/creator-profile/me/posts/[^/]+/thumbnail$")


def slow_route_concurrency() -> int:
    raw = (os.environ.get("SLOW_ROUTE_CONCURRENCY") or "").strip()
    if not raw:
        return DEFAULT_SLOW_ROUTE_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"SLOW_ROUTE_CONCURRENCY must be a whole number, got {raw!r}")
    if value < 1:
        raise ValueError(f"SLOW_ROUTE_CONCURRENCY must be >= 1, got {value}")
    return value


slow_route_concurrency()   # a bad setting stops the application at start


def is_slow_route(method: str, path: str) -> bool:
    if method == "POST":
        return path in _SLOW_POST
    return method == "GET" and bool(_THUMBNAIL.match(path))


class SlowRouteLimitMiddleware:
    def __init__(self, app: ASGIApp, limit: int | None = None) -> None:
        self.app = app
        self.limit = limit if limit is not None else slow_route_concurrency()
        self.in_flight = 0   # touched only on the event loop, with no await between the check and the change: no lock needed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_slow_route(scope["method"], scope["path"]):
            await self.app(scope, receive, send)
            return
        if self.in_flight >= self.limit:
            body = json.dumps({"detail": "Server is busy, please try again in a few seconds"}).encode()
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"retry-after", b"5")]})
            await send({"type": "http.response.body", "body": body})
            return
        self.in_flight += 1
        held = True

        def release() -> None:
            nonlocal held
            if held:
                held = False
                self.in_flight -= 1

        async def counting_send(message: Message) -> None:
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                release()   # the response is complete; background work after it is not counted

        try:
            await self.app(scope, receive, counting_send)
        finally:
            release()   # an error, a disconnect or a cancelled stream
