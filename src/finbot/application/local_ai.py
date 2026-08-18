import re
import unicodedata
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from finbot.application.errors import ApplicationError, ApplicationValidationError
from finbot.domain.transactions import TransactionDraft, TransactionType

MAX_LOCAL_AI_INPUT_CHARS = 1024
MAX_LOCAL_AI_RESPONSE_BYTES = 16 * 1024
MAX_LOCAL_AI_HINT_CHARS = 100

_CANONICAL_DECIMAL = re.compile(r"[0-9]{1,16}(?:\.[0-9]{1,2})?\Z")


class LocalAiDisabledError(ApplicationError):
    """The optional provider is intentionally disabled by configuration."""


class LocalAiUnavailableError(ApplicationError):
    """The local provider could not return a bounded response in time."""


class LocalAiInvalidSuggestionError(ApplicationValidationError):
    """The provider returned data that cannot enter the draft lifecycle."""


def normalize_local_ai_input(value: str) -> str:
    """Return one bounded in-memory prompt without retaining control characters."""

    if type(value) is not str:
        raise TypeError("Local AI input must be text")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized:
        raise ApplicationValidationError("После /ai добавьте описание операции")
    if len(normalized) > MAX_LOCAL_AI_INPUT_CHARS:
        raise ApplicationValidationError("Описание для локального AI слишком длинное")
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise ApplicationValidationError("Описание для локального AI содержит недопустимые символы")
    return normalized


def _normalize_suggestion_text(value: str, *, maximum: int, label: str) -> str:
    if type(value) is not str:
        raise LocalAiInvalidSuggestionError(f"Локальный AI вернул некорректное поле: {label}")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized or len(normalized) > maximum:
        raise LocalAiInvalidSuggestionError(f"Локальный AI вернул некорректное поле: {label}")
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise LocalAiInvalidSuggestionError(f"Локальный AI вернул некорректное поле: {label}")
    return normalized


@dataclass(frozen=True, slots=True)
class LocalAiSuggestion:
    """Exact, persistence-neutral suggestion returned by an untrusted provider."""

    amount_decimal: str = field(repr=False)
    transaction_type: TransactionType = field(repr=False)
    description: str = field(repr=False)
    category_hint: str | None = field(default=None, repr=False)
    account_hint: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if type(self.amount_decimal) is not str or not _CANONICAL_DECIMAL.fullmatch(
            self.amount_decimal
        ):
            raise LocalAiInvalidSuggestionError("Локальный AI вернул некорректную сумму")
        if not isinstance(self.transaction_type, TransactionType):
            raise LocalAiInvalidSuggestionError("Локальный AI вернул некорректный тип операции")
        object.__setattr__(
            self,
            "description",
            _normalize_suggestion_text(self.description, maximum=500, label="description"),
        )
        for attribute in ("category_hint", "account_hint"):
            value = getattr(self, attribute)
            if value is not None:
                object.__setattr__(
                    self,
                    attribute,
                    _normalize_suggestion_text(
                        value,
                        maximum=MAX_LOCAL_AI_HINT_CHARS,
                        label=attribute,
                    ),
                )


@dataclass(frozen=True, slots=True)
class SuggestLocalTransactionCommand:
    text: str = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", normalize_local_ai_input(self.text))


@dataclass(frozen=True, slots=True)
class CreateLocalAiDraftCommand:
    owner_id: UUID = field(repr=False)
    draft: TransactionDraft = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise TypeError("Local AI draft owner id must be a UUID")
        if not isinstance(self.draft, TransactionDraft):
            raise TypeError("Local AI draft suggestion must be a TransactionDraft")


class LocalAiSuggestionProvider(Protocol):
    async def suggest(self, text: str) -> LocalAiSuggestion: ...


__all__ = [
    "CreateLocalAiDraftCommand",
    "LocalAiDisabledError",
    "LocalAiInvalidSuggestionError",
    "LocalAiSuggestion",
    "LocalAiSuggestionProvider",
    "LocalAiUnavailableError",
    "MAX_LOCAL_AI_HINT_CHARS",
    "MAX_LOCAL_AI_INPUT_CHARS",
    "MAX_LOCAL_AI_RESPONSE_BYTES",
    "SuggestLocalTransactionCommand",
    "normalize_local_ai_input",
]
