from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from html import escape
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

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
from finbot.application.recurring import (
    RecurringInstanceOutcome,
    RecurringInstancePageSnapshot,
    RecurringReader,
    RecurringSchedulePageSnapshot,
    RecurringScheduleSnapshot,
    RecurringScheduleState,
)
from finbot.application.use_cases.recurring import (
    GetRecurringSchedule,
    ListRecurringInstances,
    ListRecurringSchedules,
)
from finbot.domain.money import format_minor
from finbot.domain.recurrence import RecurrenceCadence

TELEGRAM_RECURRING_LIMIT = 5


class TelegramRecurringReader(RecurringReader, Protocol):
    """Bounded owner-scoped recurring read capabilities."""


type TelegramRecurringReaderFactory = Callable[[TelegramMutationSession], TelegramRecurringReader]
type TelegramRecurringReceiptEnqueuer = Callable[
    [TelegramMutationSession, TelegramMutationRequest, TelegramQueryReceipt], Awaitable[None]
]


@dataclass(frozen=True, slots=True, repr=False)
class _ScheduleDetail:
    schedule: RecurringScheduleSnapshot = field(repr=False)
    instances: RecurringInstancePageSnapshot = field(repr=False)


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
    result = f"{public_url}/recurring"
    if len(result) > 2048:
        raise ValueError("Mini App recurring URL is too long")
    return result


def _manage_button(manage_url: str | None) -> list[InlineKeyboardButton]:
    if manage_url is None:
        return []
    return [
        InlineKeyboardButton(
            text="Управлять расписаниями",
            web_app=WebAppInfo(url=manage_url),
        )
    ]


def _state_label(state: RecurringScheduleState) -> str:
    return {
        RecurringScheduleState.ACTIVE: "активно",
        RecurringScheduleState.PAUSED: "на паузе",
        RecurringScheduleState.PAUSED_ERROR: "нужно исправить",
        RecurringScheduleState.COMPLETED: "завершено",
        RecurringScheduleState.DELETED: "удалено",
    }[state]


def _outcome_label(outcome: RecurringInstanceOutcome) -> str:
    return {
        RecurringInstanceOutcome.PENDING: "ожидает",
        RecurringInstanceOutcome.BLOCKED: "заблокирован",
        RecurringInstanceOutcome.SKIPPED: "пропущен",
        RecurringInstanceOutcome.AWAITING_REVIEW: "на проверке",
        RecurringInstanceOutcome.CONFIRMED: "подтверждён",
        RecurringInstanceOutcome.DISMISSED: "отклонён",
    }[outcome]


def _render_list(
    page: RecurringSchedulePageSnapshot,
    manage_url: str | None,
    *,
    message_id: int | None,
) -> TelegramQueryReceipt:
    lines = ["<b>🔁 Регулярные операции</b>", ""]
    rows: list[list[InlineKeyboardButton]] = []
    if not page.items:
        lines.append("Активных расписаний пока нет.")
    else:
        for schedule in page.items:
            definition = schedule.definition
            lines.append(
                f"• <b>{escape(definition.name)}</b> · "
                f"{escape(format_minor(definition.amount_minor, definition.currency))} · "
                f"{_state_label(schedule.state)}"
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        text=definition.name[:48],
                        callback_data=f"rec:s:{schedule.schedule_id}",
                    )
                ]
            )
        if page.next_cursor is not None:
            lines.extend(("", "Показаны первые 5 расписаний; полный список — в Mini App."))
    manage = _manage_button(manage_url)
    if manage:
        rows.append(manage)
    return TelegramQueryReceipt(
        text="\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
        message_id=message_id,
    )


def _cadence_label(schedule: RecurringScheduleSnapshot) -> str:
    recurrence = schedule.definition.recurrence
    unit = {
        RecurrenceCadence.DAILY: "дн.",
        RecurrenceCadence.WEEKLY: "нед.",
        RecurrenceCadence.MONTHLY: "мес.",
    }[recurrence.cadence]
    return f"каждые {recurrence.interval} {unit}"


