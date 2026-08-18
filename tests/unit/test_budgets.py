from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from finbot.adapters.http.budgets.cursor import (
    BUDGET_CURSOR_LENGTH,
    BudgetCursorCodec,
    InvalidBudgetCursorError,
)
from finbot.application.budgets import (
    BudgetCursor,
    BudgetCursorItem,
    BudgetSnapshot,
    BudgetSpendWindow,
)
from finbot.application.use_cases.budgets import ListBudgetProgress
from finbot.domain.budgets import (
    BudgetDefinition,
    budget_period_bounds,
    calculate_budget_progress,
    validate_budget_period,
)

KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA"
OWNER_ID = UUID("018f0000-0000-7000-8000-000000000001")
OTHER_OWNER_ID = UUID("018f0000-0000-7000-8000-000000000002")


def _budget(index: int) -> BudgetSnapshot:
    budget_id = UUID(f"018f0000-0000-7000-8000-{index:012d}")
    recorded_at = datetime(2026, 8, 1, tzinfo=UTC)
    return BudgetSnapshot(
        budget_id=budget_id,
        owner_id=OWNER_ID,
        definition=BudgetDefinition(
            name=f"Budget {index}",
            limit_minor=1_000,
            currency="RUB",
            starts_on=date(2026, 8, index),
            ends_on=date(2026, 8, 31),
            timezone="UTC",
        ),
        version=1,
        created_at=recorded_at,
        updated_at=recorded_at,
    )


class _BudgetReader:
    def __init__(self, rows: tuple[BudgetCursorItem, ...]) -> None:
        self.rows = rows
        self.fetch_limit: int | None = None
        self.aggregate_calls = 0
        self.windows: tuple[BudgetSpendWindow, ...] = ()

    async def get(self, owner_id: UUID, budget_id: UUID) -> BudgetSnapshot | None:
        return next(
            (row.budget for row in self.rows if row.budget.budget_id == budget_id),
            None,
        )

    async def list_after(
        self,
        owner_id: UUID,
        *,
        window_start: date,
        window_end: date,
        deleted: bool,
        cursor: BudgetCursor | None,
        limit: int,
    ) -> tuple[BudgetCursorItem, ...]:
        assert owner_id == OWNER_ID
        assert (window_start, window_end, deleted, cursor) == (
            date(2026, 8, 1),
            date(2026, 8, 31),
            False,
            None,
        )
        self.fetch_limit = limit
        return self.rows[:limit]

    async def spent_minor_for_budgets(
        self,
        owner_id: UUID,
        windows: tuple[BudgetSpendWindow, ...],
    ) -> dict[UUID, int]:
        assert owner_id == OWNER_ID
        self.aggregate_calls += 1
        self.windows = windows
        return {window.budget_id: index * 100 for index, window in enumerate(windows, 1)}


def test_budget_domain_uses_local_half_open_periods_and_integer_progress() -> None:
    start, end = budget_period_bounds(
        date(2026, 3, 29),
        date(2026, 3, 29),
        "Europe/Berlin",
    )

    assert (end - start).total_seconds() == 23 * 60 * 60
    assert calculate_budget_progress(1_000, 1_250).overspent_minor == 250
    assert calculate_budget_progress(1_000, 1_250).progress_bps == 10_000
    with pytest.raises(ValueError, match="границы"):
        validate_budget_period(date.max, date.max)


@pytest.mark.asyncio
async def test_budget_list_fetches_lookahead_then_aggregates_selected_page_once() -> None:
    budgets = tuple(_budget(index) for index in range(1, 4))
    rows = tuple(
        BudgetCursorItem(
            budget=budget,
            cursor=BudgetCursor(
                starts_on=budget.definition.starts_on,
                budget_id=budget.budget_id,
            ),
        )
        for budget in budgets
    )
    reader = _BudgetReader(rows)

    page = await ListBudgetProgress(reader)(
        OWNER_ID,
        window_start=date(2026, 8, 1),
        window_end=date(2026, 8, 31),
        deleted=False,
        cursor=None,
        limit=2,
        as_of=datetime(2026, 8, 14, 12, tzinfo=UTC),
    )

    assert reader.fetch_limit == 3
    assert reader.aggregate_calls == 1
    assert len(reader.windows) == 2
    assert tuple(item.progress.spent_minor for item in page.items) == (100, 200)
    assert page.next_cursor == rows[1].cursor


def test_budget_cursor_is_canonical_and_bound_to_owner_window_and_deleted_filter() -> None:
    codec = BudgetCursorCodec(KEY)
    cursor = BudgetCursor(
        starts_on=date(2026, 8, 1),
        budget_id=UUID("018f0000-0000-7000-8000-000000000003"),
    )
    encoded = codec.encode(
        OWNER_ID,
        cursor,
        window_start=date(2026, 8, 1),
        window_end=date(2026, 8, 31),
        deleted=False,
    )

    assert len(encoded) == BUDGET_CURSOR_LENGTH
    assert (
        codec.decode(
            OWNER_ID,
            encoded,
            window_start=date(2026, 8, 1),
            window_end=date(2026, 8, 31),
            deleted=False,
        )
        == cursor
    )
    with pytest.raises(InvalidBudgetCursorError):
        codec.decode(
            OTHER_OWNER_ID,
            encoded,
            window_start=date(2026, 8, 1),
            window_end=date(2026, 8, 31),
            deleted=False,
        )
    with pytest.raises(InvalidBudgetCursorError):
        codec.decode(
            OWNER_ID,
            encoded,
            window_start=date(2026, 8, 1),
            window_end=date(2026, 8, 31),
            deleted=True,
        )
