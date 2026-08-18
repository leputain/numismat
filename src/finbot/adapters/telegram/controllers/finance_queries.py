from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardMarkup

from finbot.adapters.telegram.executor import (
    DuplicateTelegramMutation,
    TelegramMutationExecutor,
    TelegramMutationRequest,
    TelegramMutationSession,
)
from finbot.adapters.telegram.presenters import (
    MONTH_NAMES,
    history_text,
    money,
    operation_sign,
    report_text,
    transaction_details_from_snapshot,
    transaction_snapshot_card,
    trash_text,
)
from finbot.adapters.telegram.ui import (
    append_resume_button,
    delete_confirmation_keyboard,
    history_keyboard,
    resume_draft_keyboard,
    transaction_keyboard,
    trash_keyboard,
    trash_restore_keyboard,
)
from finbot.application.dto import (
    DraftRef,
    DraftSnapshot,
    PeriodReportSnapshot,
    TransactionPageSnapshot,
    TransactionSnapshot,
)
from finbot.application.interactions import (
    MAX_CALLBACK_BYTES,
    MAX_OBJECT_VERSION,
    MAX_PAGE,
)
from finbot.application.ports import FinanceReader, OwnerReader
from finbot.application.queries.reports import CategoryTotal, ReportTransaction
from finbot.application.use_cases.queries import (
    ComparePeriods,
    GetOwnerSettings,
    GetPeriodReport,
    GetTransaction,
    ListTransactions,
)
from finbot.domain.transactions import (
    month_to_date_bounds,
    period_bounds,
    previous_month_to_date_bounds,
)

TRANSACTION_PAGE_SIZE = 5
REPORT_ITEM_LIMIT = 8
MAX_TELEGRAM_TEXT_LENGTH = 4096

HISTORY_PAGE_PREFIX = "h:"
TRASH_PAGE_PREFIX = "z:list:"
ACTIVE_TRANSACTION_PREFIX = "tx:view:"
DELETED_TRANSACTION_PREFIX = "z:view:"


class InvalidFinanceQueryCallback(ValueError):
    """A Telegram finance callback is malformed or outside safe bounds."""


class StaleFinanceQueryCallback(ValueError):
    """A Telegram finance callback targets an obsolete transaction snapshot."""


class FinanceReportPeriod(StrEnum):
    """Initial report periods exposed by the Telegram navigation UI."""

    TODAY = "today"
    MONTH = "month"


class FinanceQueryReader(FinanceReader, OwnerReader, Protocol):
    """Read capabilities required by this controller family."""


type FinanceQueryReaderFactory = Callable[[TelegramMutationSession], FinanceQueryReader]


class FinanceQueryDraftLifecycle(Protocol):
    """Minimum shared draft lifecycle required by navigation queries."""

    async def get_active(self, owner_id: UUID) -> DraftSnapshot | None: ...

    async def suspend(self, owner_id: UUID, expected: DraftRef) -> DraftSnapshot: ...


type FinanceQueryDraftFactory = Callable[[TelegramMutationSession], FinanceQueryDraftLifecycle]


class FinanceQueryClock(Protocol):
    def now(self, timezone: str) -> datetime: ...


class SystemFinanceQueryClock:
    __slots__ = ()

    def now(self, timezone: str) -> datetime:
        return datetime.now(ZoneInfo(timezone))


@dataclass(frozen=True, slots=True)
class TransactionCallbackTarget:
    transaction_id: UUID = field(repr=False)
    expected_version: int = field(repr=False)
    page: int = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.transaction_id, UUID):
            raise TypeError("Transaction id must be a UUID")
        if isinstance(self.expected_version, bool) or not isinstance(self.expected_version, int):
            raise TypeError("Transaction version must be an integer")
        if not 1 <= self.expected_version <= MAX_OBJECT_VERSION:
            raise ValueError("Transaction version is outside callback bounds")
        _validated_page(self.page)


@dataclass(frozen=True, slots=True)
class TelegramQueryContext:
    request: TelegramMutationRequest = field(repr=False)
    message_id: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.message_id, bool) or (
            self.message_id is not None and not isinstance(self.message_id, int)
        ):
            raise TypeError("Telegram message id must be an integer")
        if self.message_id is not None and self.message_id <= 0:
            raise ValueError("Telegram message id must be positive")


