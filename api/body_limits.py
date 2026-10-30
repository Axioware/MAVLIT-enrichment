"""
api/body_limits.py

A request-body size limit, answered with HTTP 413 and a JSON body before any route parses the body. The Content-Length header is checked
first; when it is missing (chunked uploads) or wrong, the bytes are counted as they arrive and the request is stopped at the limit.

Limits (kilobytes; each can be changed with an environment variable, 0 = no limit for that class):
    BODY_LIMIT_KB            16   everything not listed below
    BODY_LIMIT_KB_STANDARD   32   PUT /creator-profile/me and the /pitches routes
    BODY_LIMIT_KB_LARGE      64   POST /contract-advice and PATCH /pitches/{id}
    BODY_LIMIT_KB_BULK      256   POST /content-creator-re/bulk-add
/admin (SQLAdmin forms, behind the admin passkey) and /static are not limited here.
"""

import json
import os

from starlette.types import ASGIApp, Message, Receive, Scope, Send

EXEMPT_PREFIXES = ("/admin", "/static")


def _kb(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number of kilobytes, got {raw!r}")
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value}")
    return value


def limit_bytes(method: str, path: str) -> int:
    """The limit for this request in bytes (0 = unlimited). Read from the environment at call time."""
    if path.startswith(EXEMPT_PREFIXES):
        return 0
    if path == "/content-creator-re/bulk-add":
        kb = _kb("BODY_LIMIT_KB_BULK", 256)
    elif path == "/contract-advice" or (method == "PATCH" and path.startswith("/pitches/")):
        kb = _kb("BODY_LIMIT_KB_LARGE", 64)
    elif (method == "PUT" and path == "/creator-profile/me") or path == "/pitches" or path.startswith("/pitches/"):
        kb = _kb("BODY_LIMIT_KB_STANDARD", 32)
    else:
        kb = _kb("BODY_LIMIT_KB", 16)
    return kb * 1024


for _name, _default in (("BODY_LIMIT_KB", 16), ("BODY_LIMIT_KB_STANDARD", 32), ("BODY_LIMIT_KB_LARGE", 64), ("BODY_LIMIT_KB_BULK", 256)):
    _kb(_name, _default)   # a bad value stops the application at start


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = limit_bytes(scope["method"], scope["path"])
        if not limit:
            await self.app(scope, receive, send)
            return

        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await self._too_large(send, limit)
            return

        state = {"seen": 0, "refused": False, "started": False}

        async def counting_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                state["seen"] += len(message.get("body", b""))
                if state["seen"] > limit and not state["refused"]:
                    state["refused"] = True
                    if not state["started"]:
                        await self._too_large(send, limit)
                    return {"type": "http.disconnect"}   # the application stops reading; whatever it then sends is dropped
            return message

        async def guarded_send(message: Message) -> None:
            if state["refused"]:
                return
            if message["type"] == "http.response.start":
                state["started"] = True
            await send(message)

        await self.app(scope, counting_receive, guarded_send)

    @staticmethod
    async def _too_large(send: Send, limit: int) -> None:
        body = json.dumps({"detail": f"The request is too large (the limit for this request is {limit // 1024} KB)."}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})
