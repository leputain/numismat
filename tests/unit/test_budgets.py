from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from finbot.adapters.http.budgets.cursor import (
    BUDGET_CURSOR_LENGTH,
    BudgetCursorCodec,
    InvalidBudgetCursorError,
)
from finbot.adapters.http.schemas.budgets import budget_response
from finbot.application.budgets import (
    BudgetCursor,
    BudgetCursorItem,
    BudgetSnapshot,
    BudgetSpendWindow,
)
from finbot.application.use_cases.budgets import ListBudgetProgress
from finbot.domain.budgets import (
    BudgetDefinition,
    BudgetForecastState,
    budget_period_bounds,
    calculate_budget_forecast,
    calculate_budget_progress,
    remaining_budget_days,
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
        self.recurring_calls = 0
        self.windows: tuple[BudgetSpendWindow, ...] = ()
        self.forecast_windows: tuple[BudgetSpendWindow, ...] = ()

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

    async def known_recurring_minor_for_budgets(
        self,
        owner_id: UUID,
        windows: tuple[BudgetSpendWindow, ...],
    ) -> dict[UUID, int]:
        assert owner_id == OWNER_ID
        self.recurring_calls += 1
        self.forecast_windows = windows
        return {window.budget_id: index * 300 for index, window in enumerate(windows, 1)}


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


def test_budget_forecast_is_commitment_only_integer_math_with_explicit_states() -> None:
    on_track = calculate_budget_forecast(1_000, 200, 300, 7)
    below_watch = calculate_budget_forecast(1_000, 499, 300, 3)
    watch = calculate_budget_forecast(1_000, 500, 300, 3)
    over = calculate_budget_forecast(1_000, 1_001, 0, 0)

    assert (on_track.forecast_minor, on_track.safe_daily_minor, on_track.state) == (
        500,
        71,
        BudgetForecastState.ON_TRACK,
    )
    assert (watch.forecast_minor, watch.safe_daily_minor, watch.state) == (
        800,
        66,
        BudgetForecastState.WATCH,
    )
    assert below_watch.state is BudgetForecastState.ON_TRACK
    assert (over.forecast_minor, over.safe_daily_minor, over.state) == (
        1_001,
        0,
        BudgetForecastState.OVER,
    )
    assert (
        remaining_budget_days(
            date(2026, 8, 1),
            date(2026, 8, 31),
            "Europe/Moscow",
            datetime(2026, 8, 14, 21, 30, tzinfo=UTC),
        )
        == 17
    )


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
    assert reader.recurring_calls == 1
    assert len(reader.windows) == 2
    assert len(reader.forecast_windows) == 2
    assert all(
        window.start == datetime(2026, 8, 14, 12, tzinfo=UTC) for window in reader.forecast_windows
    )
    assert tuple(item.progress.spent_minor for item in page.items) == (100, 200)
    assert tuple(item.known_recurring_minor for item in page.items) == (300, 600)
    assert tuple(item.forecast_minor for item in page.items) == (400, 800)
    assert tuple(item.safe_daily_minor for item in page.items) == (33, 11)
    assert tuple(item.state for item in page.items) == (
        BudgetForecastState.ON_TRACK,
        BudgetForecastState.WATCH,
    )
    response = budget_response(page.items[0])
    assert response.progress.known_recurring_minor == "300"
    assert response.progress.safe_daily_minor == "33"
    assert response.progress.forecast_minor == "400"
    assert response.progress.state == "on_track"
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
