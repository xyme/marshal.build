"""Request body-size backstop (pre-Beta hardening item 3).

Field-level pydantic caps govern the KNOWN large inputs (specs 400k chars,
chat 20k, marketplace copy); this middleware is the coarse belt for everything
else — most importantly the unbounded dict fields (template guardrails,
sample spec_snapshot/assets, guided answers) and any future endpoint someone
forgets to cap. The WAF's SizeRestrictions_BODY managed rule stays in COUNT
mode deliberately: its 8KB threshold would break legitimate spec saves, so
enforcement lives here where the limit can be right.

Raw ASGI (the AuditMiddleware precedent — BaseHTTPMiddleware would buffer
SSE): declared Content-Length over the cap is refused before the app runs;
chunked/streamed bodies are counted as they arrive and cut off at the cap.
"""

import json
import logging

logger = logging.getLogger("marshal.limits")

MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MiB — 3 full spec docs + JSON overhead fit

_413_BODY = json.dumps(
    {"detail": {
        "code": "payload_too_large",
        "message": f"Request body exceeds the {MAX_BODY_BYTES // (1024 * 1024)} MiB limit.",
    }}
).encode()


class BodySizeLimitMiddleware:
    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)

        declared = None
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = None
                break
        if declared is not None and declared > self.max_bytes:
            return await self._reject(send)

        # Chunked / streamed bodies: count as chunks arrive. FastAPI buffers
        # JSON bodies before routing, so the 413 beats any handler work.
        received = 0
        response_started = False
        rejected = False

        async def recv_wrapper():
            nonlocal received, rejected
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    rejected = True
                    # Present a completed (empty-tail) body to the app while
                    # the outer handler races to reject; dropping the excess
                    # keeps memory bounded either way.
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        async def send_wrapper(message):
            nonlocal response_started
            if rejected:
                # The body was cut mid-stream: the app's own response (however
                # it interpreted the truncated body) is refused wholesale and
                # replaced with ONE 413. Later app messages are swallowed.
                if not response_started:
                    response_started = True
                    await self._reject(send)
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        await self.app(scope, recv_wrapper, send_wrapper)

    async def _reject(self, send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": _413_BODY})
