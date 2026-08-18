from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    DraftRef,
    DraftSnapshot,
    OwnerSnapshot,
    TransactionSnapshot,
)
from finbot.domain.transactions import TransactionType


class TransactionDraftNavigationAction(StrEnum):
    """Closed set of non-mutating saved-transaction edit navigation actions."""

    EDIT_TYPE = "edit_type"
    EDIT_AMOUNT = "edit_amount"
    EDIT_CATEGORY = "edit_category"
    EDIT_ACCOUNT = "edit_account"
    EDIT_DATE = "edit_date"
    EDIT_DESCRIPTION = "edit_description"
    DATE_BACK = "date_back"
    BACK = "back"


TRANSACTION_DRAFT_NAVIGATION_ALLOWED_STATES = MappingProxyType(
    {
        TransactionDraftNavigationAction.EDIT_TYPE: frozenset({"edit_menu"}),
        TransactionDraftNavigationAction.EDIT_AMOUNT: frozenset({"edit_menu"}),
        TransactionDraftNavigationAction.EDIT_CATEGORY: frozenset({"edit_menu"}),
        TransactionDraftNavigationAction.EDIT_ACCOUNT: frozenset({"edit_menu"}),
        TransactionDraftNavigationAction.EDIT_DATE: frozenset({"edit_menu"}),
        TransactionDraftNavigationAction.EDIT_DESCRIPTION: frozenset({"edit_menu"}),
        TransactionDraftNavigationAction.DATE_BACK: frozenset({"edit_date"}),
        TransactionDraftNavigationAction.BACK: frozenset(
            {
                "edit_type",
                "edit_amount",
                "edit_category",
                "edit_account",
                "edit_date_menu",
                "edit_description",
            }
        ),
    }
)

TRANSACTION_DRAFT_NAVIGATION_TARGET_STATES = MappingProxyType(
    {
        TransactionDraftNavigationAction.EDIT_TYPE: "edit_type",
        TransactionDraftNavigationAction.EDIT_AMOUNT: "edit_amount",
        TransactionDraftNavigationAction.EDIT_CATEGORY: "edit_category",
        TransactionDraftNavigationAction.EDIT_ACCOUNT: "edit_account",
        TransactionDraftNavigationAction.EDIT_DATE: "edit_date_menu",
        TransactionDraftNavigationAction.EDIT_DESCRIPTION: "edit_description",
        TransactionDraftNavigationAction.DATE_BACK: "edit_date_menu",
        TransactionDraftNavigationAction.BACK: "edit_menu",
    }
)


@dataclass(frozen=True, slots=True)
class TransactionDraftNavigationCommand:
    owner_id: UUID = field(repr=False)
    expected: DraftRef = field(repr=False)
    action: TransactionDraftNavigationAction

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise TypeError("Transaction draft owner id must be a UUID")
        if not isinstance(self.expected, DraftRef):
            raise TypeError("Expected transaction draft reference must be a DraftRef")
        if not isinstance(self.action, TransactionDraftNavigationAction):
            raise TypeError("Transaction draft navigation action is invalid")


@dataclass(frozen=True, slots=True)
class TransactionDraftNavigationChoices:
    """Active catalog snapshots required to render one resulting edit screen."""

    accounts: tuple[AccountSnapshot, ...] = field(default=(), repr=False)
    categories: tuple[CategorySnapshot, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if self.accounts and self.categories:
            raise ValueError("Transaction draft navigation cannot present two catalog kinds")
        if any(account.archived_at is not None for account in self.accounts):
            raise ValueError("Transaction draft account choices must be active")
        if any(category.archived_at is not None for category in self.categories):
            raise ValueError("Transaction draft category choices must be active")


@dataclass(frozen=True, slots=True)
class TransactionDraftNavigationResult:
    action: TransactionDraftNavigationAction
    owner: OwnerSnapshot = field(repr=False)
    draft: DraftSnapshot = field(repr=False)
    transaction: TransactionSnapshot = field(repr=False)
    choices: TransactionDraftNavigationChoices = field(
        default_factory=TransactionDraftNavigationChoices,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.draft.state != TRANSACTION_DRAFT_NAVIGATION_TARGET_STATES[self.action]:
            raise ValueError("Transaction draft navigation result has an invalid state")
        if self.transaction.deleted_at is not None:
            raise ValueError("Transaction draft navigation requires an active transaction")
        if self.action is TransactionDraftNavigationAction.EDIT_ACCOUNT:
            if self.choices.categories:
                raise ValueError("Account edit must not contain category choices")
        elif self.action is TransactionDraftNavigationAction.EDIT_CATEGORY:
            if self.choices.accounts:
                raise ValueError("Category edit must not contain account choices")
            if any(
                category.kind is not self.transaction.kind for category in self.choices.categories
            ):
                raise ValueError("Category choices must match the transaction type")
        elif self.choices.accounts or self.choices.categories:
            raise ValueError("This transaction draft screen must not contain catalog choices")


class TransactionDraftNavigationOwnerQuery(Protocol):
    async def __call__(self, owner_id: UUID) -> OwnerSnapshot: ...


class TransactionDraftNavigationTransactionQuery(Protocol):
    async def __call__(
        self,
        owner_id: UUID,
        transaction_id: UUID,
    ) -> TransactionSnapshot: ...


class TransactionDraftNavigationAccountQuery(Protocol):
    async def __call__(
        self,
        owner_id: UUID,
        *,
        archived: bool = False,
    ) -> tuple[AccountSnapshot, ...]: ...


class TransactionDraftNavigationCategoryQuery(Protocol):
    async def __call__(
        self,
        owner_id: UUID,
        *,
        kind: TransactionType | str | None = None,
        archived: bool = False,
    ) -> tuple[CategorySnapshot, ...]: ...
