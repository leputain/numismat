from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from aiogram.types import Message

_MAX_TELEGRAM_USER_ID = 2**52 - 1
TELEGRAM_PRINCIPAL_DATA_KEY = "finbot_principal"


@dataclass(frozen=True, slots=True)
class TelegramPrincipal:
    """Authenticated Telegram actor bound to their exact private chat."""

    telegram_user_id: int = field(repr=False)
    chat_id: int = field(repr=False)

    def __post_init__(self) -> None:
        for value in (self.telegram_user_id, self.chat_id):
            if type(value) is not int:
                raise TypeError("Telegram principal identifiers must be integers")
            if not 1 <= value <= _MAX_TELEGRAM_USER_ID:
                raise ValueError("Telegram principal identifiers are out of range")
        if self.telegram_user_id != self.chat_id:
            raise ValueError("Telegram principal must use the actor's private chat")


_current_telegram_principal: ContextVar[TelegramPrincipal | None] = ContextVar(
    "finbot_telegram_principal",
    default=None,
)


@contextmanager
def telegram_principal_scope(principal: TelegramPrincipal) -> Iterator[None]:
    """Bind one verified actor for the current asynchronous update task."""

    if not isinstance(principal, TelegramPrincipal):
        raise TypeError("Telegram principal is required")
    token = _current_telegram_principal.set(principal)
    try:
        yield
    finally:
        _current_telegram_principal.reset(token)


def telegram_principal_for_message(message: Message) -> TelegramPrincipal:
    """Resolve the scoped actor and fail closed on crossed message context.

    The strict message fallback exists for focused direct adapter tests and other
    non-dispatch calls. Callback messages identify the bot as ``from_user`` and
    therefore require the middleware scope used in production.
    """

    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    chat_type = getattr(chat, "type", None)
    if type(chat_id) is not int or chat_type != "private":
        raise RuntimeError("Verified private Telegram context is required")

    principal = _current_telegram_principal.get()
    if principal is not None:
        if principal.chat_id != chat_id:
            raise RuntimeError("Telegram principal does not match the message chat")
        return principal

    actor = getattr(message, "from_user", None)
    actor_id = getattr(actor, "id", None)
    if type(actor_id) is not int or actor_id != chat_id:
        raise RuntimeError("Verified private Telegram context is required")
    return TelegramPrincipal(telegram_user_id=actor_id, chat_id=chat_id)


__all__ = [
    "TELEGRAM_PRINCIPAL_DATA_KEY",
    "TelegramPrincipal",
    "telegram_principal_for_message",
    "telegram_principal_scope",
]
