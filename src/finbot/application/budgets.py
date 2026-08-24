from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from finbot.domain.budgets import (
    BudgetDefinition,
    BudgetForecastState,
    BudgetProgress,
    validate_budget_currency,
)

MAX_BUDGET_PAGE_SIZE = 50


def _is_aware_datetime(value: object) -> bool:
    return isinstance(value, datetime) and value.utcoffset() is not None


@dataclass(frozen=True, slots=True, repr=False)
class BudgetRef:
    budget_id: UUID = field(repr=False)
    version: int = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.budget_id, UUID):
            raise ValueError("Budget id is invalid")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("Budget version must be positive")


@dataclass(frozen=True, slots=True, repr=False)
class BudgetSnapshot:
    budget_id: UUID = field(repr=False)
    owner_id: UUID = field(repr=False)
    definition: BudgetDefinition = field(repr=False)
    version: int = field(repr=False)
    created_at: datetime = field(repr=False)
    updated_at: datetime = field(repr=False)
    deleted_at: datetime | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        BudgetRef(self.budget_id, self.version)
        if not isinstance(self.owner_id, UUID):
            raise ValueError("Budget owner id is invalid")
        if not isinstance(self.definition, BudgetDefinition):
            raise ValueError("Budget definition is invalid")
        if self.deleted_at is not None and not _is_aware_datetime(self.deleted_at):
            raise ValueError("Budget deletion timestamp must contain a timezone")
        for value in (self.created_at, self.updated_at):
            if not _is_aware_datetime(value):
                raise ValueError("Budget timestamps must contain a timezone")
        if self.updated_at < self.created_at:
            raise ValueError("Budget update timestamp precedes creation")
        if self.deleted_at is not None and self.deleted_at < self.created_at:
            raise ValueError("Budget deletion timestamp precedes creation")

    @property
    def ref(self) -> BudgetRef:
        return BudgetRef(self.budget_id, self.version)


@dataclass(frozen=True, slots=True, repr=False)
class BudgetProgressSnapshot:
    budget: BudgetSnapshot = field(repr=False)
    progress: BudgetProgress = field(repr=False)
    known_recurring_minor: int = field(repr=False)
    safe_daily_minor: int = field(repr=False)
    forecast_minor: int = field(repr=False)
    state: BudgetForecastState
    measured_at: datetime = field(repr=False)
    cutoff_at: datetime = field(repr=False)

    def __post_init__(self) -> None:
        if not _is_aware_datetime(self.measured_at):
            raise ValueError("Budget progress timestamp must contain a timezone")
        if not _is_aware_datetime(self.cutoff_at):
            raise ValueError("Budget progress cutoff must contain a timezone")
        for value in (
            self.known_recurring_minor,
            self.safe_daily_minor,
            self.forecast_minor,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("Budget forecast value must be non-negative")
        if self.forecast_minor != self.progress.spent_minor + self.known_recurring_minor:
            raise ValueError("Budget forecast does not match spent and recurring amounts")
        if not isinstance(self.state, BudgetForecastState):
            raise ValueError("Budget forecast state is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class BudgetCursor:
    starts_on: date = field(repr=False)
    budget_id: UUID = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.starts_on, date) or isinstance(self.starts_on, datetime):
            raise ValueError("Budget cursor date is invalid")
        if not isinstance(self.budget_id, UUID):
            raise ValueError("Budget cursor id is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class BudgetCursorItem:
    budget: BudgetSnapshot = field(repr=False)
    cursor: BudgetCursor = field(repr=False)

    def __post_init__(self) -> None:
        if self.budget.budget_id != self.cursor.budget_id:
            raise ValueError("Budget cursor does not match its item")
        if self.budget.definition.starts_on != self.cursor.starts_on:
            raise ValueError("Budget cursor date does not match its item")


@dataclass(frozen=True, slots=True, repr=False)
class BudgetPageSnapshot:
    items: tuple[BudgetProgressSnapshot, ...] = field(repr=False)
    next_cursor: BudgetCursor | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if len(self.items) > MAX_BUDGET_PAGE_SIZE:
            raise ValueError("Budget page is too large")


@dataclass(frozen=True, slots=True, repr=False)
class BudgetSpendWindow:
    budget_id: UUID = field(repr=False)
    currency: str = field(repr=False)
    category_id: UUID | None = field(repr=False)
    start: datetime = field(repr=False)
    end: datetime = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.budget_id, UUID):
            raise ValueError("Budget spend window id is invalid")
        validate_budget_currency(self.currency)
        if self.category_id is not None and not isinstance(self.category_id, UUID):
            raise ValueError("Budget spend window category is invalid")
        if (
            not _is_aware_datetime(self.start)
            or not _is_aware_datetime(self.end)
            or self.start >= self.end
        ):
            raise ValueError("Budget spend window bounds are invalid")


@dataclass(frozen=True, slots=True, repr=False)
class CreateBudgetCommand:
    owner_id: UUID = field(repr=False)
    definition: BudgetDefinition = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class ReplaceBudgetCommand:
    owner_id: UUID = field(repr=False)
    expected: BudgetRef = field(repr=False)
    definition: BudgetDefinition = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class VersionedBudgetCommand:
    owner_id: UUID = field(repr=False)
    expected: BudgetRef = field(repr=False)


class BudgetCommandRepository(Protocol):
    """Owner-scoped budget mutations; caller owns commit and rollback."""

    async def create(self, command: CreateBudgetCommand) -> BudgetSnapshot: ...

    async def get(self, owner_id: UUID, budget_id: UUID) -> BudgetSnapshot | None: ...

    async def replace(self, command: ReplaceBudgetCommand) -> BudgetSnapshot: ...

    async def delete(self, command: VersionedBudgetCommand) -> BudgetSnapshot: ...

    async def restore(self, command: VersionedBudgetCommand) -> BudgetSnapshot: ...


class BudgetReader(Protocol):
    async def get(self, owner_id: UUID, budget_id: UUID) -> BudgetSnapshot | None: ...

    async def list_after(
        self,
        owner_id: UUID,
        *,
        window_start: date,
        window_end: date,
        deleted: bool,
        cursor: BudgetCursor | None,
        limit: int,
    ) -> tuple[BudgetCursorItem, ...]: ...

    async def spent_minor_for_budgets(
        self,
        owner_id: UUID,
        windows: tuple[BudgetSpendWindow, ...],
    ) -> dict[UUID, int]: ...

    async def known_recurring_minor_for_budgets(
        self,
        owner_id: UUID,
        windows: tuple[BudgetSpendWindow, ...],
    ) -> dict[UUID, int]:
        """Aggregate unresolved instances plus not-yet-materialized active occurrences."""
        ...