@dataclass(frozen=True, slots=True)
class PeriodReportQuery:
    start: datetime = field(repr=False)
    end: datetime = field(repr=False)
    title: str = field(repr=False)
    category_limit: int = field(default=REPORT_ITEM_LIMIT, repr=False)
    transaction_limit: int = field(default=REPORT_ITEM_LIMIT, repr=False)

    def __post_init__(self) -> None:
        if type(self.title) is not str:
            raise TypeError("Report title must be a string")
        if not self.title.strip() or len(self.title) > 128:
            raise ValueError("Report title must contain between 1 and 128 characters")
        for value, label in (
            (self.category_limit, "category"),
            (self.transaction_limit, "transaction"),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"Report {label} limit must be an integer")
            if not 1 <= value <= REPORT_ITEM_LIMIT:
                raise ValueError(f"Report {label} limit must be between 1 and {REPORT_ITEM_LIMIT}")


@dataclass(frozen=True, slots=True)
class TelegramQueryReceipt:
    text: str = field(repr=False)
    reply_markup: InlineKeyboardMarkup | None = field(default=None, repr=False)
    message_id: int | None = field(default=None, repr=False)
    parse_mode: str = field(default="HTML", repr=False)
    suspended_draft: DraftRef | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if type(self.text) is not str:
            raise TypeError("Telegram query receipt text must be a string")
        if not 1 <= len(self.text) <= MAX_TELEGRAM_TEXT_LENGTH:
            raise ValueError("Telegram query receipt has an invalid text length")
        if isinstance(self.message_id, bool) or (
            self.message_id is not None and not isinstance(self.message_id, int)
        ):
            raise TypeError("Telegram query receipt message id must be an integer")
        if self.message_id is not None and self.message_id <= 0:
            raise ValueError("Telegram query receipt message id must be positive")
        if self.parse_mode != "HTML":
            raise ValueError("Telegram query receipts require HTML parse mode")
        if self.suspended_draft is not None and not isinstance(self.suspended_draft, DraftRef):
            raise TypeError("Suspended draft must be a DraftRef")


type TelegramQueryReceiptEnqueuer = Callable[
    [TelegramMutationSession, TelegramMutationRequest, TelegramQueryReceipt], Awaitable[None]
]


@dataclass(frozen=True, slots=True)
class _TransactionPresentation:
    transaction: TransactionSnapshot = field(repr=False)
    timezone: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _TransactionPagePresentation:
    page: TransactionPageSnapshot = field(repr=False)
    deleted_count: int = field(repr=False)
    timezone: str = field(repr=False)
    suspended_draft: DraftRef | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class _PeriodReportPresentation:
    report: PeriodReportSnapshot = field(repr=False)
    timezone: str = field(repr=False)
    title: str = field(repr=False)
    previous_expense: dict[str, int] | None = field(default=None, repr=False)
    suspended_draft: DraftRef | None = field(default=None, repr=False)


def _invalid_callback() -> InvalidFinanceQueryCallback:
    return InvalidFinanceQueryCallback("Кнопка повреждена")


def _validate_callback_envelope(data: str, prefix: str) -> str:
    if type(data) is not str or type(prefix) is not str or not prefix:
        raise _invalid_callback()
    try:
        encoded = data.encode("ascii")
    except UnicodeEncodeError as error:
        raise _invalid_callback() from error
    if len(encoded) > MAX_CALLBACK_BYTES or not data.startswith(prefix):
        raise _invalid_callback()
    payload = data[len(prefix) :]
    if not payload:
        raise _invalid_callback()
    return payload


def _parse_decimal(token: str, *, minimum: int, maximum: int) -> int:
    if not token or not token.isascii() or not token.isdecimal():
        raise _invalid_callback()
    value = int(token)
    if not minimum <= value <= maximum:
        raise _invalid_callback()
    return value


