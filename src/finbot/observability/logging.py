from __future__ import annotations

import contextvars
import json
import logging
import math
import re
import sys
import uuid
from types import TracebackType

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "finbot_correlation_id", default="system"
)

# Log events are an API, not free-form prose.  Keeping the registry here makes it
# impossible for an exception message, Telegram payload, or SQL statement to become
# a structured log field merely because it was passed to logging.Logger.
SAFE_EVENT_CODES = frozenset(
    {
        "log_event_rejected",
        "polling_fetch_failed",
        "polling_fetch_recovered",
        "polling_started",
        "polling_stopped",
        "polling_update_failed",
        "update_authorized",
        "update_failed",
        "update_handled",
        "update_rejected_missing_context",
        "update_rejected_not_owner_private",
    }
)

_SAFE_RESULTS = frozenset({"error", "ignored", "rejected", "retry", "stopped", "success"})
_SAFE_EVENT_TYPES = {
    "CallbackQuery": "callback_query",
    "ChatJoinRequest": "chat_join_request",
    "ChatMemberUpdated": "chat_member",
    "ChosenInlineResult": "chosen_inline_result",
    "InlineQuery": "inline_query",
    "Message": "message",
    "MessageReactionCountUpdated": "message_reaction_count",
    "MessageReactionUpdated": "message_reaction",
    "Poll": "poll",
    "PollAnswer": "poll_answer",
    "PreCheckoutQuery": "pre_checkout_query",
    "ShippingQuery": "shipping_query",
}
_SAFE_ERROR_CLASS_NAMES = frozenset(
    {
        "CancelledError",
        "ClientConnectionError",
        "ClientDecodeError",
        "ConnectionError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "DatabaseError",
        "DBAPIError",
        "Error",
        "Exception",
        "IntegrityError",
        "InterfaceError",
        "LookupError",
        "OSError",
        "OperationalError",
        "RuntimeError",
        "SQLAlchemyError",
        "TelegramAPIError",
        "TelegramBadRequest",
        "TelegramConflictError",
        "TelegramForbiddenError",
        "TelegramNetworkError",
        "TelegramRetryAfter",
        "TelegramServerError",
        "TelegramUnauthorizedError",
        "TimeoutError",
        "TypeError",
        "ValueError",
    }
)
_COMPONENT_PREFIXES = (
    ("finbot.auth", "auth"),
    ("finbot.lifecycle", "lifecycle"),
    ("finbot.polling", "polling"),
    ("finbot.application", "application"),
    ("finbot.database", "database"),
    ("finbot.telegram", "telegram"),
    ("sqlalchemy", "database"),
    ("psycopg", "database"),
    ("aiogram", "telegram"),
    ("finbot", "application"),
)
_CORRELATION_ID = re.compile(r"[0-9a-f]{12}\Z")
_BOT_TOKEN = re.compile(r"(?i)(?:https?://api\.telegram\.org/)?bot\d+:[A-Za-z0-9_-]+")
_DATABASE_PASSWORD = re.compile(r"(postgresql(?:\+\w+)?://[^:\s/]+:)[^@\s]+(@)")
_LONG_NUMBER = re.compile(r"(?<![\w.])\d{6,}(?![\w.])")


def redact(value: str) -> str:
    """Redact a string for explicit display outside the structured logger.

    JsonFormatter deliberately does not use this function: sanitising free-form text is
    weaker than refusing to serialise it at all.
    """

    value = _BOT_TOKEN.sub("[telegram-secret]", value)
    value = _DATABASE_PASSWORD.sub(r"\1[secret]\2", value)
    return _LONG_NUMBER.sub("[numeric-id]", value)


def safe_error_class(error: BaseException | type[BaseException]) -> str:
    """Return a bounded error category without inspecting the exception value."""

    error_type = error if isinstance(error, type) else type(error)
    try:
        for candidate in error_type.__mro__:
            name = candidate.__name__
            if type(name) is str and name in _SAFE_ERROR_CLASS_NAMES:
                return name
    except Exception:
        pass
    return "Exception"


def _event_code(record: logging.LogRecord) -> str:
    # Use exact str to avoid invoking user-defined __hash__, __eq__, or __str__ methods.
    message = record.msg
    if type(message) is str and message in SAFE_EVENT_CODES:
        return message
    return "log_event_rejected"


def _level(level_number: int) -> str:
    if type(level_number) is not int:
        return "INFO"
    if level_number >= logging.CRITICAL:
        return "CRITICAL"
    if level_number >= logging.ERROR:
        return "ERROR"
    if level_number >= logging.WARNING:
        return "WARNING"
    if level_number >= logging.INFO:
        return "INFO"
    return "DEBUG"


def _component(logger_name: str) -> str:
    if type(logger_name) is not str:
        return "external"
    for prefix, component in _COMPONENT_PREFIXES:
        if logger_name == prefix or logger_name.startswith(f"{prefix}."):
            return component
    return "external"


def _safe_correlation_id() -> str:
    value = correlation_id.get()
    if value == "system" or (type(value) is str and _CORRELATION_ID.fullmatch(value)):
        return value
    return "invalid"


def _duration_bucket(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        return None
    if numeric < 10:
        return "under_10_ms"
    if numeric < 50:
        return "under_50_ms"
    if numeric < 250:
        return "under_250_ms"
    if numeric < 1000:
        return "under_1_s"
    if numeric < 5000:
        return "under_5_s"
    return "over_5_s"


def _record_error_class(
    exc_info: (
        tuple[type[BaseException], BaseException, TracebackType | None]
        | tuple[None, None, None]
        | None
    ),
) -> str | None:
    if not exc_info or exc_info[0] is None:
        return None
    return safe_error_class(exc_info[0])


class JsonFormatter(logging.Formatter):
    """Format a deliberately small, data-independent JSON log envelope."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            payload: dict[str, str] = {
                "component": _component(record.name),
                "correlation_id": _safe_correlation_id(),
                "event": _event_code(record),
                "level": _level(record.levelno),
            }

            result = getattr(record, "result", None)
            if type(result) is str and result in _SAFE_RESULTS:
                payload["result"] = result

            event_type = getattr(record, "event_type", None)
            if type(event_type) is str and event_type in _SAFE_EVENT_TYPES:
                payload["event_type"] = _SAFE_EVENT_TYPES[event_type]

            duration_bucket = _duration_bucket(getattr(record, "duration_ms", None))
            if duration_bucket is not None:
                payload["duration"] = duration_bucket

            error_class = _record_error_class(record.exc_info)
            if error_class is not None:
                payload["error_class"] = error_class

            return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        except Exception:
            # Formatting must remain safe even for a deliberately malformed LogRecord.
            return (
                '{"component":"external","correlation_id":"invalid",'
                '"event":"log_event_rejected","level":"INFO"}'
            )


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:12]


def configure(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    safe_levels = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
    safe_level = level.upper() if type(level) is str and level.upper() in safe_levels else "INFO"
    root.setLevel(safe_level)
    # Aiogram's normal lifecycle logs contain bot/update IDs.  Warnings and errors still
    # reach the root handler, where their message and arguments are discarded.
    logging.getLogger("aiogram").setLevel(logging.WARNING)
