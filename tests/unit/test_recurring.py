from datetime import UTC, date, datetime, time
from uuid import UUID

import pytest

from finbot.adapters.http.recurring.cursor import (
    RECURRING_CURSOR_LENGTH,
    InvalidRecurringCursorError,
    RecurringCursorCodec,
)
from finbot.application.draft_views import PublicDraftFlow, project_public_draft
from finbot.application.dto import DraftSnapshot
from finbot.application.recurring import RecurringInstanceCursor, RecurringScheduleCursor
from finbot.domain.recurrence import (
    RecurrenceCadence,
    RecurrenceRule,
    occurrence_date,
    recurrence_occurrence,
)

KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA"
OWNER_ID = UUID("018f0000-0000-7000-8000-000000000001")
OTHER_OWNER_ID = UUID("018f0000-0000-7000-8000-000000000002")
SCHEDULE_ID = UUID("018f0000-0000-7000-8000-000000000003")
INSTANCE_ID = UUID("018f0000-0000-7000-8000-000000000004")


def test_monthly_occurrences_clamp_each_month_to_the_anchor_day() -> None:
    rule = RecurrenceRule(
        RecurrenceCadence.MONTHLY,
        1,
        date(2024, 1, 31),
        time(9),
        "UTC",
    )

    assert occurrence_date(rule, 1) == date(2024, 2, 29)
    assert occurrence_date(rule, 2) == date(2024, 3, 31)
    assert occurrence_date(rule, 3) == date(2024, 4, 30)


def test_dst_gap_moves_forward_and_ambiguous_time_uses_fold_zero() -> None:
    gap = recurrence_occurrence(
        RecurrenceRule(
            RecurrenceCadence.DAILY,
            1,
            date(2026, 3, 29),
            time(2, 30),
            "Europe/Berlin",
        ),
        0,
    )
    fold = recurrence_occurrence(
        RecurrenceRule(
            RecurrenceCadence.DAILY,
            1,
            date(2026, 10, 25),
            time(2, 30),
            "Europe/Berlin",
        ),
        0,
    )

    assert gap is not None and gap.dst_adjusted
    assert gap.nominal_local == datetime(2026, 3, 29, 2, 30)
    assert gap.scheduled_for == datetime(2026, 3, 29, 1, tzinfo=UTC)
    assert fold is not None and not fold.dst_adjusted
    assert fold.scheduled_for == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def test_recurring_cursors_are_bounded_and_bound_to_owner_filter_and_schedule() -> None:
    codec = RecurringCursorCodec(KEY)
    recorded_at = datetime(2026, 8, 14, 12, 30, tzinfo=UTC)
    schedule_cursor = RecurringScheduleCursor(recorded_at, SCHEDULE_ID)
    instance_cursor = RecurringInstanceCursor(recorded_at, INSTANCE_ID)

    encoded_schedule = codec.encode_schedule(OWNER_ID, schedule_cursor, deleted=False)
    encoded_instance = codec.encode_instance(OWNER_ID, SCHEDULE_ID, instance_cursor)

    assert len(encoded_schedule) == len(encoded_instance) == RECURRING_CURSOR_LENGTH
    assert codec.decode_schedule(OWNER_ID, encoded_schedule, deleted=False) == schedule_cursor
    assert codec.decode_instance(OWNER_ID, SCHEDULE_ID, encoded_instance) == instance_cursor
    with pytest.raises(InvalidRecurringCursorError):
        codec.decode_schedule(OTHER_OWNER_ID, encoded_schedule, deleted=False)
    with pytest.raises(InvalidRecurringCursorError):
        codec.decode_schedule(OWNER_ID, encoded_schedule, deleted=True)
    with pytest.raises(InvalidRecurringCursorError):
        codec.decode_instance(OWNER_ID, OTHER_OWNER_ID, encoded_instance)


def test_recurring_review_draft_projects_without_exposing_private_payload() -> None:
    draft = DraftSnapshot(
        INSTANCE_ID,
        "review",
        {
            "flow": "recurring",
            "type": "expense",
            "amount_minor": 12_345,
            "currency": "RUB",
            "account_id": str(OWNER_ID),
            "account_name": "Основной",
            "category_id": str(SCHEDULE_ID),
            "category_name": "Аренда",
            "category_emoji": "🏠",
            "occurred_at": "2026-08-14T12:30:00+00:00",
            "description": "Ежемесячный платёж",
            "private_runner_marker": "must-not-project",
        },
    )

    projected = project_public_draft(draft)

    assert projected.flow is PublicDraftFlow.RECURRING
    assert projected.transaction is not None
    assert projected.transaction.amount_minor == 12_345
    assert not hasattr(projected, "private_runner_marker")


@pytest.mark.parametrize(
    ("cadence", "maximum"),
    [
        (RecurrenceCadence.DAILY, 365),
        (RecurrenceCadence.WEEKLY, 52),
        (RecurrenceCadence.MONTHLY, 24),
    ],
)
def test_recurrence_intervals_are_cadence_bounded(
    cadence: RecurrenceCadence,
    maximum: int,
) -> None:
    RecurrenceRule(cadence, maximum, date(2026, 8, 14), time(9), "UTC")
    with pytest.raises(ValueError, match="Интервал"):
        RecurrenceRule(cadence, maximum + 1, date(2026, 8, 14), time(9), "UTC")
