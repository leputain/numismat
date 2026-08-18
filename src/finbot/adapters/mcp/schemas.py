from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    CategoryTotalSnapshot,
    CurrencyTotals,
    DashboardSnapshot,
    PeriodReportSnapshot,
    TransactionPageSnapshot,
    TransactionSnapshot,
)

NonNegativeMinor = Annotated[
    str,
    Field(pattern=r"^(?:0|[1-9][0-9]{0,63})$", min_length=1, max_length=64),
]
PositiveMinor = Annotated[
    str,
    Field(pattern=r"^[1-9][0-9]{0,63}$", min_length=1, max_length=64),
]
SignedMinor = Annotated[
    str,
    Field(pattern=r"^-?(?:0|[1-9][0-9]{0,63})$", min_length=1, max_length=65),
]


class McpModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class McpCurrencyTotals(McpModel):
    currency: str = Field(pattern=r"^[A-Z]{3}$", repr=False)
    income_minor: NonNegativeMinor = Field(repr=False)
    expense_minor: NonNegativeMinor = Field(repr=False)
    net_minor: SignedMinor = Field(repr=False)


class McpCategoryTotal(McpModel):
    category_id: UUID = Field(repr=False)
    name: str = Field(min_length=1, max_length=100, repr=False)
    emoji: str = Field(max_length=8, repr=False)
    currency: str = Field(pattern=r"^[A-Z]{3}$", repr=False)
    amount_minor: PositiveMinor = Field(repr=False)


class McpAccountSummary(McpModel):
    id: UUID = Field(repr=False)
    name: str = Field(min_length=1, max_length=100, repr=False)


class McpCategorySummary(McpModel):
    id: UUID = Field(repr=False)
    name: str = Field(min_length=1, max_length=100, repr=False)
    emoji: str = Field(max_length=8, repr=False)


class McpTransaction(McpModel):
    id: UUID = Field(repr=False)
    type: Literal["expense", "income"]
    amount_minor: PositiveMinor = Field(repr=False)
    currency: str = Field(pattern=r"^[A-Z]{3}$", repr=False)
    account: McpAccountSummary = Field(repr=False)
    category: McpCategorySummary = Field(repr=False)
    occurred_at: datetime = Field(repr=False)
    description: str = Field(max_length=500, repr=False)
    source: str = Field(min_length=1, max_length=20, repr=False)
    deleted_at: datetime | None = Field(repr=False)
    version: int = Field(ge=1, le=2**31 - 1, repr=False)


class McpPeriodTotals(McpModel):
    start: datetime = Field(repr=False)
    end: datetime = Field(repr=False)
    totals: tuple[McpCurrencyTotals, ...] = Field(max_length=32, repr=False)


class McpDashboard(McpModel):
    current_period: McpPeriodTotals = Field(repr=False)
    comparable_period: McpPeriodTotals = Field(repr=False)
    top_categories: tuple[McpCategoryTotal, ...] = Field(max_length=160, repr=False)
    recent_transactions: tuple[McpTransaction, ...] = Field(max_length=8, repr=False)


class McpPeriodReport(McpModel):
    period: McpPeriodTotals = Field(repr=False)
    top_categories: tuple[McpCategoryTotal, ...] = Field(max_length=640, repr=False)
    transactions: tuple[McpTransaction, ...] = Field(max_length=50, repr=False)


class McpTransactionPage(McpModel):
    items: tuple[McpTransaction, ...] = Field(max_length=50, repr=False)
    page: int = Field(ge=0)
    page_size: int = Field(ge=1, le=50)
    total: int = Field(ge=0, repr=False)


class McpAccount(McpModel):
    id: UUID = Field(repr=False)
    name: str = Field(min_length=1, max_length=100, repr=False)
    type: str = Field(min_length=1, max_length=20, repr=False)
    currency: str = Field(pattern=r"^[A-Z]{3}$", repr=False)
    archived: bool
    version: int = Field(ge=1, le=2**31 - 1, repr=False)


class McpAccountCatalog(McpModel):
    default_account_id: UUID | None = Field(repr=False)
    items: tuple[McpAccount, ...] = Field(max_length=200, repr=False)


class McpCategory(McpModel):
    id: UUID = Field(repr=False)
    kind: Literal["expense", "income"]
    name: str = Field(min_length=1, max_length=100, repr=False)
    emoji: str = Field(max_length=8, repr=False)
    archived: bool
    version: int = Field(ge=1, le=2**31 - 1, repr=False)


class McpCategoryCatalog(McpModel):
    items: tuple[McpCategory, ...] = Field(max_length=200, repr=False)


def _totals(value: CurrencyTotals) -> McpCurrencyTotals:
    return McpCurrencyTotals(
        currency=value.currency,
        income_minor=str(value.income_minor),
        expense_minor=str(value.expense_minor),
        net_minor=str(value.net_minor),
    )


def _category_total(value: CategoryTotalSnapshot) -> McpCategoryTotal:
    return McpCategoryTotal(
        category_id=value.category_id,
        name=value.name,
        emoji=value.emoji,
        currency=value.currency,
        amount_minor=str(value.amount_minor),
    )


def transaction(value: TransactionSnapshot) -> McpTransaction:
    return McpTransaction(
        id=value.transaction_id,
        type=value.kind.value,
        amount_minor=str(value.amount_minor),
        currency=value.currency,
        account=McpAccountSummary(id=value.account_id, name=value.account_name),
        category=McpCategorySummary(
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


def dashboard(value: DashboardSnapshot) -> McpDashboard:
    return McpDashboard(
        current_period=McpPeriodTotals(
            start=value.start,
            end=value.end,
            totals=tuple(_totals(item) for item in value.totals),
        ),
        comparable_period=McpPeriodTotals(
            start=value.previous_start,
            end=value.previous_end,
            totals=tuple(_totals(item) for item in value.previous_totals),
        ),
        top_categories=tuple(_category_total(item) for item in value.top_categories),
        recent_transactions=tuple(transaction(item) for item in value.recent_transactions),
    )


def period_report(value: PeriodReportSnapshot) -> McpPeriodReport:
    return McpPeriodReport(
        period=McpPeriodTotals(
            start=value.start,
            end=value.end,
            totals=tuple(_totals(item) for item in value.totals),
        ),
        top_categories=tuple(_category_total(item) for item in value.category_totals),
        transactions=tuple(transaction(item) for item in value.transactions),
    )


def transaction_page(value: TransactionPageSnapshot) -> McpTransactionPage:
    return McpTransactionPage(
        items=tuple(transaction(item) for item in value.items),
        page=value.page,
        page_size=value.page_size,
        total=value.total,
    )


def account(value: AccountSnapshot) -> McpAccount:
    return McpAccount(
        id=value.account_id,
        name=value.name,
        type=value.account_type,
        currency=value.currency,
        archived=value.archived_at is not None,
        version=value.version,
    )


def category(value: CategorySnapshot) -> McpCategory:
    return McpCategory(
        id=value.category_id,
        kind=value.kind.value,
        name=value.name,
        emoji=value.emoji,
        archived=value.archived_at is not None,
        version=value.version,
    )


__all__ = [
    "McpAccountCatalog",
    "McpCategoryCatalog",
    "McpDashboard",
    "McpPeriodReport",
    "McpTransaction",
    "McpTransactionPage",
    "account",
    "category",
    "dashboard",
    "period_report",
    "transaction",
    "transaction_page",
]
