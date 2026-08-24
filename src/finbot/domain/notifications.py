from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from finbot.domain.recurrence import resolve_local_occurrence


class NotificationKind(StrEnum):
    BUDGET_80 = "budget_80"
    BUDGET_100 = "budget_100"
    RECURRING_READY = "recurring_ready"
    WEEKLY_DIGEST = "weekly_digest"


@dataclass(frozen=True, slots=True, repr=False)
class NotificationPreferences:
    """Tenant-owned opt-in switches and owner-local delivery schedule."""

    budget_80_enabled: bool = False
    budget_100_enabled: bool = False
    recurring_ready_enabled: bool = False
    weekly_digest_enabled: bool = False
    quiet_start: time | None = field(default=None, repr=False)
    quiet_end: time | None = field(default=None, repr=False)
    weekly_weekday: int = 0
    weekly_time: time = field(default=time(9, 0), repr=False)

    def __post_init__(self) -> None:
        for enabled in (
            self.budget_80_enabled,
            self.budget_100_enabled,
            self.recurring_ready_enabled,
            self.weekly_digest_enabled,
        ):
            if type(enabled) is not bool:
                raise ValueError("Настройка уведомлений должна быть boolean")
        if (self.quiet_start is None) != (self.quiet_end is None):
            raise ValueError("Начало и конец тихих часов задаются вместе")
        if self.quiet_start is not None and self.quiet_end is not None:
            _validate_local_time(self.quiet_start)
            _validate_local_time(self.quiet_end)
            if self.quiet_start == self.quiet_end:
                raise ValueError("Тихие часы не могут занимать полные сутки")
        if isinstance(self.weekly_weekday, bool) or not isinstance(self.weekly_weekday, int):
            raise ValueError("День еженедельной сводки не прошёл проверку")
        if not 0 <= self.weekly_weekday <= 6:
            raise ValueError("День еженедельной сводки должен быть от 0 до 6")
        _validate_local_time(self.weekly_time)

    def enabled_for(self, kind: NotificationKind) -> bool:
        if not isinstance(kind, NotificationKind):
            return False
        return {
            NotificationKind.BUDGET_80: self.budget_80_enabled,
            NotificationKind.BUDGET_100: self.budget_100_enabled,
            NotificationKind.RECURRING_READY: self.recurring_ready_enabled,
            NotificationKind.WEEKLY_DIGEST: self.weekly_digest_enabled,
        }[kind]


@dataclass(frozen=True, slots=True)
class WeeklyNotificationSlot:
    due_at: datetime
    period_key: str

    def __post_init__(self) -> None:
        if self.due_at.utcoffset() is None:
            raise ValueError("Время слота уведомления должно содержать timezone")
        if not self.period_key.isascii() or len(self.period_key) != 10:
            raise ValueError("Период слота уведомления не прошёл проверку")


def _validate_local_time(value: time) -> None:
    if not isinstance(value, time) or value.tzinfo is not None:
        raise ValueError("Локальное время уведомления не прошло проверку")
    if value.second != 0 or value.microsecond != 0:
        raise ValueError("Время уведомления должно иметь точность до минуты")


def _timezone(value: str) -> ZoneInfo:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("Часовой пояс уведомлений не прошёл проверку")
    try:
        return ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Часовой пояс уведомлений не поддерживается") from exc


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Текущее время уведомлений должно содержать timezone")
    return value.astimezone(UTC)


def quiet_until(
    preferences: NotificationPreferences,
    *,
    timezone: str,
    now: datetime,
) -> datetime | None:
    """Return the quiet-window end in UTC; intervals are local ``[start, end)``."""

    current = _aware_utc(now)
    zone = _timezone(timezone)
    start = preferences.quiet_start
    end = preferences.quiet_end
    if start is None or end is None:
        return None
    local = current.astimezone(zone)
    local_clock = local.time().replace(tzinfo=None)
    if start < end:
        inside = start <= local_clock < end
        end_day = local.date()
    else:
        inside = local_clock >= start or local_clock < end
        end_day = local.date() + timedelta(days=1) if local_clock >= start else local.date()
    if not inside:
        return None
    resolved, _adjusted = resolve_local_occurrence(
        datetime.combine(end_day, end),
        timezone,
    )
    return resolved


def latest_weekly_slot(
    preferences: NotificationPreferences,
    *,
    timezone: str,
    now: datetime,
) -> WeeklyNotificationSlot:
    """Return the latest reached owner-local weekly slot and its ISO-week key."""

    current = _aware_utc(now)
    zone = _timezone(timezone)
    local = current.astimezone(zone)
    candidate_day = local.date() - timedelta(
        days=(local.weekday() - preferences.weekly_weekday) % 7
    )
    due_at, _adjusted = resolve_local_occurrence(
        datetime.combine(candidate_day, preferences.weekly_time),
        timezone,
    )
    if due_at > current:
        candidate_day -= timedelta(days=7)
        due_at, _adjusted = resolve_local_occurrence(
            datetime.combine(candidate_day, preferences.weekly_time),
            timezone,
        )
    monday = candidate_day - timedelta(days=candidate_day.weekday())
    return WeeklyNotificationSlot(due_at=due_at, period_key=date.isoformat(monday))
