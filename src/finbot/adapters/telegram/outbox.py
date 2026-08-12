from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finbot.adapters.database.models import Draft, TelegramResponseOutbox
from finbot.adapters.database.services.outbox import lock_pending_responses


def _reply_markup(response: TelegramResponseOutbox) -> InlineKeyboardMarkup | None:
    if response.reply_markup is None:
        return None
    return InlineKeyboardMarkup.model_validate(response.reply_markup)


async def deliver_response(bot: Bot, response: TelegramResponseOutbox) -> int | None:
    """Deliver one validated outbox method.

    Delivery is intentionally at-least-once: Telegram has no idempotency key, so a
    process crash after Telegram accepts the request but before ``sent_at`` commits
    can result in a duplicate send. Row locking prevents parallel workers from
    introducing duplicates during normal operation.
    """
    markup = _reply_markup(response)
    if response.method == "send_message":
        sent = await bot.send_message(
            chat_id=response.chat_id,
            text=response.body,
            parse_mode=response.parse_mode,
            reply_markup=markup,
        )
        return sent.message_id
    if response.method != "edit_message_text" or response.message_id is None:
        raise RuntimeError("Unsupported or incomplete Telegram outbox response")
    try:
        edited = await bot.edit_message_text(
            chat_id=response.chat_id,
            message_id=response.message_id,
            text=response.body,
            parse_mode=response.parse_mode,
            reply_markup=markup,
        )
        return edited.message_id if isinstance(edited, Message) else response.message_id
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).casefold():
            return response.message_id
        # The original screen can disappear between commit and delivery. A fresh
        # receipt is safer than leaving a committed financial mutation silent.
        sent = await bot.send_message(
            chat_id=response.chat_id,
            text=response.body,
            parse_mode=response.parse_mode,
            reply_markup=markup,
        )
        return sent.message_id


async def deliver_pending_responses(
    sessions: async_sessionmaker[AsyncSession],
    bot: Bot,
    *,
    update_id: int,
    owner_telegram_user_id: int,
    chat_id: int,
) -> int:
    delivered = 0
    async with sessions() as session, session.begin():
        pending = await lock_pending_responses(
            session,
            update_id=update_id,
            owner_telegram_user_id=owner_telegram_user_id,
            chat_id=chat_id,
        )
        for response in pending:
            delivered_message_id = await deliver_response(bot, response)
            if response.draft_id is not None and delivered_message_id is not None:
                draft = await session.get(Draft, response.draft_id)
                if draft is not None:
                    payload = dict(draft.payload)
                    payload["ui_message_id"] = delivered_message_id
                    draft.payload = payload
                    draft.presentation_ref = str(delivered_message_id)
            response.sent_at = datetime.now(UTC)
            delivered += 1
    return delivered


class TelegramResponseOutboxMiddleware(BaseMiddleware):
    """Deliver committed receipts before replaying business handlers and after success."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        update_id = data.get("finbot_update_id")
        owner = data.get("event_from_user")
        chat = data.get("event_chat")
        bot = data.get("bot")
        if (
            not isinstance(update_id, int)
            or owner is None
            or chat is None
            or not isinstance(bot, Bot)
        ):
            return await handler(event, data)

        identity = {
            "update_id": update_id,
            "owner_telegram_user_id": int(owner.id),
            "chat_id": int(chat.id),
        }
        if await deliver_pending_responses(self.sessions, bot, **identity):
            # A prior invocation committed the business transaction. Replaying the
            # handler would be redundant and, for non-idempotent future handlers,
            # unsafe.
            return None

        result = await handler(event, data)
        await deliver_pending_responses(self.sessions, bot, **identity)
        return result
