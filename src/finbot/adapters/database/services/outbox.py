from collections.abc import Sequence
from typing import cast
from uuid import UUID

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import TelegramResponseOutbox

MAX_TELEGRAM_TEXT_LENGTH = 4096
_ALLOWED_PARSE_MODES = {None, "HTML"}


def _validate_response(text: str, parse_mode: str | None) -> None:
    if not 1 <= len(text) <= MAX_TELEGRAM_TEXT_LENGTH:
        raise ValueError("Telegram response text must contain between 1 and 4096 characters")
    if parse_mode not in _ALLOWED_PARSE_MODES:
        raise ValueError("Unsupported Telegram response parse mode")


def serialize_reply_markup(
    reply_markup: InlineKeyboardMarkup | None,
) -> dict[str, object] | None:
    """Serialize only aiogram's validated inline-keyboard schema."""
    if reply_markup is None:
        return None
    payload = reply_markup.model_dump(mode="json", exclude_none=True)
    # ``model_dump(mode='json')`` recursively produces JSON-compatible values.
    return cast(dict[str, object], payload)


def queue_send_message(
    session: AsyncSession,
    *,
    update_id: int,
    owner_telegram_user_id: int,
    chat_id: int,
    text: str,
    parse_mode: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
    draft_id: UUID | None = None,
    sequence: int = 0,
) -> TelegramResponseOutbox:
    _validate_response(text, parse_mode)
    if sequence < 0:
        raise ValueError("Outbox response sequence must not be negative")
    response = TelegramResponseOutbox(
        update_id=update_id,
        sequence=sequence,
        owner_telegram_user_id=owner_telegram_user_id,
        chat_id=chat_id,
        method="send_message",
        message_id=None,
        body=text,
        parse_mode=parse_mode,
        reply_markup=serialize_reply_markup(reply_markup),
        draft_id=draft_id,
    )
    session.add(response)
    return response


def queue_edit_message_text(
    session: AsyncSession,
    *,
    update_id: int,
    owner_telegram_user_id: int,
    chat_id: int,
    message_id: int,
    text: str,
    parse_mode: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
    draft_id: UUID | None = None,
    sequence: int = 0,
) -> TelegramResponseOutbox:
    _validate_response(text, parse_mode)
    if message_id <= 0:
        raise ValueError("Telegram message id must be positive")
    if sequence < 0:
        raise ValueError("Outbox response sequence must not be negative")
    response = TelegramResponseOutbox(
        update_id=update_id,
        sequence=sequence,
        owner_telegram_user_id=owner_telegram_user_id,
        chat_id=chat_id,
        method="edit_message_text",
        message_id=message_id,
        body=text,
        parse_mode=parse_mode,
        reply_markup=serialize_reply_markup(reply_markup),
        draft_id=draft_id,
    )
    session.add(response)
    return response


async def lock_pending_responses(
    session: AsyncSession,
    *,
    update_id: int,
    owner_telegram_user_id: int,
    chat_id: int,
) -> Sequence[TelegramResponseOutbox]:
    """Lock pending rows so parallel replays cannot both start delivery."""
    result = await session.scalars(
        select(TelegramResponseOutbox)
        .where(
            TelegramResponseOutbox.update_id == update_id,
            TelegramResponseOutbox.owner_telegram_user_id == owner_telegram_user_id,
            TelegramResponseOutbox.chat_id == chat_id,
            TelegramResponseOutbox.sent_at.is_(None),
        )
        .order_by(TelegramResponseOutbox.sequence)
        .with_for_update()
    )
    return result.all()