def parse_transaction_callback(
    data: str,
    *,
    prefix: str = ACTIVE_TRANSACTION_PREFIX,
) -> TransactionCallbackTarget:
    """Decode the legacy decimal callback without exposing its payload in errors."""

    parts = _validate_callback_envelope(data, prefix).split(":")
    if len(parts) not in {2, 3}:
        raise _invalid_callback()
    transaction_token, version_token = parts[:2]
    if len(transaction_token) != 36:
        raise _invalid_callback()
    try:
        transaction_id = UUID(transaction_token)
    except (ValueError, AttributeError) as error:
        raise _invalid_callback() from error
    if str(transaction_id) != transaction_token.lower():
        raise _invalid_callback()
    expected_version = _parse_decimal(
        version_token,
        minimum=1,
        maximum=MAX_OBJECT_VERSION,
    )
    page = _parse_decimal(parts[2], minimum=0, maximum=MAX_PAGE) if len(parts) == 3 else 0
    return TransactionCallbackTarget(
        transaction_id=transaction_id,
        expected_version=expected_version,
        page=page,
    )


def parse_page_callback(data: str, *, prefix: str) -> int:
    """Decode a bounded decimal page used by history and trash callbacks."""

    token = _validate_callback_envelope(data, prefix)
    return _parse_decimal(token, minimum=0, maximum=MAX_PAGE)


def parse_history_page_callback(data: str) -> int:
    return parse_page_callback(data, prefix=HISTORY_PAGE_PREFIX)


def parse_trash_page_callback(data: str) -> int:
    return parse_page_callback(data, prefix=TRASH_PAGE_PREFIX)


def _validated_page(page: int) -> int:
    if isinstance(page, bool) or not isinstance(page, int) or not 0 <= page <= MAX_PAGE:
        raise InvalidFinanceQueryCallback("Некорректная страница")
    return page


def render_transaction_receipt(
    transaction: TransactionSnapshot,
    timezone: str,
    *,
    page: int,
    message_id: int | None,
) -> TelegramQueryReceipt:
    """Render one active or deleted snapshot through the existing card presenter."""

    safe_page = _validated_page(page)
    if transaction.deleted_at is None:
        title = "Операция"
        reply_markup = transaction_keyboard(
            transaction.transaction_id,
            transaction.version,
            safe_page,
        )
    else:
        title = "Операция в Корзине"
        reply_markup = trash_restore_keyboard(
            transaction.transaction_id,
            transaction.version,
            safe_page,
        )
    return TelegramQueryReceipt(
        text=transaction_snapshot_card(transaction, timezone, title=title),
        reply_markup=reply_markup,
        message_id=message_id,
    )


def render_delete_confirmation_receipt(
    transaction: TransactionSnapshot,
    timezone: str,
    *,
    page: int,
    message_id: int | None,
) -> TelegramQueryReceipt:
    """Render an exact active transaction before the destructive confirmation."""

    if transaction.deleted_at is not None:
        raise ValueError("Deleted transaction cannot be offered for deletion")
    safe_page = _validated_page(page)
    return TelegramQueryReceipt(
        text=(
            transaction_snapshot_card(transaction, timezone, title="Удалить операцию?")
            + "\n\nОна исчезнет из отчётов и CSV, но останется в Корзине."
        ),
        reply_markup=delete_confirmation_keyboard(
            transaction.transaction_id,
            transaction.version,
            safe_page,
        ),
        message_id=message_id,
    )


