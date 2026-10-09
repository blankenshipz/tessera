"""Bound HTTP intake before parsing or admitting a command, not disk or spend quotas."""

from __future__ import annotations

import asyncio

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# Enough for one base64-encoded 16 MiB artifact plus bounded JSON metadata.
MAX_REQUEST_BYTES = 24 * 1024 * 1024
REQUEST_BODY_TIMEOUT_SECONDS = 30
MAX_TOKEN_CHARS = 4096


class RequestLimitsMiddleware:
    """Buffer only bounded input; overflow/disconnect never reaches a mutation.

    Responses (including event streams) are not buffered. A reverse proxy must
    separately bound connection count, header bytes and request rate.
    """

    def __init__(
        self,
        app: ASGIApp,
        max_bytes: int = MAX_REQUEST_BYTES,
        timeout_seconds: float = REQUEST_BODY_TIMEOUT_SECONDS,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def reject(status: int, detail: str) -> None:
            headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
            await JSONResponse({"detail": detail}, status_code=status, headers=headers)(
                scope, receive, send
            )

        headers = scope.get("headers", [])
        authorization = [value for name, value in headers if name.lower() == b"authorization"]
        if len(authorization) > 1 or any(
            len(value) > MAX_TOKEN_CHARS + len(b"Bearer ") for value in authorization
        ):
            await reject(401, "Invalid authentication header.")
            return
        lengths = [value for name, value in headers if name.lower() == b"content-length"]
        if len(lengths) > 1 or any(
            not value or len(value) > 20 or not value.isdigit() for value in lengths
        ):
            await reject(400, "Invalid Content-Length.")
            return
        declared = int(lengths[0]) if lengths else None
        if declared is not None and declared > self.max_bytes:
            await reject(413, "Request body exceeds the byte limit.")
            return
        body = bytearray()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(chunk) > self.max_bytes - len(body):
                        await reject(413, "Request body exceeds the byte limit.")
                        return
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await reject(408, "Request body deadline exceeded.")
            return
        if declared is not None and declared != len(body):
            await reject(400, "Request body length does not match Content-Length.")
            return

        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)
