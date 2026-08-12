from datetime import date, datetime
from zoneinfo import ZoneInfo

from finbot.domain.transactions import (
    month_to_date_bounds,
    period_bounds,
    previous_month_to_date_bounds,
)


def test_period_has_utc_bounds():
    start, end = period_bounds(date(2026, 8, 7), "Europe/Moscow")
    assert start.isoformat().startswith("2026-08-06T21:00")
    assert end > start


def test_previous_month_to_date_uses_same_local_progress() -> None:
    start, end = previous_month_to_date_bounds(
        datetime(2026, 8, 12, 14, 35, tzinfo=ZoneInfo("Europe/Moscow")),
        "Europe/Moscow",
    )
    assert start == datetime(2026, 6, 30, 21, 0, tzinfo=ZoneInfo("UTC"))
    assert end == datetime(2026, 7, 12, 11, 35, tzinfo=ZoneInfo("UTC"))


def test_month_to_date_ends_at_the_same_instant() -> None:
    start, end = month_to_date_bounds(
        datetime(2026, 8, 12, 14, 35, tzinfo=ZoneInfo("Europe/Moscow")),
        "Europe/Moscow",
    )
    assert start == datetime(2026, 7, 31, 21, 0, tzinfo=ZoneInfo("UTC"))
    assert end == datetime(2026, 8, 12, 11, 35, tzinfo=ZoneInfo("UTC"))


def test_previous_month_to_date_clamps_shorter_month() -> None:
    start, end = previous_month_to_date_bounds(
        datetime(2025, 3, 31, 10, 0, tzinfo=ZoneInfo("UTC")), "UTC"
    )
    assert start == datetime(2025, 2, 1, tzinfo=ZoneInfo("UTC"))
    assert end == datetime(2025, 3, 1, tzinfo=ZoneInfo("UTC"))
