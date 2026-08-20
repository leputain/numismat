from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from finbot.adapters.http.schemas.common import ApiModel
from finbot.application.dto import (
    CategoryTotalSnapshot,
    CurrencyTotals,
    DashboardSnapshot,
    PeriodComparisonSnapshot,
    PeriodReportSnapshot,
    TimeSeriesCurrencyTotals,
    TimeSeriesSnapshot,
    TransactionSnapshot,
)

NonNegativeMinor = Annotated[
    str,
    Field(pattern=r"^(?:0|[1-9][0-9]{0,63})$", min_length=1, max_length=64, repr=False),
]
PositiveMinor = Annotated[
    str,
    Field(pattern=r"^[1-9][0-9]{0,63}$", min_length=1, max_length=64, repr=False),
]
SignedMinor = Annotated[
    str,
    Field(pattern=r"^-?(?:0|[1-9][0-9]{0,63})$", min_length=1, max_length=65, repr=False),
]


class CurrencyTotalsResponse(ApiModel):
    currency: str = Field(pattern=r"^[A-Z]{3}$", repr=False)
    income_minor: NonNegativeMinor
    expense_minor: NonNegativeMinor
    net_minor: SignedMinor


class CategoryTotalResponse(ApiModel):
    category_id: UUID = Field(repr=False)
    name: str = Field(min_length=1, max_length=100, repr=False)
    emoji: str = Field(max_length=8, repr=False)
    currency: str = Field(pattern=r"^[A-Z]{3}$", repr=False)
    amount_minor: PositiveMinor


class AccountSummaryResponse(ApiModel):
    id: UUID = Field(repr=False)
    name: str = Field(min_length=1, max_length=100, repr=False)


class CategorySummaryResponse(ApiModel):
    id: UUID = Field(repr=False)
    name: str = Field(min_length=1, max_length=100, repr=False)
    emoji: str = Field(max_length=8, repr=False)


class TransactionResponse(ApiModel):
    id: UUID = Field(repr=False)
    type: Literal["expense", "income"] = Field(repr=False)
    amount_minor: PositiveMinor
    currency: str = Field(pattern=r"^[A-Z]{3}$", repr=False)
    account: AccountSummaryResponse = Field(repr=False)
    category: CategorySummaryResponse = Field(repr=False)
    occurred_at: datetime = Field(repr=False)
    description: str = Field(max_length=500, repr=False)
    source: str = Field(min_length=1, max_length=20, repr=False)
    deleted_at: datetime | None = Field(repr=False)
    version: int = Field(ge=1, le=2**31 - 1, repr=False)


class PeriodTotalsResponse(ApiModel):
    start: datetime = Field(repr=False)
    end: datetime = Field(repr=False)
    totals: tuple[CurrencyTotalsResponse, ...] = Field(max_length=32, repr=False)


class DashboardResponse(ApiModel):
    current_period: PeriodTotalsResponse = Field(repr=False)
    comparable_period: PeriodTotalsResponse = Field(repr=False)
    top_categories: tuple[CategoryTotalResponse, ...] = Field(max_length=160, repr=False)
    recent_transactions: tuple[TransactionResponse, ...] = Field(max_length=8, repr=False)


class PeriodReportResponse(ApiModel):
    period: PeriodTotalsResponse = Field(repr=False)
    top_categories: tuple[CategoryTotalResponse, ...] = Field(max_length=3200, repr=False)
    transactions: tuple[TransactionResponse, ...] = Field(max_length=100, repr=False)


class PeriodComparisonResponse(ApiModel):
    current_period: PeriodTotalsResponse = Field(repr=False)
    comparable_period: PeriodTotalsResponse = Field(repr=False)


class TimeSeriesPeriodResponse(ApiModel):
    start: datetime = Field(repr=False)
    end: datetime = Field(repr=False)


class TimeSeriesCurrencyTotalsResponse(ApiModel):
    currency: str = Field(pattern=r"^[A-Z]{3}$", repr=False)
    income_minor: NonNegativeMinor
    expense_minor: NonNegativeMinor
    net_minor: SignedMinor
    income_count: int = Field(ge=0, le=2**63 - 1, repr=False)
    expense_count: int = Field(ge=0, le=2**63 - 1, repr=False)


