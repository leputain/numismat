from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from finbot.adapters.http.auth.crypto import HttpSecurityDigester
from finbot.adapters.http.auth.service import SessionAuthenticator, SessionCredentials
from finbot.adapters.http.finance.cursor import TransactionCursorCodec
from finbot.adapters.http.finance.ports import FinanceQueryUnitOfWorkFactory
from finbot.application.dto import (
    EMPTY_TRANSACTION_LIST_FILTERS,
    DashboardSnapshot,
    PeriodComparisonSnapshot,
    PeriodReportSnapshot,
    TimeSeriesGrain,
    TimeSeriesSnapshot,
    TransactionListFilters,
    TransactionSnapshot,
)
from finbot.application.use_cases.queries import (
    ComparePeriods,
    GetDashboard,
    GetPeriodReport,
    GetTimeSeries,
    GetTransaction,
    ListDeletedTransactionsByCursor,
    ListTransactionsByCursor,
)
from finbot.domain.transactions import (
    month_to_date_bounds,
    period_bounds,
    previous_month_to_date_bounds,
)

DASHBOARD_CATEGORY_LIMIT = 5
DASHBOARD_RECENT_LIMIT = 8
TODAY_REPORT_LIMIT = 20


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True, repr=False)
class TransactionCursorPage:
    items: tuple[TransactionSnapshot, ...] = field(repr=False)
    next_cursor: str | None = field(default=None, repr=False)


class FinanceQueryService:
    """Authenticated HTTP finance queries over a single database snapshot."""

    __slots__ = ("_authenticator", "_clock", "_cursor_codec", "_uow_factory")

    def __init__(
        self,
        *,
        digester: HttpSecurityDigester,
        cursor_codec: TransactionCursorCodec,
        uow_factory: FinanceQueryUnitOfWorkFactory,
        allowed_telegram_user_ids: frozenset[int] | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._authenticator = SessionAuthenticator(digester, allowed_telegram_user_ids)
        self._cursor_codec = cursor_codec
        self._uow_factory = uow_factory
        self._clock = clock

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("finance query clock must be timezone-aware")
        return value.astimezone(UTC)

    async def dashboard(self, credentials: SessionCredentials) -> DashboardSnapshot:
        now = self._now()
        async with self._uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                credentials,
                now=now,
            )
            owner = authenticated.owner
            start, end = month_to_date_bounds(now, owner.timezone)
            previous_start, previous_end = previous_month_to_date_bounds(
                now,
                owner.timezone,
            )
            return await GetDashboard(uow.finance)(
                owner.owner_id,
                start,
                end,
                previous_start,
                previous_end,
                category_limit=DASHBOARD_CATEGORY_LIMIT,
                recent_limit=DASHBOARD_RECENT_LIMIT,
            )

    async def today_report(self, credentials: SessionCredentials) -> PeriodReportSnapshot:
        now = self._now()
        async with self._uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                credentials,
                now=now,
            )
            owner = authenticated.owner
            local_day = now.astimezone(ZoneInfo(owner.timezone)).date()
            start, end = period_bounds(local_day, owner.timezone)
            return await GetPeriodReport(uow.finance)(
                owner.owner_id,
                start,
                end,
                category_limit=TODAY_REPORT_LIMIT,
                transaction_limit=TODAY_REPORT_LIMIT,
            )

    async def period_report(
        self,
        credentials: SessionCredentials,
        start: datetime,
        end: datetime,
        *,
        category_limit: int,
        transaction_limit: int,
    ) -> PeriodReportSnapshot:
        now = self._now()
        async with self._uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                credentials,
                now=now,
            )
            return await GetPeriodReport(uow.finance)(
                authenticated.owner.owner_id,
                start,
                end,
                category_limit=category_limit,
                transaction_limit=transaction_limit,
            )

    async def compare_periods(
        self,
        credentials: SessionCredentials,
        current_start: datetime,
        current_end: datetime,
        previous_start: datetime,
        previous_end: datetime,
    ) -> PeriodComparisonSnapshot:
        now = self._now()
        async with self._uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                credentials,
                now=now,
            )
            return await ComparePeriods(uow.finance)(
                authenticated.owner.owner_id,
                current_start,
                current_end,
                previous_start,
                previous_end,
            )

    async def timeseries(
        self,
        credentials: SessionCredentials,
        start: datetime,
        end: datetime,
        *,
        grain: TimeSeriesGrain,
    ) -> TimeSeriesSnapshot:
        now = self._now()
        async with self._uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                credentials,
                now=now,
            )
            owner = authenticated.owner
            return await GetTimeSeries(uow.finance)(
                owner.owner_id,
                start,
                end,
                timezone=owner.timezone,
                grain=grain,
            )

    async def transactions(
        self,
        credentials: SessionCredentials,
        *,
        limit: int,
        raw_cursor: str | None,
        filters: TransactionListFilters | None = None,
    ) -> TransactionCursorPage:
        """Read one deterministic page of the current active dataset.

        Cursors are stable for an unchanged dataset. A transaction mutation can move
        an item across the live keyset anchor, so clients must restart pagination
        after a successful Task 14 mutation rather than treating this as a snapshot.
        """

        now = self._now()
        async with self._uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                credentials,
                now=now,
            )
            owner_id = authenticated.owner.owner_id
            active_filters = filters if filters is not None else EMPTY_TRANSACTION_LIST_FILTERS
            cursor = (
                self._cursor_codec.decode(
                    owner_id,
                    raw_cursor,
                    filters=active_filters,
                )
                if raw_cursor is not None
                else None
            )
            page = await ListTransactionsByCursor(uow.finance)(
                owner_id,
                cursor=cursor,
                limit=limit,
                filters=active_filters,
            )
            next_cursor = None
            if page.has_more:
                next_cursor = self._cursor_codec.encode(
                    owner_id,
                    page.items[-1].cursor,
                    filters=active_filters,
                )
            return TransactionCursorPage(
                items=tuple(item.transaction for item in page.items),
                next_cursor=next_cursor,
            )

    async def deleted_transactions(
        self,
        credentials: SessionCredentials,
        *,
        limit: int,
        raw_cursor: str | None,
    ) -> TransactionCursorPage:
        """Read one deterministic page from the live deleted dataset."""

        now = self._now()
        async with self._uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                credentials,
                now=now,
            )
            owner_id = authenticated.owner.owner_id
            cursor = (
                self._cursor_codec.decode_deleted(owner_id, raw_cursor)
                if raw_cursor is not None
                else None
            )
            page = await ListDeletedTransactionsByCursor(uow.finance)(
                owner_id,
                cursor=cursor,
                limit=limit,
            )
            next_cursor = None
            if page.has_more:
                next_cursor = self._cursor_codec.encode_deleted(
                    owner_id,
                    page.items[-1].cursor,
                )
            return TransactionCursorPage(
                items=tuple(item.transaction for item in page.items),
                next_cursor=next_cursor,
            )

    async def transaction(
        self,
        credentials: SessionCredentials,
        transaction_id: UUID,
    ) -> TransactionSnapshot:
        now = self._now()
        async with self._uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                credentials,
                now=now,
            )
            return await GetTransaction(uow.finance)(
                authenticated.owner.owner_id,
                transaction_id,
            )
