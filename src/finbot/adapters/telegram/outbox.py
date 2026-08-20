from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finbot.adapters.database.models import TelegramResponseOutbox
from finbot.adapters.database.services.outbox import (
    bind_telegram_draft_presentation,
    lock_pending_responses,
    response_draft_belongs_to_private_chat,
)
from finbot.adapters.telegram.principal import (
    TELEGRAM_PRINCIPAL_DATA_KEY,
    TelegramPrincipal,
)

type SpecialOutboxDelivery = Callable[[Bot, TelegramResponseOutbox], Awaitable[int | None]]


def _reply_markup(
    response: TelegramResponseOutbox,
) -> InlineKeyboardMarkup | ReplyKeyboardMarkup | None:
    if response.reply_markup is None:
        return None
    has_inline_keyboard = "inline_keyboard" in response.reply_markup
    has_reply_keyboard = "keyboard" in response.reply_markup
    if has_inline_keyboard == has_reply_keyboard:
        raise RuntimeError("Unsupported Telegram outbox reply markup")
    if has_inline_keyboard:
        return InlineKeyboardMarkup.model_validate(response.reply_markup)
    if response.method != "send_message":
        raise RuntimeError("Reply keyboards are supported only for sent messages")
    return ReplyKeyboardMarkup.model_validate(response.reply_markup)


async def deliver_response(
    bot: Bot,
    response: TelegramResponseOutbox,
    *,
    special_delivery: SpecialOutboxDelivery | None = None,
) -> int | None:
    """Deliver one validated outbox method.

    Delivery is intentionally at-least-once: Telegram has no idempotency key, so a
    process crash after Telegram accepts the request but before ``sent_at`` commits
    can result in a duplicate send. Row locking prevents parallel workers from
    introducing duplicates during normal operation.
    """
    markup = _reply_markup(response)
    if response.method == "send_csv_export":
        if special_delivery is None:
            raise RuntimeError("CSV export outbox delivery is not configured")
        return await special_delivery(bot, response)
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
    if isinstance(markup, ReplyKeyboardMarkup):  # pragma: no cover - rejected while decoding
        raise RuntimeError("Reply keyboards cannot be attached to edited messages")
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
    special_delivery: SpecialOutboxDelivery | None = None,
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
            if not await response_draft_belongs_to_private_chat(session, response):
                raise RuntimeError("Telegram outbox draft ownership mismatch")
            delivered_message_id = await deliver_response(
                bot,
                response,
                special_delivery=special_delivery,
            )
            if (
                response.draft_id is not None
                and response.draft_revision is not None
                and delivered_message_id is not None
            ):
                await bind_telegram_draft_presentation(
                    session,
                    draft_id=response.draft_id,
                    draft_revision=response.draft_revision,
                    chat_id=response.chat_id,
                    message_id=delivered_message_id,
                    history_page=response.history_page,
                    pending_history_page=response.pending_history_page,
                )
            response.sent_at = datetime.now(UTC)
            delivered += 1
    return delivered


class TelegramResponseOutboxMiddleware(BaseMiddleware):
    """Deliver committed receipts before replaying business handlers and after success."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        special_delivery: SpecialOutboxDelivery | None = None,
    ) -> None:
        self.sessions = sessions
        self.special_delivery = special_delivery

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        update_id = data.get("finbot_update_id")
        principal = data.get(TELEGRAM_PRINCIPAL_DATA_KEY)
        bot = data.get("bot")
        if type(update_id) is not int:
            return await handler(event, data)
        if not isinstance(principal, TelegramPrincipal) or not isinstance(bot, Bot):
            raise RuntimeError("Tracked Telegram delivery requires a verified principal")

        identity = {
            "update_id": update_id,
            "owner_telegram_user_id": principal.telegram_user_id,
            "chat_id": principal.chat_id,
        }
        if await deliver_pending_responses(
            self.sessions,
            bot,
            special_delivery=self.special_delivery,
            **identity,
        ):
            # A prior invocation committed the business transaction. Replaying the
            # handler would be redundant and, for non-idempotent future handlers,
            # unsafe.
            return None

        result = await handler(event, data)
        await deliver_pending_responses(
            self.sessions,
            bot,
            special_delivery=self.special_delivery,
            **identity,
        )
        return result
