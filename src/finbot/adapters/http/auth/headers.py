from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SENSITIVE_PREFIX = "/api/v1/"
_SECURITY_HEADERS = (
    (b"cache-control", b"no-store, no-cache"),
    (b"pragma", b"no-cache"),
    (b"referrer-policy", b"no-referrer"),
)
_REPLACED_NAMES = frozenset(name for name, _value in _SECURITY_HEADERS)


class AuthSecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path")
        if (
            scope.get("type") != "http"
            or not isinstance(path, str)
            or not path.startswith(_SENSITIVE_PREFIX)
        ):
            await self._app(scope, receive, send)
            return

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() not in _REPLACED_NAMES
                ]
                headers.extend(_SECURITY_HEADERS)
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, send_with_security_headers)


AuthSecuritySend = Callable[[Message], Awaitable[Any]]
