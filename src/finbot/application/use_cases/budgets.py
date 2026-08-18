from datetime import UTC, date, datetime
from uuid import UUID

from finbot.application.budgets import (
    MAX_BUDGET_PAGE_SIZE,
    BudgetCommandRepository,
    BudgetCursor,
    BudgetPageSnapshot,
    BudgetProgressSnapshot,
    BudgetReader,
    BudgetSnapshot,
    BudgetSpendWindow,
    CreateBudgetCommand,
    ReplaceBudgetCommand,
    VersionedBudgetCommand,
)
from finbot.application.errors import (
    ApplicationValidationError,
    EntityNotFoundError,
)
from finbot.domain.budgets import (
    budget_period_bounds,
    calculate_budget_progress,
    validate_budget_period,
)


def _aware_utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ApplicationValidationError("Текущее время должно содержать часовой пояс")
    return value.astimezone(UTC)


def _validate_owner(owner_id: UUID) -> None:
    if not isinstance(owner_id, UUID):
        raise ApplicationValidationError("Владелец бюджета не прошёл проверку")


class BudgetUseCases:
    """Framework-neutral, owner-scoped budget command API."""

    __slots__ = ("_commands",)

    def __init__(self, commands: BudgetCommandRepository) -> None:
        self._commands = commands

    async def create(self, command: CreateBudgetCommand) -> BudgetSnapshot:
        _validate_owner(command.owner_id)
        try:
            return await self._commands.create(command)
        except ValueError as exc:
            raise ApplicationValidationError(str(exc)) from exc

    async def get(self, owner_id: UUID, budget_id: UUID) -> BudgetSnapshot:
        _validate_owner(owner_id)
        budget = await self._commands.get(owner_id, budget_id)
        if budget is None:
            raise EntityNotFoundError("Бюджет не найден")
        return budget

    async def replace(self, command: ReplaceBudgetCommand) -> BudgetSnapshot:
        _validate_owner(command.owner_id)
        try:
            return await self._commands.replace(command)
        except ValueError as exc:
            raise ApplicationValidationError(str(exc)) from exc

    async def delete(self, command: VersionedBudgetCommand) -> BudgetSnapshot:
        _validate_owner(command.owner_id)
        return await self._commands.delete(command)

    async def restore(self, command: VersionedBudgetCommand) -> BudgetSnapshot:
        _validate_owner(command.owner_id)
        return await self._commands.restore(command)


class GetBudgetProgress:
    __slots__ = ("_reader",)

    def __init__(self, reader: BudgetReader) -> None:
        self._reader = reader

    async def __call__(
        self,
        owner_id: UUID,
        budget_id: UUID,
        *,
        as_of: datetime,
    ) -> BudgetProgressSnapshot:
        _validate_owner(owner_id)
        budget = await self._reader.get(owner_id, budget_id)
        if budget is None:
            raise EntityNotFoundError("Бюджет не найден")
        return (await _progress_many(self._reader, owner_id, (budget,), as_of=as_of))[0]


class ListBudgetProgress:
    __slots__ = ("_reader",)

    def __init__(self, reader: BudgetReader) -> None:
        self._reader = reader

    async def __call__(
        self,
        owner_id: UUID,
        *,
        window_start: date,
        window_end: date,
        deleted: bool,
        cursor: BudgetCursor | None,
        limit: int,
        as_of: datetime,
    ) -> BudgetPageSnapshot:
        _validate_owner(owner_id)
        if type(deleted) is not bool:
            raise ApplicationValidationError("Признак удаления бюджета не прошёл проверку")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_BUDGET_PAGE_SIZE
        ):
            raise ApplicationValidationError("Размер страницы бюджетов должен быть от 1 до 50")
        try:
            validate_budget_period(window_start, window_end)
        except ValueError as exc:
            raise ApplicationValidationError(str(exc)) from exc
        rows = await self._reader.list_after(
            owner_id,
            window_start=window_start,
            window_end=window_end,
            deleted=deleted,
            cursor=cursor,
            limit=limit + 1,
        )
        if len(rows) > limit + 1:
            raise RuntimeError("Budget reader violated the bounded fetch contract")
        selected = rows[:limit]
        progress = await _progress_many(
            self._reader,
            owner_id,
            tuple(row.budget for row in selected),
            as_of=as_of,
        )
        next_cursor = rows[limit - 1].cursor if len(rows) > limit else None
        return BudgetPageSnapshot(items=progress, next_cursor=next_cursor)


async def _progress_many(
    reader: BudgetReader,
    owner_id: UUID,
    budgets: tuple[BudgetSnapshot, ...],
    *,
    as_of: datetime,
) -> tuple[BudgetProgressSnapshot, ...]:
    measured_at = _aware_utc(as_of)
    bounds = tuple(
        budget_period_bounds(
            budget.definition.starts_on,
            budget.definition.ends_on,
            budget.definition.timezone,
        )
        for budget in budgets
    )
    cutoffs = tuple(min(max(measured_at, start), end) for start, end in bounds)
    windows = tuple(
        BudgetSpendWindow(
            budget_id=budget.budget_id,
            currency=budget.definition.currency,
            category_id=budget.definition.category_id,
            start=start,
            end=cutoff,
        )
        for budget, (start, _end), cutoff in zip(budgets, bounds, cutoffs, strict=True)
        if cutoff > start
    )
    spent_by_budget = await reader.spent_minor_for_budgets(owner_id, windows) if windows else {}
    expected_ids = {window.budget_id for window in windows}
    if set(spent_by_budget) != expected_ids:
        raise RuntimeError("Budget reader returned an incomplete aggregate")
    return tuple(
        BudgetProgressSnapshot(
            budget=budget,
            progress=calculate_budget_progress(
                budget.definition.limit_minor,
                spent_by_budget.get(budget.budget_id, 0),
            ),
            measured_at=measured_at,
            cutoff_at=cutoff,
        )
        for budget, cutoff in zip(budgets, cutoffs, strict=True)
    )
