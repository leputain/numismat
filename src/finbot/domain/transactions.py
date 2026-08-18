from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from uuid import UUID
from zoneinfo import ZoneInfo

from finbot.domain.money import validate_minor


class TransactionType(StrEnum):
    EXPENSE = "expense"
    INCOME = "income"


@dataclass(frozen=True, slots=True)
class TransactionDraft:
    amount_minor: int = field(repr=False)
    type: TransactionType = field(repr=False)
    occurred_at: datetime | None = field(default=None, repr=False)
    category_hint: str | None = field(default=None, repr=False)
    category_explicit: bool = field(default=False, repr=False)
    account_hint: str | None = field(default=None, repr=False)
    description: str = field(default="", repr=False)
    needs_confirmation: bool = False

    def __post_init__(self) -> None:
        validate_minor(self.amount_minor)
        if self.occurred_at is not None and self.occurred_at.utcoffset() is None:
            raise ValueError("Дата операции должна содержать часовой пояс")
        if len(self.description) > 500:
            raise ValueError("Описание должно быть не длиннее 500 символов")


@dataclass(frozen=True, slots=True)
class TransactionView:
    id: UUID = field(repr=False)
    amount_minor: int = field(repr=False)
    type: TransactionType = field(repr=False)
    category: str = field(repr=False)
    account: str = field(repr=False)
    occurred_at: datetime = field(repr=False)
    description: str = field(repr=False)


def period_bounds(day: date, tz_name: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(tz_name)
    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    end = start + timedelta(days=1)
    return start.astimezone(ZoneInfo("UTC")), end.astimezone(ZoneInfo("UTC"))


def month_bounds(day: date, tz_name: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(tz_name)
    start = datetime(day.year, day.month, 1, tzinfo=zone)
    if day.month == 12:
        end = datetime(day.year + 1, 1, 1, tzinfo=zone)
    else:
        end = datetime(day.year, day.month + 1, 1, tzinfo=zone)
    utc = ZoneInfo("UTC")
    return start.astimezone(utc), end.astimezone(utc)


def previous_month(day: date) -> date:
    if day.month == 1:
        return date(day.year - 1, 12, 1)
    return date(day.year, day.month - 1, 1)


def month_to_date_bounds(moment: datetime, tz_name: str) -> tuple[datetime, datetime]:
    """Return the elapsed part of the current local month."""
    zone = ZoneInfo(tz_name)
    local = moment.astimezone(zone)
    start = datetime(local.year, local.month, 1, tzinfo=zone)
    utc = ZoneInfo("UTC")
    return start.astimezone(utc), local.astimezone(utc)


def previous_month_to_date_bounds(moment: datetime, tz_name: str) -> tuple[datetime, datetime]:
    """Return the comparable elapsed part of the previous local month."""
    zone = ZoneInfo(tz_name)
    local = moment.astimezone(zone)
    previous = previous_month(local.date())
    start = datetime(previous.year, previous.month, 1, tzinfo=zone)
    last_day = monthrange(previous.year, previous.month)[1]
    if local.day > last_day:
        if previous.month == 12:
            end = datetime(previous.year + 1, 1, 1, tzinfo=zone)
        else:
            end = datetime(previous.year, previous.month + 1, 1, tzinfo=zone)
    else:
        end = datetime(
            previous.year,
            previous.month,
            local.day,
            local.hour,
            local.minute,
            local.second,
            local.microsecond,
            tzinfo=zone,
            fold=local.fold,
        )
    utc = ZoneInfo("UTC")
    return start.astimezone(utc), end.astimezone(utc)