def render_transaction_page_receipt(
    page: TransactionPageSnapshot,
    timezone: str,
    *,
    deleted: bool,
    deleted_count: int = 0,
    message_id: int | None,
    suspended_draft: DraftRef | None = None,
) -> TelegramQueryReceipt:
    """Render a page using the legacy text and keyboard presenters."""

    if page.page_size != TRANSACTION_PAGE_SIZE:
        raise ValueError("Telegram transaction page size must be five")
    if page.page != min(page.page, max(page.total_pages, 1) - 1):
        raise ValueError("Telegram transaction page is outside the available range")
    if any((item.deleted_at is not None) is not deleted for item in page.items):
        raise ValueError("Telegram transaction page contains an unexpected state")
    if deleted and suspended_draft is not None:
        raise ValueError("Trash navigation cannot bind a suspended draft")
    if deleted_count < 0:
        raise ValueError("Deleted transaction count must not be negative")
    details = [transaction_details_from_snapshot(item) for item in page.items]
    total_pages = max(page.total_pages, 1)
    labels = [
        (
            item.id,
            item.version,
            f"{operation_sign(item.type)}{money(item.amount_minor, item.currency)} "
            f"· {item.category_name[:18]}",
        )
        for item in details
    ]
    if deleted:
        text = trash_text(details, page.page, page.total, timezone)
        reply_markup = trash_keyboard(labels, page.page, total_pages)
    else:
        text = history_text(details, page.page, page.total, timezone)
        reply_markup = history_keyboard(
            labels,
            page.page,
            total_pages,
            deleted_count,
        )
        if suspended_draft is not None:
            reply_markup = append_resume_button(
                reply_markup,
                has_draft=True,
                draft_id=suspended_draft.draft_id,
                revision=suspended_draft.revision,
            )
    return TelegramQueryReceipt(
        text=text,
        reply_markup=reply_markup,
        message_id=message_id,
        suspended_draft=suspended_draft,
    )


def _report_transaction(item: TransactionSnapshot) -> ReportTransaction:
    details = transaction_details_from_snapshot(item)
    return ReportTransaction(
        id=details.id,
        type=details.type,
        amount_minor=details.amount_minor,
        currency=details.currency,
        occurred_at=details.occurred_at,
        description=details.description,
        version=details.version,
        category_name=details.category_name,
        category_emoji=details.category_emoji,
        account_name=details.account_name,
    )


def render_period_report_receipt(
    report: PeriodReportSnapshot,
    timezone: str,
    *,
    title: str,
    message_id: int | None,
    previous_expense: dict[str, int] | None = None,
    suspended_draft: DraftRef | None = None,
) -> TelegramQueryReceipt:
    """Adapt the shared report snapshot to the existing escaped presenter."""

    totals = {
        item.currency: {
            "income": item.income_minor,
            "expense": item.expense_minor,
        }
        for item in report.totals
    }
    categories = [
        CategoryTotal(
            name=item.name,
            emoji=item.emoji,
            currency=item.currency,
            amount_minor=item.amount_minor,
        )
        for item in report.category_totals
    ]
    transactions = [_report_transaction(item) for item in report.transactions]
    return TelegramQueryReceipt(
        text=report_text(
            title,
            totals,
            categories,
            transactions,
            timezone,
            previous_expense=previous_expense,
        ),
        reply_markup=(
            resume_draft_keyboard(
                suspended_draft.draft_id,
                suspended_draft.revision,
            )
            if suspended_draft is not None
            else None
        ),
        message_id=message_id,
        suspended_draft=suspended_draft,
    )


