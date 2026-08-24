from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from finbot.domain.money import validate_minor

MAX_BUDGET_NAME_LENGTH = 60
MAX_BUDGET_PERIOD_DAYS = 366
BUDGET_PROGRESS_BPS_SCALE = 10_000


class BudgetForecastState(StrEnum):
    ON_TRACK = "on_track"
    WATCH = "watch"
    OVER = "over"


def normalize_budget_name(value: str) -> str:
    """Return a printable, whitespace-normalized budget label."""

    if not isinstance(value, str):
        raise ValueError("Название бюджета должно быть строкой")
    normalized = " ".join(value.strip().split())
    if not normalized:
        raise ValueError("Название бюджета не может быть пустым")
    if len(normalized) > MAX_BUDGET_NAME_LENGTH:
        raise ValueError("Название бюджета должно быть не длиннее 60 символов")
    if not normalized.isprintable():
        raise ValueError("Название бюджета содержит недопустимые символы")
    return normalized


def validate_budget_currency(value: str) -> str:
    if not isinstance(value, str) or len(value) != 3 or not value.isascii():
        raise ValueError("Валюта бюджета должна состоять из трёх латинских букв")
    if not value.isalpha() or value != value.upper():
        raise ValueError("Валюта бюджета должна состоять из трёх заглавных букв")
    return value


def validate_budget_period(starts_on: date, ends_on: date) -> None:
    if not isinstance(starts_on, date) or isinstance(starts_on, datetime):
        raise ValueError("Начало периода бюджета должно быть датой")
    if not isinstance(ends_on, date) or isinstance(ends_on, datetime):
        raise ValueError("Окончание периода бюджета должно быть датой")
    if ends_on == date.max:
        raise ValueError("Окончание периода бюджета вышло за допустимые границы")
    days = (ends_on - starts_on).days + 1
    if not 1 <= days <= MAX_BUDGET_PERIOD_DAYS:
        raise ValueError("Период бюджета должен содержать от 1 до 366 дней")


