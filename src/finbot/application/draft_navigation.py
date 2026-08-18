from dataclasses import dataclass, field
from datetime import datetime
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
)
from finbot.domain.transactions import TransactionType


class DraftNavigationAction(StrEnum):
    """Closed set of draft navigation and selection actions."""

    EDIT_TYPE = "edit_type"
    EDIT_AMOUNT = "edit_amount"
    EDIT_CATEGORY = "edit_category"
    EDIT_ACCOUNT = "edit_account"
    EDIT_DATE = "edit_date"
    EDIT_DESCRIPTION = "edit_description"
    SKIP_DESCRIPTION = "skip_description"
    BACK = "back"
    SELECT_TYPE = "select_type"
    SELECT_CATEGORY = "select_category"
    SELECT_ACCOUNT = "select_account"
    SELECT_DATE = "select_date"


class DraftDateChoice(StrEnum):
    TODAY = "today"
    YESTERDAY = "yesterday"
    CUSTOM = "custom"


class DraftCatalogChoice(StrEnum):
    """Explicit non-entity choice used by account/category selectors."""

    CUSTOM = "custom"


@dataclass(frozen=True, slots=True)
class DraftCatalogRef:
    """Versioned owner-catalog reference carried by an interaction."""

    entity_id: UUID = field(repr=False)
    version: int = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.entity_id, UUID):
            raise TypeError("Catalog id must be a UUID")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TypeError("Catalog version must be an integer")
        if self.version < 1:
            raise ValueError("Catalog version must be positive")


type DraftNavigationChoice = (
    TransactionType | DraftCatalogRef | DraftCatalogChoice | DraftDateChoice
)


class DraftNavigationStatus(StrEnum):
    UPDATED = "updated"
    CLOSED = "closed"


_REVIEW_STATES = frozenset({"wizard_confirm", "quick_confirm", "review"})
_BACK_STATES = frozenset(
    {
        "review_type",
        "review_amount",
        "review_category",
        "review_account",
        "review_date",
        "review_date_input",
        "wizard_amount",
        "wizard_category",
        "custom_category",
        "wizard_account",
        "quick_account",
        "account_required",
        "custom_account",
        "custom_date",
        "wizard_date",
        "wizard_description",
        "wizard_confirm",
        "quick_confirm",
        "review",
        "quick_category",
        "category_required",
    }
)

DRAFT_NAVIGATION_ALLOWED_STATES = MappingProxyType(
    {
        DraftNavigationAction.EDIT_TYPE: _REVIEW_STATES,
        DraftNavigationAction.EDIT_AMOUNT: _REVIEW_STATES,
        DraftNavigationAction.EDIT_CATEGORY: _REVIEW_STATES,
        DraftNavigationAction.EDIT_ACCOUNT: _REVIEW_STATES,
        DraftNavigationAction.EDIT_DATE: _REVIEW_STATES,
        DraftNavigationAction.EDIT_DESCRIPTION: _REVIEW_STATES,
        DraftNavigationAction.SKIP_DESCRIPTION: frozenset({"wizard_description"}),
        DraftNavigationAction.BACK: _BACK_STATES,
        DraftNavigationAction.SELECT_TYPE: frozenset({"wizard_type", "review_type"}),
        DraftNavigationAction.SELECT_CATEGORY: frozenset(
            {"wizard_category", "quick_category", "category_required", "review_category"}
        ),
        DraftNavigationAction.SELECT_ACCOUNT: frozenset(
            {"wizard_account", "quick_account", "account_required", "review_account"}
        ),
        DraftNavigationAction.SELECT_DATE: frozenset({"wizard_date", "review_date"}),
    }
)

_SELECTION_ACTIONS = frozenset(
    {
        DraftNavigationAction.SELECT_TYPE,
        DraftNavigationAction.SELECT_CATEGORY,
        DraftNavigationAction.SELECT_ACCOUNT,
        DraftNavigationAction.SELECT_DATE,
    }
)


