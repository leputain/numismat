from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, MutableMapping
from time import monotonic
from typing import Any

from finbot.observability.logging import correlation_id, new_correlation_id

AsgiMessage = MutableMapping[str, Any]
AsgiReceive = Callable[[], Awaitable[AsgiMessage]]
AsgiSend = Callable[[AsgiMessage], Awaitable[None]]
AsgiApp = Callable[[MutableMapping[str, Any], AsgiReceive, AsgiSend], Awaitable[None]]

logger = logging.getLogger("finbot.http.requests")


class PrivacySafeRequestLoggingMiddleware:
    """Emit one allowlisted completion event without inspecting request data."""

    def __init__(self, app: AsgiApp) -> None:
        self._app = app

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: AsgiReceive,
        send: AsgiSend,
    ) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        started = monotonic()
        status_code = 500
        unexpected_error = False
        token = correlation_id.set(new_correlation_id())

        async def capture_status(message: AsgiMessage) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                candidate = message.get("status")
                if type(candidate) is int and 100 <= candidate <= 599:
                    status_code = candidate
            await send(message)

        try:
            await self._app(scope, receive, capture_status)
        except Exception:
            unexpected_error = True
            raise
        finally:
            route = scope.get("route")
            route_template = getattr(route, "path", None)
            if type(route_template) is not str:
                route_template = "/unmatched"
            state = scope.get("state")
            error_code = state.get("http_error_code") if isinstance(state, dict) else None
            if unexpected_error:
                error_code = "internal_error"
            result = (
                "success" if status_code < 400 else "rejected" if status_code < 500 else "error"
            )
            try:
                logger.info(
                    "http_request_completed",
                    extra={
                        "duration_ms": (monotonic() - started) * 1000,
                        "error_code": error_code,
                        "result": result,
                        "route_template": route_template,
                        "status_code": status_code,
                    },
                )
            finally:
                correlation_id.reset(token)
