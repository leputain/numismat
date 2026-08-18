from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from finbot.application.dto import CurrencyTotals
from finbot.domain.exchange_rates import (
    MAX_EXCHANGE_RATE_ENTRIES,
    ExchangeRateEntry,
    validate_exchange_currency,
)

MAX_EXCHANGE_RATE_SOURCES = 32
MAX_EXCHANGE_RATE_VERSION_PAGE_SIZE = 50


class ExchangeRateSourceKind(StrEnum):
    MANUAL = "manual"


@dataclass(frozen=True, slots=True, repr=False)
class RateSourceSnapshot:
    source_id: UUID = field(repr=False)
    owner_id: UUID = field(repr=False)
    kind: ExchangeRateSourceKind
    target_currency: str = field(repr=False)
    latest_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, UUID) or not isinstance(self.owner_id, UUID):
            raise ValueError("Exchange-rate source reference is invalid")
        if not isinstance(self.kind, ExchangeRateSourceKind):
            raise ValueError("Exchange-rate source kind is invalid")
        validate_exchange_currency(self.target_currency)
        if (
            isinstance(self.latest_version, bool)
            or not isinstance(self.latest_version, int)
            or not 1 <= self.latest_version <= 2**31 - 1
        ):
            raise ValueError("Exchange-rate source version is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class RateVersionSummary:
    rate_version_id: UUID = field(repr=False)
    source_id: UUID = field(repr=False)
    owner_id: UUID = field(repr=False)
    target_currency: str = field(repr=False)
    version: int
    effective_at: datetime = field(repr=False)
    created_at: datetime = field(repr=False)

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, UUID)
            for value in (self.rate_version_id, self.source_id, self.owner_id)
        ):
            raise ValueError("Exchange-rate version reference is invalid")
        validate_exchange_currency(self.target_currency)
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or not 1 <= self.version <= 2**31 - 1
        ):
            raise ValueError("Exchange-rate version is invalid")
        if not isinstance(self.effective_at, datetime) or not isinstance(self.created_at, datetime):
            raise ValueError("Exchange-rate timestamps are invalid")
        if self.effective_at.utcoffset() is None or self.created_at.utcoffset() is None:
            raise ValueError("Exchange-rate timestamps must contain a timezone")