class FinanceQueryController:
    """Run owner-scoped finance reads through the durable Telegram executor."""

    __slots__ = (
        "_clock",
        "_draft_factory",
        "_enqueue_receipt",
        "_executor",
        "_reader_factory",
    )

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        reader_factory: FinanceQueryReaderFactory,
        enqueue_receipt: TelegramQueryReceiptEnqueuer,
        *,
        draft_factory: FinanceQueryDraftFactory | None = None,
        clock: FinanceQueryClock | None = None,
    ) -> None:
        self._executor = executor
        self._reader_factory = reader_factory
        self._enqueue_receipt = enqueue_receipt
        self._draft_factory = draft_factory
        self._clock = clock or SystemFinanceQueryClock()

    @staticmethod
    def _require_initial_message(context: TelegramQueryContext) -> None:
        if context.message_id is not None:
            raise ValueError("Initial finance navigation must send a new message")

    async def _suspend_for_navigation(
        self,
        session: TelegramMutationSession,
        owner_id: UUID,
    ) -> DraftRef | None:
        if self._draft_factory is None:
            raise RuntimeError("Finance navigation draft lifecycle is not configured")
        drafts = self._draft_factory(session)
        active = await drafts.get_active(owner_id)
        if active is None:
            return None
        # Call unconditionally: the PostgreSQL adapter locks and CAS-validates
        # an already suspended row without incrementing its revision.
        return (await drafts.suspend(owner_id, active.ref)).ref

    async def _execute[QueryValue](
        self,
        context: TelegramQueryContext,
        *,
        query: Callable[[TelegramMutationSession, UUID], Awaitable[QueryValue]],
        render: Callable[[QueryValue], TelegramQueryReceipt],
    ) -> TelegramQueryReceipt | None:
        execution = await self._executor.execute(
            context.request,
            mutate=query,
            build_receipt=render,
            enqueue_receipt=self._enqueue_receipt,
        )
        if isinstance(execution, DuplicateTelegramMutation):
            return None
        return execution.receipt

    async def get_transaction(
        self,
        context: TelegramQueryContext,
        target: TransactionCallbackTarget,
        *,
        deleted: bool,
    ) -> TelegramQueryReceipt | None:
        async def query(
            session: TelegramMutationSession, owner_id: UUID
        ) -> _TransactionPresentation:
            reader = self._reader_factory(session)
            transaction = await GetTransaction(reader)(owner_id, target.transaction_id)
            if transaction.version != target.expected_version or (
                (transaction.deleted_at is not None) is not deleted
            ):
                raise StaleFinanceQueryCallback("Операция изменилась")
            owner = await GetOwnerSettings(reader)(owner_id)
            return _TransactionPresentation(transaction=transaction, timezone=owner.timezone)

        def render(value: _TransactionPresentation) -> TelegramQueryReceipt:
            return render_transaction_receipt(
                value.transaction,
                value.timezone,
                page=target.page,
                message_id=context.message_id,
            )

        return await self._execute(context, query=query, render=render)

    async def confirm_transaction_delete(
        self,
        context: TelegramQueryContext,
        target: TransactionCallbackTarget,
    ) -> TelegramQueryReceipt | None:
        """Load and render one exact active snapshot in the atomic query envelope."""

        async def query(
            session: TelegramMutationSession, owner_id: UUID
        ) -> _TransactionPresentation:
            reader = self._reader_factory(session)
            transaction = await GetTransaction(reader)(owner_id, target.transaction_id)
            if transaction.version != target.expected_version or transaction.deleted_at is not None:
                raise StaleFinanceQueryCallback("Операция изменилась")
            owner = await GetOwnerSettings(reader)(owner_id)
            return _TransactionPresentation(transaction=transaction, timezone=owner.timezone)

        return await self._execute(
            context,
            query=query,
            render=lambda value: render_delete_confirmation_receipt(
                value.transaction,
                value.timezone,
                page=target.page,
                message_id=context.message_id,
            ),
        )

    async def list_transactions(
        self,
        context: TelegramQueryContext,
        *,
        page: int,
        deleted: bool,
    ) -> TelegramQueryReceipt | None:
        return await self._list_transactions(
            context,
            page=page,
            deleted=deleted,
            suspend_for_navigation=False,
        )

    async def open_history(
        self,
        context: TelegramQueryContext,
    ) -> TelegramQueryReceipt | None:
        """Open the first history page and pause any active draft atomically."""

        self._require_initial_message(context)
        return await self._list_transactions(
            context,
            page=0,
            deleted=False,
            suspend_for_navigation=True,
        )

    async def _list_transactions(
        self,
        context: TelegramQueryContext,
        *,
        page: int,
        deleted: bool,
        suspend_for_navigation: bool,
    ) -> TelegramQueryReceipt | None:
        requested_page = _validated_page(page)

        async def query(
            session: TelegramMutationSession, owner_id: UUID
        ) -> _TransactionPagePresentation:
            reader = self._reader_factory(session)
            suspended_draft = (
                await self._suspend_for_navigation(session, owner_id)
                if suspend_for_navigation
                else None
            )
            list_transactions = ListTransactions(reader)
            snapshot = await list_transactions(
                owner_id,
                page=requested_page,
                page_size=TRANSACTION_PAGE_SIZE,
                deleted=deleted,
            )
            safe_page = min(requested_page, max(snapshot.total_pages, 1) - 1)
            if safe_page != requested_page:
                snapshot = await list_transactions(
                    owner_id,
                    page=safe_page,
                    page_size=TRANSACTION_PAGE_SIZE,
                    deleted=deleted,
                )
            deleted_count = 0
            if not deleted:
                deleted_count = (
                    await list_transactions(
                        owner_id,
                        page=0,
                        page_size=1,
                        deleted=True,
                    )
                ).total
            owner = await GetOwnerSettings(reader)(owner_id)
            return _TransactionPagePresentation(
                page=snapshot,
                deleted_count=deleted_count,
                timezone=owner.timezone,
                suspended_draft=suspended_draft,
            )

        def render(value: _TransactionPagePresentation) -> TelegramQueryReceipt:
            return render_transaction_page_receipt(
                value.page,
                value.timezone,
                deleted=deleted,
                deleted_count=value.deleted_count,
                message_id=context.message_id,
                suspended_draft=value.suspended_draft,
            )

        return await self._execute(context, query=query, render=render)

    async def get_period_report(
        self,
        context: TelegramQueryContext,
        query_input: PeriodReportQuery,
    ) -> TelegramQueryReceipt | None:
        async def query(
            session: TelegramMutationSession, owner_id: UUID
        ) -> _PeriodReportPresentation:
            reader = self._reader_factory(session)
            report = await GetPeriodReport(reader)(
                owner_id,
                query_input.start,
                query_input.end,
                category_limit=query_input.category_limit,
                transaction_limit=query_input.transaction_limit,
            )
            owner = await GetOwnerSettings(reader)(owner_id)
            return _PeriodReportPresentation(
                report=report,
                timezone=owner.timezone,
                title=query_input.title,
            )

        def render(value: _PeriodReportPresentation) -> TelegramQueryReceipt:
            return render_period_report_receipt(
                value.report,
                value.timezone,
                title=value.title,
                message_id=context.message_id,
                previous_expense=value.previous_expense,
                suspended_draft=value.suspended_draft,
            )

        return await self._execute(context, query=query, render=render)

    async def open_report(
        self,
        context: TelegramQueryContext,
        period: FinanceReportPeriod,
    ) -> TelegramQueryReceipt | None:
        """Open today's or month-to-date report and pause an active draft."""

        self._require_initial_message(context)
        if not isinstance(period, FinanceReportPeriod):
            raise TypeError("Finance report period is invalid")

        async def query(
            session: TelegramMutationSession, owner_id: UUID
        ) -> _PeriodReportPresentation:
            reader = self._reader_factory(session)
            owner = await GetOwnerSettings(reader)(owner_id)
            suspended_draft = await self._suspend_for_navigation(session, owner_id)
            now = self._clock.now(owner.timezone)
            if now.utcoffset() is None:
                raise ValueError("Finance query clock must return a timezone-aware moment")
            local_now = now.astimezone(ZoneInfo(owner.timezone))

            previous_expense: dict[str, int] | None = None
            if period is FinanceReportPeriod.MONTH:
                start, end = month_to_date_bounds(local_now, owner.timezone)
                previous_start, previous_end = previous_month_to_date_bounds(
                    local_now,
                    owner.timezone,
                )
                comparison = await ComparePeriods(reader)(
                    owner_id,
                    start,
                    end,
                    previous_start,
                    previous_end,
                )
                previous_expense = {
                    item.currency: item.expense_minor for item in comparison.previous_totals
                }
                title = f"📊 {MONTH_NAMES[local_now.month].capitalize()} {local_now.year}"
            else:
                start, end = period_bounds(local_now.date(), owner.timezone)
                title = f"📅 Сегодня · {local_now:%d.%m.%Y}"

            report = await GetPeriodReport(reader)(
                owner_id,
                start,
                end,
                category_limit=REPORT_ITEM_LIMIT,
                transaction_limit=REPORT_ITEM_LIMIT,
            )
            return _PeriodReportPresentation(
                report=report,
                timezone=owner.timezone,
                title=title,
                previous_expense=previous_expense,
                suspended_draft=suspended_draft,
            )

        def render(value: _PeriodReportPresentation) -> TelegramQueryReceipt:
            return render_period_report_receipt(
                value.report,
                value.timezone,
                title=value.title,
                message_id=None,
                previous_expense=value.previous_expense,
                suspended_draft=value.suspended_draft,
            )

        return await self._execute(context, query=query, render=render)
