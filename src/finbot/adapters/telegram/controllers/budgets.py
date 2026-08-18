from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from html import escape
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from finbot.adapters.telegram.controllers.finance_queries import (
    TelegramQueryContext,
    TelegramQueryReceipt,
)
from finbot.adapters.telegram.executor import (
    DuplicateTelegramMutation,
    TelegramMutationExecutor,
    TelegramMutationRequest,
    TelegramMutationSession,
)
from finbot.application.budgets import BudgetPageSnapshot, BudgetReader
from finbot.application.dto import DraftRef, DraftSnapshot
from finbot.application.ports import OwnerReader
from finbot.application.use_cases.budgets import ListBudgetProgress
from finbot.application.use_cases.queries import GetOwnerSettings
from finbot.domain.money import format_minor

TELEGRAM_BUDGET_LIMIT = 5


class TelegramBudgetReader(BudgetReader, OwnerReader, Protocol):
    """Read capabilities needed by the bounded Telegram budget overview."""


class TelegramBudgetDraftLifecycle(Protocol):
    async def get_active(self, owner_id: UUID) -> DraftSnapshot | None: ...

    async def suspend(self, owner_id: UUID, expected: DraftRef) -> DraftSnapshot: ...


type TelegramBudgetReaderFactory = Callable[[TelegramMutationSession], TelegramBudgetReader]
type TelegramBudgetDraftFactory = Callable[[TelegramMutationSession], TelegramBudgetDraftLifecycle]
type TelegramBudgetReceiptEnqueuer = Callable[
    [TelegramMutationSession, TelegramMutationRequest, TelegramQueryReceipt], Awaitable[None]
]


@dataclass(frozen=True, slots=True, repr=False)
class _BudgetPresentation:
    page: BudgetPageSnapshot = field(repr=False)
    suspended_draft: DraftRef | None = field(default=None, repr=False)


def _month_window(now: datetime, timezone: str) -> tuple[date, date]:
    local = now.astimezone(ZoneInfo(timezone)).date()
    start = local.replace(day=1)
    next_month = (
        date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)
    )
    return start, next_month - timedelta(days=1)


def _percent(progress_bps: int) -> str:
    whole, fraction = divmod(progress_bps, 100)
    return f"{whole}%" if fraction == 0 else f"{whole},{fraction:02d}%"


def _manage_url(public_url: str | None) -> str | None:
    if public_url is None:
        return None
    parsed = urlsplit(public_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Mini App public URL is invalid")
    result = f"{public_url}/budgets"
    if len(result) > 2048:
        raise ValueError("Mini App budget URL is too long")
    return result


def _render(presentation: _BudgetPresentation, manage_url: str | None) -> TelegramQueryReceipt:
    lines = ["<b>🎯 Бюджеты текущего месяца</b>", ""]
    if not presentation.page.items:
        lines.append("Активных бюджетов на этот период пока нет.")
    else:
        for item in presentation.page.items:
            budget = item.budget
            definition = budget.definition
            progress = item.progress
            marker = "🔴" if progress.overspent_minor > 0 else "🟢"
            lines.append(
                f"{marker} <b>{escape(definition.name)}</b> · "
                f"{escape(format_minor(progress.spent_minor, definition.currency))} из "
                f"{escape(format_minor(definition.limit_minor, definition.currency))} "
                f"({_percent(progress.progress_bps)})"
            )
        if presentation.page.next_cursor is not None:
            lines.extend(("", "Показаны первые 5 бюджетов; полный список доступен в Mini App."))
    markup = None
    if manage_url is not None:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Управлять бюджетами",
                        web_app=WebAppInfo(url=manage_url),
                    )
                ]
            ]
        )
    return TelegramQueryReceipt(
        text="\n".join(lines),
        reply_markup=markup,
        suspended_draft=presentation.suspended_draft,
    )


class TelegramBudgetController:
    """Commit deduplication, bounded read, draft suspension and receipt atomically."""

    __slots__ = (
        "_drafts",
        "_enqueue_receipt",
        "_executor",
        "_manage_url",
        "_readers",
    )

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        reader_factory: TelegramBudgetReaderFactory,
        draft_factory: TelegramBudgetDraftFactory,
        enqueue_receipt: TelegramBudgetReceiptEnqueuer,
        *,
        miniapp_public_url: str | None,
    ) -> None:
        self._executor = executor
        self._readers = reader_factory
        self._drafts = draft_factory
        self._enqueue_receipt = enqueue_receipt
        self._manage_url = _manage_url(miniapp_public_url)

    async def open(self, context: TelegramQueryContext) -> TelegramQueryReceipt | None:
        now = datetime.now(UTC)

        async def query(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> _BudgetPresentation:
            reader = self._readers(session)
            owner = await GetOwnerSettings(reader)(owner_id)
            drafts = self._drafts(session)
            active = await drafts.get_active(owner_id)
            suspended = (
                (await drafts.suspend(owner_id, active.ref)).ref if active is not None else None
            )
            start, end = _month_window(now, owner.timezone)
            page = await ListBudgetProgress(reader)(
                owner_id,
                window_start=start,
                window_end=end,
                deleted=False,
                cursor=None,
                limit=TELEGRAM_BUDGET_LIMIT,
                as_of=now,
            )
            return _BudgetPresentation(page=page, suspended_draft=suspended)

        execution = await self._executor.execute(
            context.request,
            mutate=query,
            build_receipt=lambda value: _render(value, self._manage_url),
            enqueue_receipt=self._enqueue_receipt,
        )
        if isinstance(execution, DuplicateTelegramMutation):
            return None
        return execution.receipt


__all__ = ["TELEGRAM_BUDGET_LIMIT", "TelegramBudgetController"]
