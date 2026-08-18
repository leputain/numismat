from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from finbot.domain.money import validate_minor
from finbot.domain.transactions import TransactionType

MAX_RECURRING_NAME_LENGTH = 60
MAX_RECURRING_DESCRIPTION_LENGTH = 500


class RecurrenceCadence(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


_MAX_INTERVAL = {
    RecurrenceCadence.DAILY: 365,
    RecurrenceCadence.WEEKLY: 52,
    RecurrenceCadence.MONTHLY: 24,
}


def normalize_recurring_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Название расписания должно быть строкой")
    normalized = " ".join(value.strip().split())
    if not normalized:
        raise ValueError("Название расписания не может быть пустым")
    if len(normalized) > MAX_RECURRING_NAME_LENGTH:
        raise ValueError("Название расписания должно быть не длиннее 60 символов")
    if not normalized.isprintable():
        raise ValueError("Название расписания содержит недопустимые символы")
    return normalized


def _timezone(value: str) -> ZoneInfo:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("Часовой пояс расписания не прошёл проверку")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Часовой пояс расписания не поддерживается") from exc


@dataclass(frozen=True, slots=True, repr=False)
class RecurrenceRule:
    cadence: RecurrenceCadence
    interval: int
    anchor_date: date = field(repr=False)
    local_time: time = field(repr=False)
    timezone: str = field(repr=False)
    ends_on: date | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.cadence, RecurrenceCadence):
            raise ValueError("Периодичность расписания не поддерживается")
        if (
            isinstance(self.interval, bool)
            or not isinstance(self.interval, int)
            or not 1 <= self.interval <= _MAX_INTERVAL[self.cadence]
        ):
            raise ValueError("Интервал расписания вышел за допустимые границы")
        if not isinstance(self.anchor_date, date) or isinstance(self.anchor_date, datetime):
            raise ValueError("Дата начала расписания не прошла проверку")
        if not isinstance(self.local_time, time) or self.local_time.tzinfo is not None:
            raise ValueError("Локальное время расписания не прошло проверку")
        if self.local_time.second != 0 or self.local_time.microsecond != 0:
            raise ValueError("Время расписания должно иметь точность до минуты")
        if self.ends_on is not None:
            if not isinstance(self.ends_on, date) or isinstance(self.ends_on, datetime):
                raise ValueError("Дата окончания расписания не прошла проверку")
            if self.ends_on < self.anchor_date:
                raise ValueError("Дата окончания расписания раньше даты начала")
        _timezone(self.timezone)


@dataclass(frozen=True, slots=True, repr=False)
class RecurringTransactionDefinition:
    name: str = field(repr=False)
    kind: TransactionType = field(repr=False)
    amount_minor: int = field(repr=False)
    currency: str = field(repr=False)
    account_id: UUID = field(repr=False)
    category_id: UUID = field(repr=False)
    recurrence: RecurrenceRule = field(repr=False)
    description: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", normalize_recurring_name(self.name))
        if not isinstance(self.kind, TransactionType):
            raise ValueError("Тип операции расписания не поддерживается")
        validate_minor(self.amount_minor)
        if (
            not isinstance(self.currency, str)
            or len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isalpha()
            or self.currency != self.currency.upper()
        ):
            raise ValueError("Валюта расписания должна состоять из трёх заглавных букв")
        if not isinstance(self.account_id, UUID) or not isinstance(self.category_id, UUID):
            raise ValueError("Справочник расписания не прошёл проверку")
        if not isinstance(self.description, str):
            raise ValueError("Описание расписания должно быть строкой")
        if len(self.description) > MAX_RECURRING_DESCRIPTION_LENGTH:
            raise ValueError("Описание расписания должно быть не длиннее 500 символов")
        if not self.description.isprintable() and self.description:
            raise ValueError("Описание расписания содержит недопустимые символы")
        if not isinstance(self.recurrence, RecurrenceRule):
            raise ValueError("Правило расписания не прошло проверку")