class TimeSeriesBucketResponse(ApiModel):
    start: datetime = Field(repr=False)
    end: datetime = Field(repr=False)
    totals: tuple[TimeSeriesCurrencyTotalsResponse, ...] = Field(max_length=32, repr=False)


class TimeSeriesResponse(ApiModel):
    period: TimeSeriesPeriodResponse = Field(repr=False)
    timezone: str = Field(min_length=1, max_length=64, repr=False)
    grain: Literal["day", "week", "month"]
    buckets: tuple[TimeSeriesBucketResponse, ...] = Field(max_length=366, repr=False)


class TransactionPageResponse(ApiModel):
    items: tuple[TransactionResponse, ...] = Field(max_length=100, repr=False)
    next_cursor: str | None = Field(min_length=76, max_length=76, repr=False)


def currency_totals_response(value: CurrencyTotals) -> CurrencyTotalsResponse:
    return CurrencyTotalsResponse(
        currency=value.currency,
        income_minor=str(value.income_minor),
        expense_minor=str(value.expense_minor),
        net_minor=str(value.net_minor),
    )


def category_total_response(value: CategoryTotalSnapshot) -> CategoryTotalResponse:
    return CategoryTotalResponse(
        category_id=value.category_id,
        name=value.name,
        emoji=value.emoji,
        currency=value.currency,
        amount_minor=str(value.amount_minor),
    )


def transaction_response(value: TransactionSnapshot) -> TransactionResponse:
    return TransactionResponse(
        id=value.transaction_id,
        type=value.kind.value,
        amount_minor=str(value.amount_minor),
        currency=value.currency,
        account=AccountSummaryResponse(id=value.account_id, name=value.account_name),
        category=CategorySummaryResponse(
            id=value.category_id,
            name=value.category_name,
            emoji=value.category_emoji,
        ),
        occurred_at=value.occurred_at,
        description=value.description,
        source=value.source,
        deleted_at=value.deleted_at,
        version=value.version,
    )


def dashboard_response(value: DashboardSnapshot) -> DashboardResponse:
    return DashboardResponse(
        current_period=PeriodTotalsResponse(
            start=value.start,
            end=value.end,
            totals=tuple(currency_totals_response(item) for item in value.totals),
        ),
        comparable_period=PeriodTotalsResponse(
            start=value.previous_start,
            end=value.previous_end,
            totals=tuple(currency_totals_response(item) for item in value.previous_totals),
        ),
        top_categories=tuple(category_total_response(item) for item in value.top_categories),
        recent_transactions=tuple(transaction_response(item) for item in value.recent_transactions),
    )


def period_report_response(value: PeriodReportSnapshot) -> PeriodReportResponse:
    return PeriodReportResponse(
        period=PeriodTotalsResponse(
            start=value.start,
            end=value.end,
            totals=tuple(currency_totals_response(item) for item in value.totals),
        ),
        top_categories=tuple(category_total_response(item) for item in value.category_totals),
        transactions=tuple(transaction_response(item) for item in value.transactions),
    )


def comparison_response(value: PeriodComparisonSnapshot) -> PeriodComparisonResponse:
    return PeriodComparisonResponse(
        current_period=PeriodTotalsResponse(
            start=value.current_start,
            end=value.current_end,
            totals=tuple(currency_totals_response(item) for item in value.current_totals),
        ),
        comparable_period=PeriodTotalsResponse(
            start=value.previous_start,
            end=value.previous_end,
            totals=tuple(currency_totals_response(item) for item in value.previous_totals),
        ),
    )


def timeseries_response(value: TimeSeriesSnapshot) -> TimeSeriesResponse:
    def totals_response(item: TimeSeriesCurrencyTotals) -> TimeSeriesCurrencyTotalsResponse:
        return TimeSeriesCurrencyTotalsResponse(
            currency=item.currency,
            income_minor=str(item.income_minor),
            expense_minor=str(item.expense_minor),
            net_minor=str(item.net_minor),
            income_count=item.income_count,
            expense_count=item.expense_count,
        )

    return TimeSeriesResponse(
        period=TimeSeriesPeriodResponse(start=value.start, end=value.end),
        timezone=value.timezone,
        grain=value.grain.value,
        buckets=tuple(
            TimeSeriesBucketResponse(
                start=bucket.start,
                end=bucket.end,
                totals=tuple(totals_response(item) for item in bucket.totals),
            )
            for bucket in value.buckets
        ),
    )
