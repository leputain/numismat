from collections import Counter
from datetime import datetime
from uuid import UUID

from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    CategoryTotalSnapshot,
    CurrencyTotals,
    DashboardSnapshot,
    DeletedTransactionCursor,
    DeletedTransactionCursorPageSnapshot,
    OwnerSnapshot,
    PeriodComparisonSnapshot,
    PeriodReportSnapshot,
    TransactionCursor,
    TransactionCursorPageSnapshot,
    TransactionPageSnapshot,
    TransactionSnapshot,
)
from finbot.application.errors import ApplicationValidationError, EntityNotFoundError
from finbot.application.ports import CatalogReader, FinanceReader, OwnerReader
from finbot.domain.transactions import TransactionType

_MAX_QUERY_LIMIT = 100
_MAX_RESPONSE_CURRENCIES = 32
_MAX_CATEGORY_TOTAL_ROWS = _MAX_RESPONSE_CURRENCIES * _MAX_QUERY_LIMIT


def _validated_limit(value: int, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApplicationValidationError(f"{field} должен быть целым числом")
    if not 1 <= value <= _MAX_QUERY_LIMIT:
        raise ApplicationValidationError(f"{field} должен быть от 1 до {_MAX_QUERY_LIMIT}")
    return value


def _validated_page(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ApplicationValidationError("Номер страницы должен быть целым неотрицательным числом")
    return value


def _validate_period(start: datetime, end: datetime, *, field: str) -> None:
    if start.utcoffset() is None or end.utcoffset() is None:
        raise ApplicationValidationError(f"Границы периода {field} должны содержать часовой пояс")
    if start >= end:
        raise ApplicationValidationError(f"Начало периода {field} должно предшествовать окончанию")


def _bounded_currency_totals(
    values: tuple[CurrencyTotals, ...] | list[CurrencyTotals],
) -> tuple[CurrencyTotals, ...]:
    result = tuple(values)
    if len(result) > _MAX_RESPONSE_CURRENCIES or len({item.currency for item in result}) != len(
        result
    ):
        raise ApplicationValidationError("Слишком много валют в результате")
    return result


def _bounded_category_totals(
    values: tuple[CategoryTotalSnapshot, ...] | list[CategoryTotalSnapshot],
    *,
    per_currency_limit: int,
) -> tuple[CategoryTotalSnapshot, ...]:
    result = tuple(values)
    currencies = {item.currency for item in result}
    per_currency = Counter(item.currency for item in result)
    if (
        len(currencies) > _MAX_RESPONSE_CURRENCIES
        or len(result)
        > min(
            _MAX_CATEGORY_TOTAL_ROWS,
            _MAX_RESPONSE_CURRENCIES * per_currency_limit,
        )
        or any(count > per_currency_limit for count in per_currency.values())
    ):
        raise ApplicationValidationError("Слишком много валют в результате")
    return result


class GetTransaction:
    __slots__ = ("_reader",)

    def __init__(self, reader: FinanceReader) -> None:
        self._reader = reader

    async def __call__(self, owner_id: UUID, transaction_id: UUID) -> TransactionSnapshot:
        transaction = await self._reader.get_transaction(owner_id, transaction_id)
        if transaction is None:
            raise EntityNotFoundError("Операция не найдена")
        return transaction


class ListTransactions:
    __slots__ = ("_reader",)

    def __init__(self, reader: FinanceReader) -> None:
        self._reader = reader

    async def __call__(
        self,
        owner_id: UUID,
        *,
        page: int = 0,
        page_size: int = 20,
        deleted: bool = False,
    ) -> TransactionPageSnapshot:
        safe_page = _validated_page(page)
        safe_page_size = _validated_limit(page_size, field="Размер страницы")
        items, total = await self._reader.list_transactions(
            owner_id,
            page=safe_page,
            page_size=safe_page_size,
            deleted=deleted,
        )
        return TransactionPageSnapshot(
            items=tuple(items),
            page=safe_page,
            page_size=safe_page_size,
            total=total,
        )


class ListTransactionsByCursor:
    __slots__ = ("_reader",)

    def __init__(self, reader: FinanceReader) -> None:
        self._reader = reader

    async def __call__(
        self,
        owner_id: UUID,
        *,
        cursor: TransactionCursor | None = None,
        limit: int = 20,
    ) -> TransactionCursorPageSnapshot:
        safe_limit = _validated_limit(limit, field="Размер страницы")
        fetched = await self._reader.list_transactions_after(
            owner_id,
            cursor=cursor,
            limit=safe_limit + 1,
        )
        return TransactionCursorPageSnapshot(
            items=tuple(fetched[:safe_limit]),
            has_more=len(fetched) > safe_limit,
        )


class ListDeletedTransactionsByCursor:
    __slots__ = ("_reader",)

    def __init__(self, reader: FinanceReader) -> None:
        self._reader = reader

    async def __call__(
        self,
        owner_id: UUID,
        *,
        cursor: DeletedTransactionCursor | None = None,
        limit: int = 20,
    ) -> DeletedTransactionCursorPageSnapshot:
        safe_limit = _validated_limit(limit, field="Размер страницы")
        fetched = await self._reader.list_deleted_transactions_after(
            owner_id,
            cursor=cursor,
            limit=safe_limit + 1,
        )
        return DeletedTransactionCursorPageSnapshot(
            items=tuple(fetched[:safe_limit]),
            has_more=len(fetched) > safe_limit,
        )


class ListAccounts:
    __slots__ = ("_reader",)

    def __init__(self, reader: CatalogReader) -> None:
        self._reader = reader

    async def __call__(
        self, owner_id: UUID, *, archived: bool = False
    ) -> tuple[AccountSnapshot, ...]:
        return tuple(await self._reader.list_accounts(owner_id, archived=archived))


class ListCategories:
    __slots__ = ("_reader",)

    def __init__(self, reader: CatalogReader) -> None:
        self._reader = reader

    async def __call__(
        self,
        owner_id: UUID,
        *,
        kind: TransactionType | str | None = None,
        archived: bool = False,
    ) -> tuple[CategorySnapshot, ...]:
        normalized_kind: str | None = None
        if kind is not None:
            try:
                normalized_kind = TransactionType(str(kind)).value
            except ValueError as error:
                raise ApplicationValidationError("Неизвестный тип категории") from error
        return tuple(
            await self._reader.list_categories(
                owner_id,
                kind=normalized_kind,
                archived=archived,
            )
        )


class GetOwnerSettings:
    __slots__ = ("_reader",)

    def __init__(self, reader: OwnerReader) -> None:
        self._reader = reader

    async def __call__(self, owner_id: UUID) -> OwnerSnapshot:
        owner = await self._reader.get_owner(owner_id)
        if owner is None:
            raise EntityNotFoundError("Владелец не найден")
        return owner


class GetPeriodReport:
    __slots__ = ("_reader",)

    def __init__(self, reader: FinanceReader) -> None:
        self._reader = reader

    async def __call__(
        self,
        owner_id: UUID,
        start: datetime,
        end: datetime,
        *,
        category_limit: int = 20,
        transaction_limit: int = 20,
    ) -> PeriodReportSnapshot:
        _validate_period(start, end, field="отчёта")
        safe_category_limit = _validated_limit(category_limit, field="Лимит категорий")
        safe_transaction_limit = _validated_limit(transaction_limit, field="Лимит операций")
        totals = _bounded_currency_totals(
            await self._reader.totals_by_currency(owner_id, start, end)
        )
        category_totals = _bounded_category_totals(
            await self._reader.category_totals(owner_id, start, end, limit=safe_category_limit),
            per_currency_limit=safe_category_limit,
        )
        transactions = await self._reader.period_transactions(
            owner_id,
            start,
            end,
            limit=safe_transaction_limit,
        )
        return PeriodReportSnapshot(
            start=start,
            end=end,
            totals=totals,
            category_totals=category_totals,
            transactions=tuple(transactions),
        )


class ComparePeriods:
    __slots__ = ("_reader",)

    def __init__(self, reader: FinanceReader) -> None:
        self._reader = reader

    async def __call__(
        self,
        owner_id: UUID,
        current_start: datetime,
        current_end: datetime,
        previous_start: datetime,
        previous_end: datetime,
    ) -> PeriodComparisonSnapshot:
        _validate_period(current_start, current_end, field="текущего отчёта")
        _validate_period(previous_start, previous_end, field="сравнения")
        current_totals = _bounded_currency_totals(
            await self._reader.totals_by_currency(owner_id, current_start, current_end)
        )
        previous_totals = _bounded_currency_totals(
            await self._reader.totals_by_currency(owner_id, previous_start, previous_end)
        )
        return PeriodComparisonSnapshot(
            current_start=current_start,
            current_end=current_end,
            previous_start=previous_start,
            previous_end=previous_end,
            current_totals=current_totals,
            previous_totals=previous_totals,
        )


class GetDashboard:
    __slots__ = ("_reader",)

    def __init__(self, reader: FinanceReader) -> None:
        self._reader = reader

    async def __call__(
        self,
        owner_id: UUID,
        start: datetime,
        end: datetime,
        previous_start: datetime,
        previous_end: datetime,
        *,
        category_limit: int = 5,
        recent_limit: int = 8,
    ) -> DashboardSnapshot:
        safe_category_limit = _validated_limit(category_limit, field="Лимит категорий")
        safe_recent_limit = _validated_limit(recent_limit, field="Лимит операций")
        comparison = await ComparePeriods(self._reader)(
            owner_id,
            start,
            end,
            previous_start,
            previous_end,
        )
        category_totals = _bounded_category_totals(
            await self._reader.category_totals(owner_id, start, end, limit=safe_category_limit),
            per_currency_limit=safe_category_limit,
        )
        recent_transactions = await self._reader.period_transactions(
            owner_id,
            start,
            end,
            limit=safe_recent_limit,
        )
        return DashboardSnapshot(
            start=start,
            end=end,
            previous_start=previous_start,
            previous_end=previous_end,
            totals=comparison.current_totals,
            previous_totals=comparison.previous_totals,
            top_categories=category_totals,
            recent_transactions=tuple(recent_transactions),
        )