@dataclass(frozen=True, slots=True, repr=False)
class RateEntrySnapshot:
    rate_version_id: UUID = field(repr=False)
    entry: ExchangeRateEntry = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.rate_version_id, UUID):
            raise ValueError("Exchange-rate entry version id is invalid")
        if not isinstance(self.entry, ExchangeRateEntry):
            raise ValueError("Exchange-rate entry is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class RateVersionSnapshot:
    summary: RateVersionSummary = field(repr=False)
    entries: tuple[RateEntrySnapshot, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.summary, RateVersionSummary):
            raise ValueError("Exchange-rate version summary is invalid")
        if not 1 <= len(self.entries) <= MAX_EXCHANGE_RATE_ENTRIES:
            raise ValueError("Exchange-rate version entry count is invalid")
        currencies: set[str] = set()
        for item in self.entries:
            if not isinstance(item, RateEntrySnapshot):
                raise ValueError("Exchange-rate version entry is invalid")
            if item.rate_version_id != self.summary.rate_version_id:
                raise ValueError("Exchange-rate entry belongs to another version")
            if item.entry.target_currency != self.summary.target_currency:
                raise ValueError("Exchange-rate entry target currency is inconsistent")
            if item.entry.source_currency in currencies:
                raise ValueError("Exchange-rate version contains duplicate currencies")
            currencies.add(item.entry.source_currency)
        if tuple(item.entry.source_currency for item in self.entries) != tuple(sorted(currencies)):
            raise ValueError("Exchange-rate version entries are not canonical")


@dataclass(frozen=True, slots=True, repr=False)
class RateVersionCursor:
    created_at: datetime = field(repr=False)
    rate_version_id: UUID = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.created_at, datetime) or self.created_at.utcoffset() is None:
            raise ValueError("Exchange-rate cursor timestamp must contain a timezone")
        if not isinstance(self.rate_version_id, UUID):
            raise ValueError("Exchange-rate cursor id is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class RateVersionCursorItem:
    version: RateVersionSummary = field(repr=False)
    cursor: RateVersionCursor = field(repr=False)

    def __post_init__(self) -> None:
        if self.version.rate_version_id != self.cursor.rate_version_id:
            raise ValueError("Exchange-rate cursor does not match its item")
        if self.version.created_at != self.cursor.created_at:
            raise ValueError("Exchange-rate cursor timestamp does not match its item")


@dataclass(frozen=True, slots=True, repr=False)
class RateVersionPageSnapshot:
    items: tuple[RateVersionSummary, ...] = field(repr=False)
    next_cursor: RateVersionCursor | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if len(self.items) > MAX_EXCHANGE_RATE_VERSION_PAGE_SIZE:
            raise ValueError("Exchange-rate version page is too large")
        if self.next_cursor is not None and not self.items:
            raise ValueError("Empty exchange-rate page cannot contain a cursor")


@dataclass(frozen=True, slots=True, repr=False)
class PublishManualRateVersionCommand:
    owner_id: UUID = field(repr=False)
    target_currency: str = field(repr=False)
    expected_source_version: int
    effective_at: datetime = field(repr=False)
    entries: tuple[ExchangeRateEntry, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise ValueError("Exchange-rate owner id is invalid")
        validate_exchange_currency(self.target_currency)
        if (
            isinstance(self.expected_source_version, bool)
            or not isinstance(self.expected_source_version, int)
            or not 0 <= self.expected_source_version <= 2**31 - 1
        ):
            raise ValueError("Expected exchange-rate source version is invalid")
        if self.effective_at.utcoffset() is None:
            raise ValueError("Exchange-rate effective timestamp must contain a timezone")
        if not 1 <= len(self.entries) <= MAX_EXCHANGE_RATE_ENTRIES:
            raise ValueError("Exchange-rate version must contain from 1 to 32 rates")
        ordered = tuple(sorted(self.entries, key=lambda item: item.source_currency))
        if any(item.target_currency != self.target_currency for item in ordered):
            raise ValueError("Exchange-rate target currencies do not match")
        if len({item.source_currency for item in ordered}) != len(ordered):
            raise ValueError("Exchange-rate source currencies must be unique")
        object.__setattr__(self, "entries", ordered)


@dataclass(frozen=True, slots=True, repr=False)
class ConvertedPeriodValuation:
    version: RateVersionSummary = field(repr=False)
    original_totals: tuple[CurrencyTotals, ...] = field(repr=False)
    income_minor: int = field(repr=False)
    expense_minor: int = field(repr=False)
    net_minor: int = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.version, RateVersionSummary):
            raise ValueError("Exchange-rate valuation version is invalid")
        if not isinstance(self.original_totals, tuple) or len(self.original_totals) > 32:
            raise ValueError("Original currency totals are invalid")
        for value in (self.income_minor, self.expense_minor):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or len(str(value)) > 64
            ):
                raise ValueError("Converted totals must be non-negative integers")
        if (
            isinstance(self.net_minor, bool)
            or not isinstance(self.net_minor, int)
            or self.net_minor != self.income_minor - self.expense_minor
            or len(str(abs(self.net_minor))) > 64
        ):
            raise ValueError("Converted net does not match income and expense")


class ExchangeRateReader(Protocol):
    async def get_source(
        self,
        owner_id: UUID,
        source_id: UUID,
    ) -> RateSourceSnapshot | None: ...

    async def list_sources_bounded(
        self,
        owner_id: UUID,
        *,
        limit: int,
    ) -> tuple[RateSourceSnapshot, ...]: ...

    async def list_versions_after(
        self,
        owner_id: UUID,
        source_id: UUID,
        *,
        cursor: RateVersionCursor | None,
        limit: int,
    ) -> tuple[RateVersionCursorItem, ...]: ...

    async def get_version(
        self,
        owner_id: UUID,
        rate_version_id: UUID,
    ) -> RateVersionSnapshot | None: ...


class ExchangeRateCommandRepository(Protocol):
    async def publish(
        self,
        command: PublishManualRateVersionCommand,
    ) -> RateVersionSnapshot: ...
