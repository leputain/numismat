from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from finbot.adapters.mcp.ports import McpQueryUnitOfWorkFactory
from finbot.adapters.mcp.schemas import (
    McpAccountCatalog,
    McpCategoryCatalog,
    McpDashboard,
    McpPeriodReport,
    McpTransaction,
    McpTransactionPage,
    account,
    category,
    dashboard,
    period_report,
    transaction,
    transaction_page,
)
from finbot.application.errors import ApplicationValidationError
from finbot.application.use_cases.catalogs import ListBoundedAccounts, ListBoundedCategories
from finbot.application.use_cases.queries import (
    GetDashboard,
    GetOwnerSettings,
    GetPeriodReport,
    GetTransaction,
    ListTransactions,
)
from finbot.domain.transactions import (
    TransactionType,
    month_to_date_bounds,
    period_bounds,
    previous_month_to_date_bounds,
)

MAX_MCP_TRANSACTION_PAGE_SIZE = 50
MAX_MCP_TRANSACTION_PAGE = 200
MAX_MCP_PERIOD_DAYS = 366


def _aware_utc_now() -> datetime:
    return datetime.now(UTC)


def _canonical_uuid(value: str) -> UUID:
    if type(value) is not str or len(value) != 36:
        raise ApplicationValidationError("Идентификатор операции не прошёл проверку")
    try:
        parsed = UUID(value)
    except ValueError:
        raise ApplicationValidationError("Идентификатор операции не прошёл проверку") from None
    if str(parsed) != value:
        raise ApplicationValidationError("Идентификатор операции не прошёл проверку")
    return parsed


def _canonical_date(value: str, *, field: str) -> date:
    if type(value) is not str or len(value) != 10:
        raise ApplicationValidationError(f"{field} не прошла проверку")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ApplicationValidationError(f"{field} не прошла проверку") from None
    if parsed.isoformat() != value:
        raise ApplicationValidationError(f"{field} не прошла проверку")
    return parsed


class McpFinanceQueryService:
    """Bounded, owner-fixed queries used by the local stdio MCP surface."""

    __slots__ = ("_clock", "_uow_factory")

    def __init__(
        self,
        uow_factory: McpQueryUnitOfWorkFactory,
        *,
        clock: Callable[[], datetime] = _aware_utc_now,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError("MCP clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    async def dashboard(self) -> McpDashboard:
        now = self._now()
        async with self._uow_factory() as uow:
            owner = await GetOwnerSettings(uow.owners)(uow.owner_id)
            start, end = month_to_date_bounds(now, owner.timezone)
            previous_start, previous_end = previous_month_to_date_bounds(now, owner.timezone)
            value = await GetDashboard(uow.finance)(
                uow.owner_id,
                start,
                end,
                previous_start,
                previous_end,
                category_limit=5,
                recent_limit=8,
            )
            return dashboard(value)

    async def report(self, start: str, end: str) -> McpPeriodReport:
        first_day = _canonical_date(start, field="Дата начала")
        last_day = _canonical_date(end, field="Дата окончания")
        if first_day > last_day or (last_day - first_day).days + 1 > MAX_MCP_PERIOD_DAYS:
            raise ApplicationValidationError("Период должен содержать от 1 до 366 дней")
        async with self._uow_factory() as uow:
            owner = await GetOwnerSettings(uow.owners)(uow.owner_id)
            period_start, _ = period_bounds(first_day, owner.timezone)
            _, period_end = period_bounds(last_day, owner.timezone)
            value = await GetPeriodReport(uow.finance)(
                uow.owner_id,
                period_start,
                period_end,
                category_limit=20,
                transaction_limit=50,
            )
            return period_report(value)

    async def list_transactions(
        self,
        *,
        page: int = 0,
        page_size: int = 20,
        deleted: bool = False,
    ) -> McpTransactionPage:
        if type(page) is not int or not 0 <= page <= MAX_MCP_TRANSACTION_PAGE:
            raise ApplicationValidationError(
                f"Номер страницы должен быть от 0 до {MAX_MCP_TRANSACTION_PAGE}"
            )
        if type(page_size) is not int or not 1 <= page_size <= MAX_MCP_TRANSACTION_PAGE_SIZE:
            raise ApplicationValidationError("Размер страницы должен быть от 1 до 50")
        if type(deleted) is not bool:
            raise ApplicationValidationError("Признак корзины не прошёл проверку")
        async with self._uow_factory() as uow:
            value = await ListTransactions(uow.finance)(
                uow.owner_id,
                page=page,
                page_size=page_size,
                deleted=deleted,
            )
            return transaction_page(value)

    async def get_transaction(self, transaction_id: str) -> McpTransaction:
        identifier = _canonical_uuid(transaction_id)
        async with self._uow_factory() as uow:
            value = await GetTransaction(uow.finance)(uow.owner_id, identifier)
            return transaction(value)

    async def list_accounts(self, *, archived: bool = False) -> McpAccountCatalog:
        async with self._uow_factory() as uow:
            owner = await GetOwnerSettings(uow.owners)(uow.owner_id)
            values = await ListBoundedAccounts(uow.catalogs)(uow.owner_id, archived=archived)
            return McpAccountCatalog(
                default_account_id=owner.default_account_id,
                items=tuple(account(item) for item in values),
            )

    async def list_categories(
        self,
        *,
        kind: Literal["expense", "income"] | None = None,
        archived: bool = False,
    ) -> McpCategoryCatalog:
        normalized_kind = TransactionType(kind) if kind is not None else None
        async with self._uow_factory() as uow:
            values = await ListBoundedCategories(uow.catalogs)(
                uow.owner_id,
                kind=normalized_kind,
                archived=archived,
            )
            return McpCategoryCatalog(items=tuple(category(item) for item in values))


__all__ = [
    "MAX_MCP_PERIOD_DAYS",
    "MAX_MCP_TRANSACTION_PAGE",
    "MAX_MCP_TRANSACTION_PAGE_SIZE",
    "McpFinanceQueryService",
]