def budget_period_bounds(
    starts_on: date,
    ends_on: date,
    timezone: str,
) -> tuple[datetime, datetime]:
    """Convert an inclusive owner-local date interval to UTC half-open bounds."""

    validate_budget_period(starts_on, ends_on)
    if not isinstance(timezone, str) or not timezone or len(timezone) > 64:
        raise ValueError("Часовой пояс бюджета не прошёл проверку")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Часовой пояс бюджета не поддерживается") from exc
    start = datetime(starts_on.year, starts_on.month, starts_on.day, tzinfo=zone)
    end_day = ends_on + timedelta(days=1)
    end = datetime(end_day.year, end_day.month, end_day.day, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


@dataclass(frozen=True, slots=True, repr=False)
class BudgetDefinition:
    name: str = field(repr=False)
    limit_minor: int = field(repr=False)
    currency: str = field(repr=False)
    starts_on: date = field(repr=False)
    ends_on: date = field(repr=False)
    timezone: str = field(repr=False)
    category_id: UUID | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        normalized_name = normalize_budget_name(self.name)
        validate_minor(self.limit_minor)
        validate_budget_currency(self.currency)
        budget_period_bounds(self.starts_on, self.ends_on, self.timezone)
        if self.category_id is not None and not isinstance(self.category_id, UUID):
            raise ValueError("Категория бюджета не прошла проверку")
        object.__setattr__(self, "name", normalized_name)

    @property
    def starts_at(self) -> datetime:
        return budget_period_bounds(self.starts_on, self.ends_on, self.timezone)[0]

    @property
    def ends_at(self) -> datetime:
        return budget_period_bounds(self.starts_on, self.ends_on, self.timezone)[1]


@dataclass(frozen=True, slots=True, repr=False)
class BudgetProgress:
    spent_minor: int = field(repr=False)
    remaining_minor: int = field(repr=False)
    overspent_minor: int = field(repr=False)
    progress_bps: int

    def __post_init__(self) -> None:
        for value in (self.spent_minor, self.remaining_minor, self.overspent_minor):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("Значение прогресса бюджета должно быть неотрицательным")
        if not 0 <= self.progress_bps <= BUDGET_PROGRESS_BPS_SCALE:
            raise ValueError("Прогресс бюджета вышел за допустимые границы")
        if self.remaining_minor > 0 and self.overspent_minor > 0:
            raise ValueError("Бюджет не может одновременно иметь остаток и перерасход")


@dataclass(frozen=True, slots=True, repr=False)
class BudgetForecast:
    """Commitment-only forecast; discretionary spending is not extrapolated."""

    known_recurring_minor: int = field(repr=False)
    safe_daily_minor: int = field(repr=False)
    forecast_minor: int = field(repr=False)
    state: BudgetForecastState

    def __post_init__(self) -> None:
        for value in (
            self.known_recurring_minor,
            self.safe_daily_minor,
            self.forecast_minor,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("Значение прогноза бюджета должно быть неотрицательным")
        if not isinstance(self.state, BudgetForecastState):
            raise ValueError("Состояние прогноза бюджета не поддерживается")


def calculate_budget_progress(limit_minor: int, spent_minor: int) -> BudgetProgress:
    validate_minor(limit_minor)
    if isinstance(spent_minor, bool) or not isinstance(spent_minor, int) or spent_minor < 0:
        raise ValueError("Расход бюджета должен быть неотрицательным целым числом")
    remaining = max(limit_minor - spent_minor, 0)
    overspent = max(spent_minor - limit_minor, 0)
    progress_bps = min(
        spent_minor * BUDGET_PROGRESS_BPS_SCALE // limit_minor,
        BUDGET_PROGRESS_BPS_SCALE,
    )
    return BudgetProgress(
        spent_minor=spent_minor,
        remaining_minor=remaining,
        overspent_minor=overspent,
        progress_bps=progress_bps,
    )


def calculate_budget_forecast(
    limit_minor: int,
    spent_minor: int,
    known_recurring_minor: int,
    remaining_days: int,
) -> BudgetForecast:
    """Forecast only committed recurring expenses and floor the safe daily amount.

    ``over`` is reserved for an actual overspend. ``watch`` starts when actual
    spend plus known recurring commitments reaches 80% of the limit. The
    threshold uses integer cross-multiplication, so no money value crosses a
    floating-point boundary.
    """

    validate_minor(limit_minor)
    for value in (spent_minor, known_recurring_minor, remaining_days):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Параметр прогноза бюджета должен быть неотрицательным")
    forecast_minor = spent_minor + known_recurring_minor
    if spent_minor > limit_minor:
        state = BudgetForecastState.OVER
    elif forecast_minor * 100 >= limit_minor * 80:
        state = BudgetForecastState.WATCH
    else:
        state = BudgetForecastState.ON_TRACK
    available_minor = max(limit_minor - forecast_minor, 0)
    return BudgetForecast(
        known_recurring_minor=known_recurring_minor,
        safe_daily_minor=available_minor // remaining_days if remaining_days else 0,
        forecast_minor=forecast_minor,
        state=state,
    )


def remaining_budget_days(
    starts_on: date,
    ends_on: date,
    timezone: str,
    measured_at: datetime,
) -> int:
    """Return owner-local calendar days left, including the current day."""

    budget_period_bounds(starts_on, ends_on, timezone)
    if not isinstance(measured_at, datetime) or measured_at.utcoffset() is None:
        raise ValueError("Время прогноза бюджета должно содержать часовой пояс")
    zone = _timezone_for_budget(timezone)
    local_day = measured_at.astimezone(zone).date()
    if local_day < starts_on:
        return (ends_on - starts_on).days + 1
    if local_day > ends_on:
        return 0
    return (ends_on - local_day).days + 1


def _timezone_for_budget(value: str) -> ZoneInfo:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("Часовой пояс бюджета не прошёл проверку")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Часовой пояс бюджета не поддерживается") from exc
