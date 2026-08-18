from __future__ import annotations

from datetime import datetime
from uuid import UUID

from finbot.application.dto import CurrencyTotals
from finbot.application.errors import ApplicationValidationError, EntityNotFoundError
from finbot.application.exchange_rates import (
    MAX_EXCHANGE_RATE_SOURCES,
    MAX_EXCHANGE_RATE_VERSION_PAGE_SIZE,
    ConvertedPeriodValuation,
    ExchangeRateCommandRepository,
    ExchangeRateReader,
    PublishManualRateVersionCommand,
    RateSourceSnapshot,
    RateVersionCursor,
    RateVersionPageSnapshot,
    RateVersionSnapshot,
)
from finbot.application.ports import FinanceReader
from finbot.domain.exchange_rates import convert_minor_units


def _owner(owner_id: UUID) -> UUID:
    if not isinstance(owner_id, UUID):
        raise ApplicationValidationError("Владелец курсов не прошёл проверку")
    return owner_id


class ListExchangeRateSources:
    __slots__ = ("_reader",)

    def __init__(self, reader: ExchangeRateReader) -> None:
        self._reader = reader

    async def __call__(self, owner_id: UUID) -> tuple[RateSourceSnapshot, ...]:
        rows = await self._reader.list_sources_bounded(
            _owner(owner_id),
            limit=MAX_EXCHANGE_RATE_SOURCES + 1,
        )
        if len(rows) > MAX_EXCHANGE_RATE_SOURCES:
            raise ApplicationValidationError("Слишком много источников курсов")
        if len({row.source_id for row in rows}) != len(rows):
            raise RuntimeError("Exchange-rate reader returned duplicate sources")
        return rows


class ListExchangeRateVersions:
    __slots__ = ("_reader",)

    def __init__(self, reader: ExchangeRateReader) -> None:
        self._reader = reader

    async def __call__(
        self,
        owner_id: UUID,
        source_id: UUID,
        *,
        cursor: RateVersionCursor | None = None,
        limit: int = 20,
    ) -> RateVersionPageSnapshot:
        safe_owner = _owner(owner_id)
        if not isinstance(source_id, UUID):
            raise ApplicationValidationError("Источник курсов не прошёл проверку")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_EXCHANGE_RATE_VERSION_PAGE_SIZE
        ):
            raise ApplicationValidationError("Размер страницы версий должен быть от 1 до 50")
        if await self._reader.get_source(safe_owner, source_id) is None:
            raise EntityNotFoundError("Источник курсов не найден")
        rows = await self._reader.list_versions_after(
            safe_owner,
            source_id,
            cursor=cursor,
            limit=limit + 1,
        )
        if len(rows) > limit + 1:
            raise RuntimeError("Exchange-rate reader violated the bounded fetch contract")
        selected = rows[:limit]
        next_cursor = selected[-1].cursor if len(rows) > limit else None
        return RateVersionPageSnapshot(
            items=tuple(item.version for item in selected),
            next_cursor=next_cursor,
        )


class GetExchangeRateVersion:
    __slots__ = ("_reader",)

    def __init__(self, reader: ExchangeRateReader) -> None:
        self._reader = reader

    async def __call__(self, owner_id: UUID, version_id: UUID) -> RateVersionSnapshot:
        if not isinstance(version_id, UUID):
            raise ApplicationValidationError("Версия курсов не прошла проверку")
        result = await self._reader.get_version(_owner(owner_id), version_id)
        if result is None:
            raise EntityNotFoundError("Версия курсов не найдена")
        return result


class PublishManualExchangeRateVersion:
    __slots__ = ("_commands",)

    def __init__(self, commands: ExchangeRateCommandRepository) -> None:
        self._commands = commands

    async def __call__(
        self,
        command: PublishManualRateVersionCommand,
    ) -> RateVersionSnapshot:
        _owner(command.owner_id)
        return await self._commands.publish(command)


class ExchangeRateUseCases:
    """Closed command facade used by transactional presentation adapters."""

    __slots__ = ("_publish",)

    def __init__(self, commands: ExchangeRateCommandRepository) -> None:
        self._publish = PublishManualExchangeRateVersion(commands)

    async def publish(
        self,
        command: PublishManualRateVersionCommand,
    ) -> RateVersionSnapshot:
        return await self._publish(command)


class GetConvertedPeriodValuation:
    """Convert currency aggregates with one explicit immutable rate version."""

    __slots__ = ("_finance", "_rates")

    def __init__(self, finance: FinanceReader, rates: ExchangeRateReader) -> None:
        self._finance = finance
        self._rates = rates

    async def __call__(
        self,
        owner_id: UUID,
        version_id: UUID,
        start: datetime,
        end: datetime,
    ) -> ConvertedPeriodValuation:
        safe_owner = _owner(owner_id)
        if start.utcoffset() is None or end.utcoffset() is None or start >= end:
            raise ApplicationValidationError("Период конвертации не прошёл проверку")
        version = await GetExchangeRateVersion(self._rates)(safe_owner, version_id)
        raw_totals = tuple(await self._finance.totals_by_currency(safe_owner, start, end))
        if len(raw_totals) > 32:
            raise ApplicationValidationError("Слишком много валют в отчёте")
        totals = tuple(sorted(raw_totals, key=lambda item: item.currency))
        if len({item.currency for item in totals}) != len(totals):
            raise RuntimeError("Finance reader returned duplicate currency totals")
        entries = {item.entry.source_currency: item.entry for item in version.entries}
        income = 0
        expense = 0
        for total in totals:
            _validate_total(total)
            if total.currency == version.summary.target_currency:
                income += total.income_minor
                expense += total.expense_minor
                continue
            rate = entries.get(total.currency)
            if rate is None:
                raise ApplicationValidationError("Для валюты отчёта не задан курс")
            income += convert_minor_units(total.income_minor, rate)
            expense += convert_minor_units(total.expense_minor, rate)
        return ConvertedPeriodValuation(
            version=version.summary,
            original_totals=totals,
            income_minor=income,
            expense_minor=expense,
            net_minor=income - expense,
        )


def _validate_total(total: CurrencyTotals) -> None:
    if not isinstance(total, CurrencyTotals):
        raise RuntimeError("Finance reader returned an invalid currency total")
    if (
        not isinstance(total.currency, str)
        or len(total.currency) != 3
        or not total.currency.isascii()
        or not total.currency.isalpha()
        or total.currency != total.currency.upper()
    ):
        raise RuntimeError("Finance reader returned an invalid currency")
    for value in (total.income_minor, total.expense_minor):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError("Finance reader returned an invalid currency amount")


__all__ = [
    "ExchangeRateUseCases",
    "GetConvertedPeriodValuation",
    "GetExchangeRateVersion",
    "ListExchangeRateSources",
    "ListExchangeRateVersions",
    "PublishManualExchangeRateVersion",
]
