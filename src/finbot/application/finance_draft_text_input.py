from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    DraftRef,
    DraftSnapshot,
    OwnerSnapshot,
)
from finbot.application.errors import InvalidStateError
from finbot.domain.transactions import TransactionType

MAX_FINANCE_DRAFT_TEXT_LENGTH = 4096
FINANCE_DRAFT_TEXT_INPUT_STATES = frozenset(
    {
        "wizard_amount",
        "custom_category",
        "custom_account",
        "custom_date",
        "wizard_description",
        "review_amount",
        "review_date_input",
    }
)


class FinanceDraftTextInputStatus(StrEnum):
    UPDATED = "updated"
    RETRY = "retry"


class FinanceDraftTextInputError(StrEnum):
    """Safe renderer-facing retry reasons that never contain submitted text."""

    INVALID_AMOUNT = "invalid_amount"
    INVALID_CATEGORY_NAME = "invalid_category_name"
    CATEGORY_UNAVAILABLE = "category_unavailable"
    INVALID_ACCOUNT_NAME = "invalid_account_name"
    ACCOUNT_UNAVAILABLE = "account_unavailable"
    INVALID_DATE = "invalid_date"
    INVALID_DESCRIPTION = "invalid_description"


class FinanceDraftTextInputNotApplicableError(InvalidStateError):
    """The active flow belongs to another bounded text-input controller."""


@dataclass(frozen=True, slots=True)
class FinanceDraftTextInputCommand:
    owner_id: UUID = field(repr=False)
    expected: DraftRef = field(repr=False)
    text: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise TypeError("Finance draft owner id must be a UUID")
        if not isinstance(self.expected, DraftRef):
            raise TypeError("Expected draft reference must be a DraftRef")
        if not isinstance(self.text, str):
            raise TypeError("Finance draft text must be a string")
        if len(self.text) > MAX_FINANCE_DRAFT_TEXT_LENGTH:
            raise ValueError("Finance draft text is too long")


@dataclass(frozen=True, slots=True)
class FinanceDraftTextInputChoices:
    """Typed catalog values required to render the resulting draft screen."""

    accounts: tuple[AccountSnapshot, ...] = field(default=(), repr=False)
    categories: tuple[CategorySnapshot, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if self.accounts and self.categories:
            raise ValueError("Finance draft text input cannot present two catalog kinds")
        if any(item.archived_at is not None for item in self.accounts):
            raise ValueError("Finance draft account choices must be active")
        if any(item.archived_at is not None for item in self.categories):
            raise ValueError("Finance draft category choices must be active")


@dataclass(frozen=True, slots=True)
class FinanceDraftTextInputResult:
    status: FinanceDraftTextInputStatus
    owner: OwnerSnapshot = field(repr=False)
    draft: DraftSnapshot = field(repr=False)
    choices: FinanceDraftTextInputChoices = field(
        default_factory=FinanceDraftTextInputChoices,
        repr=False,
    )
    retry_error: FinanceDraftTextInputError | None = None

    def __post_init__(self) -> None:
        if self.draft.suspended:
            raise ValueError("Finance draft text result must contain an active draft")
        is_retry = self.status is FinanceDraftTextInputStatus.RETRY
        if is_retry != (self.retry_error is not None):
            raise ValueError("Finance draft retry status and reason do not match")
        if is_retry and (self.choices.accounts or self.choices.categories):
            raise ValueError("Finance draft retry result must not contain choices")


class FinanceDraftTextInputOwnerQuery(Protocol):
    async def __call__(self, owner_id: UUID) -> OwnerSnapshot: ...


class FinanceDraftTextInputAccountQuery(Protocol):
    async def __call__(
        self,
        owner_id: UUID,
        *,
        archived: bool = False,
    ) -> tuple[AccountSnapshot, ...]: ...


class FinanceDraftTextInputCategoryQuery(Protocol):
    async def __call__(
        self,
        owner_id: UUID,
        *,
        kind: TransactionType | str | None = None,
        archived: bool = False,
    ) -> tuple[CategorySnapshot, ...]: ...


class FinanceDraftTextInputCatalogPort(Protocol):
    """Create-or-get and fallback resolution in the caller-owned transaction."""

    async def create_or_get_category(
        self,
        owner_id: UUID,
        name: str,
        kind: TransactionType,
    ) -> CategorySnapshot: ...

    async def create_or_get_account(
        self,
        owner_id: UUID,
        name: str,
        currency: str,
    ) -> AccountSnapshot: ...

    async def resolve_account(
        self,
        owner_id: UUID,
        hint: str | None,
        default_account_id: UUID | None,
    ) -> AccountSnapshot | None: ...


class FinanceDraftTextInputClock(Protocol):
    def now(self, timezone: str) -> datetime: ...
