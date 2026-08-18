from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

from finbot.application.draft_navigation import DraftCatalogRef, DraftDateChoice
from finbot.application.dto import (
    DraftRef,
    DraftSnapshot,
    OwnerSnapshot,
    TransactionMutationResult,
    TransactionSnapshot,
)
from finbot.domain.transactions import TransactionType


class TransactionDraftSelectionAction(StrEnum):
    """Closed set of choices available while editing a saved transaction."""

    TYPE = "type"
    CATEGORY = "category"
    ACCOUNT = "account"
    DATE = "date"


TRANSACTION_DRAFT_SELECTION_STATES = MappingProxyType(
    {
        TransactionDraftSelectionAction.TYPE: "edit_type",
        TransactionDraftSelectionAction.CATEGORY: "edit_category",
        TransactionDraftSelectionAction.ACCOUNT: "edit_account",
        TransactionDraftSelectionAction.DATE: "edit_date_menu",
    }
)


@dataclass(frozen=True, slots=True)
class TransactionTypeSelection:
    kind: TransactionType
    category: DraftCatalogRef = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TransactionType):
            raise TypeError("Transaction type selection kind is invalid")
        if not isinstance(self.category, DraftCatalogRef):
            raise TypeError("Transaction type selection category is invalid")


type TransactionDraftSelectionChoice = DraftCatalogRef | DraftDateChoice | TransactionTypeSelection


@dataclass(frozen=True, slots=True)
class TransactionDraftSelectionCommand:
    owner_id: UUID = field(repr=False)
    expected: DraftRef = field(repr=False)
    action: TransactionDraftSelectionAction
    choice: TransactionDraftSelectionChoice = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise TypeError("Transaction draft owner id must be a UUID")
        if not isinstance(self.expected, DraftRef):
            raise TypeError("Expected transaction draft reference must be a DraftRef")
        if not isinstance(self.action, TransactionDraftSelectionAction):
            raise TypeError("Transaction draft selection action is invalid")
        if self.action is TransactionDraftSelectionAction.TYPE:
            is_valid_choice = isinstance(self.choice, TransactionTypeSelection)
        elif self.action in {
            TransactionDraftSelectionAction.CATEGORY,
            TransactionDraftSelectionAction.ACCOUNT,
        }:
            is_valid_choice = isinstance(self.choice, DraftCatalogRef)
        else:
            is_valid_choice = isinstance(self.choice, DraftDateChoice)
        if not is_valid_choice:
            raise TypeError("Transaction draft selection choice does not match its action")


class TransactionDraftSelectionStatus(StrEnum):
    TRANSACTION_UPDATED = "transaction_updated"
    DATE_INPUT_REQUIRED = "date_input_required"


@dataclass(frozen=True, slots=True)
class TransactionDraftSelectionResult:
    action: TransactionDraftSelectionAction
    status: TransactionDraftSelectionStatus
    owner: OwnerSnapshot = field(repr=False)
    draft: DraftSnapshot | None = field(default=None, repr=False)
    transaction: TransactionSnapshot | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        requires_input = self.status is TransactionDraftSelectionStatus.DATE_INPUT_REQUIRED
        has_draft = self.draft is not None
        has_transaction = self.transaction is not None
        if requires_input != has_draft:
            raise ValueError("Date-input result must contain exactly one draft")
        if requires_input == has_transaction:
            raise ValueError("Updated transaction result must contain exactly one transaction")
        if requires_input and self.action is not TransactionDraftSelectionAction.DATE:
            raise ValueError("Only a date choice may request custom input")


class TransactionDraftSelectionRepository(Protocol):
    """Atomic saved-transaction edit authorized by one exact edit draft.

    Implementations own the authoritative owner, draft, transaction, and
    selected-catalog locks. They must update the transaction, append its audit
    event, and delete the draft without committing the caller-owned unit of work.
    """

    async def apply(
        self,
        command: TransactionDraftSelectionCommand,
        *,
        occurred_at: datetime | None = None,
    ) -> TransactionMutationResult: ...


class TransactionDraftSelectionOwnerQuery(Protocol):
    async def __call__(self, owner_id: UUID) -> OwnerSnapshot: ...


class TransactionDraftSelectionClock(Protocol):
    def now(self, timezone: str) -> datetime: ...