def _render_detail(
    detail: _ScheduleDetail,
    manage_url: str | None,
    *,
    message_id: int | None,
) -> TelegramQueryReceipt:
    schedule = detail.schedule
    definition = schedule.definition
    recurrence = definition.recurrence
    due = (
        schedule.next_due_local.strftime("%d.%m.%Y %H:%M")
        if schedule.next_due_local is not None
        else "нет"
    )
    lines = [
        f"<b>🔁 {escape(definition.name)}</b>",
        "",
        f"Сумма: {escape(format_minor(definition.amount_minor, definition.currency))}",
        f"Период: {_cadence_label(schedule)}",
        f"Время: {recurrence.local_time.strftime('%H:%M')} · {escape(recurrence.timezone)}",
        f"Следующий запуск: {due}",
        f"Статус: {_state_label(schedule.state)}",
    ]
    if detail.instances.items:
        lines.extend(("", "<b>Последние экземпляры</b>"))
        for instance in detail.instances.items:
            nominal = instance.nominal_local.strftime("%d.%m.%Y %H:%M")
            lines.append(f"• {nominal} · {_outcome_label(instance.outcome)}")
        if detail.instances.next_cursor is not None:
            lines.append("Ещё экземпляры доступны в Mini App.")
    rows = [[InlineKeyboardButton(text="← К списку", callback_data="rec:list")]]
    manage = _manage_button(manage_url)
    if manage:
        rows.append(manage)
    return TelegramQueryReceipt(
        text="\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        message_id=message_id,
    )


class TelegramRecurringController:
    """Execute bounded recurring reads inside Telegram's deduplicated UoW."""

    __slots__ = ("_enqueue_receipt", "_executor", "_manage_url", "_readers")

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        reader_factory: TelegramRecurringReaderFactory,
        enqueue_receipt: TelegramRecurringReceiptEnqueuer,
        *,
        miniapp_public_url: str | None,
    ) -> None:
        self._executor = executor
        self._readers = reader_factory
        self._enqueue_receipt = enqueue_receipt
        self._manage_url = _manage_url(miniapp_public_url)

    async def list(self, context: TelegramQueryContext) -> TelegramQueryReceipt | None:
        async def query(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> RecurringSchedulePageSnapshot:
            return await ListRecurringSchedules(self._readers(session))(
                owner_id,
                deleted=False,
                cursor=None,
                limit=TELEGRAM_RECURRING_LIMIT,
            )

        execution = await self._executor.execute(
            context.request,
            mutate=query,
            build_receipt=lambda page: _render_list(
                page,
                self._manage_url,
                message_id=context.message_id,
            ),
            enqueue_receipt=self._enqueue_receipt,
        )
        if isinstance(execution, DuplicateTelegramMutation):
            return None
        return execution.receipt

    async def detail(
        self,
        context: TelegramQueryContext,
        schedule_id: UUID,
    ) -> TelegramQueryReceipt | None:
        async def query(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> _ScheduleDetail:
            reader = self._readers(session)
            schedule = await GetRecurringSchedule(reader)(owner_id, schedule_id)
            instances = await ListRecurringInstances(reader)(
                owner_id,
                schedule_id,
                cursor=None,
                limit=TELEGRAM_RECURRING_LIMIT,
            )
            return _ScheduleDetail(schedule=schedule, instances=instances)

        execution = await self._executor.execute(
            context.request,
            mutate=query,
            build_receipt=lambda detail: _render_detail(
                detail,
                self._manage_url,
                message_id=context.message_id,
            ),
            enqueue_receipt=self._enqueue_receipt,
        )
        if isinstance(execution, DuplicateTelegramMutation):
            return None
        return execution.receipt


__all__ = ["TELEGRAM_RECURRING_LIMIT", "TelegramRecurringController"]