@dataclass(frozen=True, slots=True, repr=False)
class RecurrenceOccurrence:
    index: int
    nominal_local: datetime = field(repr=False)
    scheduled_for: datetime = field(repr=False)
    dst_adjusted: bool

    def __post_init__(self) -> None:
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            raise ValueError("Индекс occurrence должен быть неотрицательным")
        if self.nominal_local.tzinfo is not None:
            raise ValueError("Номинальное локальное время не должно содержать timezone")
        if self.scheduled_for.utcoffset() is None:
            raise ValueError("Фактическое время occurrence должно содержать timezone")
        if type(self.dst_adjusted) is not bool:
            raise ValueError("Признак DST adjustment должен быть boolean")


def _add_months(anchor: date, months: int) -> date:
    absolute_month = anchor.year * 12 + anchor.month - 1 + months
    year, zero_based_month = divmod(absolute_month, 12)
    if not 1 <= year <= 9999:
        raise ValueError("Дата occurrence вышла за поддерживаемый диапазон")
    month = zero_based_month + 1
    day = min(anchor.day, monthrange(year, month)[1])
    return date(year, month, day)


def occurrence_date(rule: RecurrenceRule, index: int) -> date | None:
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError("Индекс occurrence должен быть неотрицательным")
    try:
        if rule.cadence is RecurrenceCadence.DAILY:
            result = rule.anchor_date + timedelta(days=index * rule.interval)
        elif rule.cadence is RecurrenceCadence.WEEKLY:
            result = rule.anchor_date + timedelta(days=index * rule.interval * 7)
        else:
            result = _add_months(rule.anchor_date, index * rule.interval)
    except OverflowError as exc:
        raise ValueError("Дата occurrence вышла за поддерживаемый диапазон") from exc
    if rule.ends_on is not None and result > rule.ends_on:
        return None
    return result


def _roundtrip_local(candidate: datetime, zone: ZoneInfo) -> datetime:
    return candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)


def resolve_local_occurrence(nominal: datetime, timezone: str) -> tuple[datetime, bool]:
    """Resolve an owner-local wall time, choosing fold=0 and shifting DST gaps forward."""

    if nominal.tzinfo is not None:
        raise ValueError("Номинальное время occurrence должно быть локальным")
    zone = _timezone(timezone)
    fold_zero = nominal.replace(tzinfo=zone, fold=0)
    if _roundtrip_local(fold_zero, zone) == nominal:
        return fold_zero.astimezone(UTC), False

    fold_one = nominal.replace(tzinfo=zone, fold=1)
    if _roundtrip_local(fold_one, zone) == nominal:
        return fold_one.astimezone(UTC), False

    # For a nonexistent wall time ZoneInfo's two folds round-trip to the two
    # sides of the gap. The first valid local time after the request is the
    # required gap-forward result, including non-hour historical transitions.
    shifted = sorted(
        candidate
        for candidate in (
            _roundtrip_local(fold_zero, zone),
            _roundtrip_local(fold_one, zone),
        )
        if candidate > nominal
    )
    if not shifted:
        raise ValueError("Локальное время occurrence невозможно разрешить")
    # Recurrence input has minute precision. Walk only the bounded missing
    # interval and choose its first representable local minute, rather than
    # preserving the minute offset across the gap (02:30 -> 03:00, not 03:30).
    first_known_valid = shifted[0]
    resolved_local = nominal
    while resolved_local < first_known_valid:
        resolved_local += timedelta(minutes=1)
        resolved = resolved_local.replace(tzinfo=zone, fold=0)
        if _roundtrip_local(resolved, zone) == resolved_local:
            return resolved.astimezone(UTC), True

    raise ValueError("Локальное время occurrence невозможно разрешить")


def recurrence_occurrence(rule: RecurrenceRule, index: int) -> RecurrenceOccurrence | None:
    day = occurrence_date(rule, index)
    if day is None:
        return None
    nominal = datetime.combine(day, rule.local_time)
    scheduled_for, adjusted = resolve_local_occurrence(nominal, rule.timezone)
    return RecurrenceOccurrence(
        index=index,
        nominal_local=nominal,
        scheduled_for=scheduled_for,
        dst_adjusted=adjusted,
    )
