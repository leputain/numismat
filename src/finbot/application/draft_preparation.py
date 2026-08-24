from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol
from uuid import UUID

from finbot.application.dto import AccountSnapshot, CategorySnapshot
from finbot.domain.money import MoneyError, validate_minor
from finbot.domain.transactions import TransactionDraft, TransactionType

_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "history_page",
        "pending_intent",
        "presentation_ref",
        "telegram_chat_id",
        "telegram_message_id",
        "ui_message_id",
    }
)


def _channel_neutral_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    forbidden = {
        key
        for key in payload
        if key in _FORBIDDEN_PAYLOAD_KEYS or key == "slug" or key.endswith("_slug")
    }
    if forbidden:
        raise ValueError("Prepared draft payload contains adapter state")
    return MappingProxyType(deepcopy(dict(payload)))


class DraftPreparationState(StrEnum):
    REVIEW = "review"
    CATEGORY_REQUIRED = "category_required"
    ACCOUNT_REQUIRED = "account_required"
    TYPE_REQUIRED = "wizard_type"


@dataclass(frozen=True, slots=True)
class PrepareParsedDraftCommand:
    owner_id: UUID = field(repr=False)
    draft: TransactionDraft = field(repr=False)
    flow: str = "quick"

    def __post_init__(self) -> None:
        if not self.flow or len(self.flow) > 30:
            raise ValueError("Draft flow must contain between 1 and 30 characters")


@dataclass(frozen=True, slots=True)
class PrepareQuickDraftCommand:
    owner_id: UUID = field(repr=False)
    text: str = field(repr=False)
    flow: str = "quick"

    def __post_init__(self) -> None:
        if not self.flow or len(self.flow) > 30:
            raise ValueError("Draft flow must contain between 1 and 30 characters")


@dataclass(frozen=True, slots=True)
class PreparedDraftResult:
    state: DraftPreparationState
    payload: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        payload = _channel_neutral_payload(self.payload)
        if self.state is DraftPreparationState.TYPE_REQUIRED:
            if (
                set(payload) != {"flow", "input_mode", "amount_minor"}
                or payload.get("flow") != "quick"
                or payload.get("input_mode") != "amount_only"
            ):
                raise ValueError("Amount-only prepared draft payload is invalid")
            try:
                validate_minor(payload["amount_minor"])
            except MoneyError:
                raise ValueError("Amount-only prepared draft payload is invalid") from None
        object.__setattr__(self, "payload", payload)


@dataclass(frozen=True, slots=True)
class AmountOnlyQuickDraft:
    """Validated amount captured before transaction type and currency are known."""

    amount_minor: int = field(repr=False)

    def __post_init__(self) -> None:
        try:
            validated = validate_minor(self.amount_minor)
        except MoneyError:
            raise ValueError("Amount-only quick draft is invalid") from None
        object.__setattr__(self, "amount_minor", validated)


class SignedAmountOnlyQuickDraftError(ValueError):
    """A safe parser signal for a standalone amount carrying a type-like sign."""

    def __init__(self) -> None:
        super().__init__("Amount-only quick draft must not contain a sign")


type QuickDraftParseResult = TransactionDraft | AmountOnlyQuickDraft


class QuickDraftParser(Protocol):
    def parse(self, text: str, *, timezone: str) -> QuickDraftParseResult: ...


class DraftPreparationClock(Protocol):
    def now(self, timezone: str) -> datetime: ...


class DraftCatalogResolver(Protocol):
    async def resolve_account(
        self,
        owner_id: UUID,
        hint: str | None,
        default_account_id: UUID | None,
    ) -> AccountSnapshot | None: ...

    async def resolve_category(
        self,
        owner_id: UUID,
        kind: TransactionType,
        hint: str | None,
    ) -> CategorySnapshot | None: ...

    async def get_category(
        self,
        owner_id: UUID,
        kind: TransactionType,
        category_id: UUID,
    ) -> CategorySnapshot | None: ...
