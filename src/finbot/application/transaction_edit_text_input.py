from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from finbot.application.dto import (
    DraftRef,
    DraftSnapshot,
    OwnerSnapshot,
    TransactionSnapshot,
)
from finbot.application.errors import InvalidStateError

MAX_TRANSACTION_EDIT_TEXT_LENGTH = 4096
TRANSACTION_EDIT_TEXT_STATES = frozenset({"edit_amount", "edit_date", "edit_description"})


class TransactionEditTextInputStatus(StrEnum):
    UPDATED = "updated"
    RETRY = "retry"


class TransactionEditTextInputField(StrEnum):
    AMOUNT = "amount"
    DATE = "date"
    DESCRIPTION = "description"


class TransactionEditTextInputError(StrEnum):
    """Safe renderer-facing reasons that never contain submitted text."""

    INVALID_AMOUNT = "invalid_amount"
    INVALID_DATE = "invalid_date"
    INVALID_DESCRIPTION = "invalid_description"


class TransactionEditTextInputNotApplicableError(InvalidStateError):
    """The active flow belongs to another bounded text-input controller."""


@dataclass(frozen=True, slots=True)
class TransactionEditTextInputCommand:
    owner_id: UUID = field(repr=False)
    expected: DraftRef = field(repr=False)
    text: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise TypeError("Transaction edit owner id must be a UUID")
        if not isinstance(self.expected, DraftRef):
            raise TypeError("Expected transaction edit draft must be a DraftRef")
        if not isinstance(self.text, str):
            raise TypeError("Transaction edit text must be a string")
        if len(self.text) > MAX_TRANSACTION_EDIT_TEXT_LENGTH:
            raise ValueError("Transaction edit text is too long")


@dataclass(frozen=True, slots=True)
class TransactionEditTextTarget:
    """Authoritative owner/draft/transaction tuple locked by the adapter."""

    owner: OwnerSnapshot = field(repr=False)
    draft: DraftSnapshot = field(repr=False)
    transaction: TransactionSnapshot = field(repr=False)

    def __post_init__(self) -> None:
        if self.draft.suspended or self.draft.state not in TRANSACTION_EDIT_TEXT_STATES:
            raise ValueError("Transaction edit target draft state is invalid")
        if set(self.draft.payload) != {"transaction_id", "version"}:
            raise ValueError("Transaction edit target draft is not canonical")
        if str(self.draft.payload["transaction_id"]) != str(self.transaction.transaction_id):
            raise ValueError("Transaction edit draft targets another transaction")
        version = self.draft.payload["version"]
        if isinstance(version, bool):
            raise ValueError("Transaction edit draft version is invalid")
        try:
            expected_version = int(str(version))
        except ValueError:
            raise ValueError("Transaction edit draft version is invalid") from None
        if expected_version != self.transaction.version:
            raise ValueError("Transaction edit draft version is stale")
        if self.transaction.deleted_at is not None:
            raise ValueError("Transaction edit target must be active")


@dataclass(frozen=True, slots=True)
class TransactionEditTextInputResult:
    status: TransactionEditTextInputStatus
    edited_field: TransactionEditTextInputField
    owner: OwnerSnapshot = field(repr=False)
    transaction: TransactionSnapshot = field(repr=False)
    draft: DraftSnapshot | None = field(default=None, repr=False)
    retry_error: TransactionEditTextInputError | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.edited_field, TransactionEditTextInputField):
            raise TypeError("Transaction edit field is invalid")
        is_retry = self.status is TransactionEditTextInputStatus.RETRY
        if is_retry != (self.retry_error is not None):
            raise ValueError("Transaction edit retry status and reason do not match")
        if is_retry != (self.draft is not None):
            raise ValueError("Transaction edit retry status and draft do not match")
        if self.draft is not None:
            if self.draft.suspended or self.draft.state not in TRANSACTION_EDIT_TEXT_STATES:
                raise ValueError("Transaction edit retry draft is invalid")


class TransactionEditTextTargetRepository(Protocol):
    """Lock one exact edit draft and its active versioned transaction."""

    async def lock_target(
        self,
        owner_id: UUID,
        expected: DraftRef,
    ) -> TransactionEditTextTarget: ...


class TransactionEditTextInputClock(Protocol):
    def now(self, timezone: str) -> datetime: ...
