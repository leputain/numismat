import logging
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from finbot.config import Settings
from finbot.observability.logging import correlation_id, new_correlation_id


class OwnerOnlyMiddleware(BaseMiddleware):
    def __init__(
        self,
        settings: Settings,
        sessions: object | None = None,
    ) -> None:
        self.settings = settings
        self.sessions = sessions  # kept for backwards-compatible construction
        self.logger = logging.getLogger("finbot.auth")

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        chat = data.get("event_chat")
        if user is None or chat is None:
            self.logger.info("update_rejected_missing_context")
            return None
        if user.id != self.settings.owner_telegram_user_id or chat.type != "private":
            self.logger.info("update_rejected_not_owner_private")
            return None
        update = data.get("event_update")
        update_id = getattr(update, "update_id", None)
        data["finbot_update_id"] = update_id if isinstance(update_id, int) else None
        token = correlation_id.set(new_correlation_id())
        started = perf_counter()
        handler_name = getattr(handler, "__name__", type(handler).__name__)
        log_context = {"handler": handler_name, "event_type": type(event).__name__}
        try:
            self.logger.info("update_authorized")
            result = await handler(event, data)
            self.logger.info(
                "update_handled",
                extra={
                    **log_context,
                    "duration_ms": round((perf_counter() - started) * 1000),
                    "result": "success",
                },
            )
            return result
        except Exception:
            self.logger.exception(
                "update_failed",
                extra={
                    **log_context,
                    "duration_ms": round((perf_counter() - started) * 1000),
                    "result": "error",
                },
            )
            raise
        finally:
            correlation_id.reset(token)