@dataclass(frozen=True, slots=True)
class DraftNavigationCommand:
    owner_id: UUID = field(repr=False)
    expected: DraftRef = field(repr=False)
    action: DraftNavigationAction
    choice: DraftNavigationChoice | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise TypeError("Draft owner id must be a UUID")
        if not isinstance(self.expected, DraftRef):
            raise TypeError("Expected draft reference must be a DraftRef")
        if not isinstance(self.action, DraftNavigationAction):
            raise TypeError("Draft navigation action is invalid")
        if self.action is DraftNavigationAction.SELECT_TYPE:
            is_valid_choice = isinstance(self.choice, TransactionType)
        elif self.action in {
            DraftNavigationAction.SELECT_CATEGORY,
            DraftNavigationAction.SELECT_ACCOUNT,
        }:
            is_valid_choice = isinstance(self.choice, (DraftCatalogRef, DraftCatalogChoice))
        elif self.action is DraftNavigationAction.SELECT_DATE:
            is_valid_choice = isinstance(self.choice, DraftDateChoice)
        else:
            is_valid_choice = self.choice is None
        if not is_valid_choice:
            if self.action in _SELECTION_ACTIONS:
                raise TypeError("Draft selection choice does not match its action")
            raise TypeError("Draft navigation action must not contain a selection choice")


@dataclass(frozen=True, slots=True)
class DraftNavigationChoices:
    """Typed catalog values required to render the resulting draft screen."""

    accounts: tuple[AccountSnapshot, ...] = field(default=(), repr=False)
    categories: tuple[CategorySnapshot, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if self.accounts and self.categories:
            raise ValueError("Draft navigation cannot present two catalog kinds at once")
        if any(account.archived_at is not None for account in self.accounts):
            raise ValueError("Draft navigation choices must contain active accounts")
        if any(category.archived_at is not None for category in self.categories):
            raise ValueError("Draft navigation choices must contain active categories")


@dataclass(frozen=True, slots=True)
class DraftNavigationResult:
    action: DraftNavigationAction
    status: DraftNavigationStatus
    owner: OwnerSnapshot = field(repr=False)
    draft: DraftSnapshot | None = field(default=None, repr=False)
    choices: DraftNavigationChoices = field(default_factory=DraftNavigationChoices, repr=False)

    def __post_init__(self) -> None:
        if self.status is DraftNavigationStatus.UPDATED and self.draft is None:
            raise ValueError("Updated draft navigation result requires a draft")
        if self.status is DraftNavigationStatus.CLOSED and self.draft is not None:
            raise ValueError("Closed draft navigation result must not contain a draft")
        if self.status is DraftNavigationStatus.CLOSED and (
            self.choices.accounts or self.choices.categories
        ):
            raise ValueError("Closed draft navigation result must not contain choices")


class DraftNavigationOwnerQuery(Protocol):
    async def __call__(self, owner_id: UUID) -> OwnerSnapshot: ...


class DraftNavigationAccountQuery(Protocol):
    async def __call__(
        self, owner_id: UUID, *, archived: bool = False
    ) -> tuple[AccountSnapshot, ...]: ...


class DraftNavigationCategoryQuery(Protocol):
    async def __call__(
        self,
        owner_id: UUID,
        *,
        kind: TransactionType | str | None = None,
        archived: bool = False,
    ) -> tuple[CategorySnapshot, ...]: ...


class DraftNavigationCatalogPort(Protocol):
    """Authoritative catalog selection and fallback resolution boundary.

    Implementations must validate owner, active state, kind, and optimistic
    version in the caller's transaction.  Resolving an account or fallback
    category must share the catalog-mutation serialization boundary so an
    archive cannot race between validation and the draft transition.
    """

    async def select_account(
        self,
        owner_id: UUID,
        reference: DraftCatalogRef,
    ) -> AccountSnapshot: ...

    async def select_category(
        self,
        owner_id: UUID,
        kind: TransactionType,
        reference: DraftCatalogRef,
    ) -> CategorySnapshot: ...

    async def resolve_account(
        self,
        owner_id: UUID,
        hint: str | None,
        default_account_id: UUID | None,
    ) -> AccountSnapshot | None: ...

    async def resolve_fallback_category(
        self,
        owner_id: UUID,
        kind: TransactionType,
    ) -> CategorySnapshot: ...


class DraftNavigationClock(Protocol):
    def now(self, timezone: str) -> datetime: ...
