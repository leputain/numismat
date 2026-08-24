"""Focused message handlers for draft ingress and initial finance queries."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

from finbot.adapters.telegram.controllers.budgets import TelegramBudgetController
from finbot.adapters.telegram.controllers.draft_ingress import (
    DraftIngressController,
    DraftIngressReceiptSnapshot,
    TelegramDraftIngressContext,
)
from finbot.adapters.telegram.controllers.exchange_rates import (
    TelegramExchangeRateController,
)
from finbot.adapters.telegram.controllers.finance_queries import (
    FinanceQueryController,
    FinanceReportPeriod,
    TelegramQueryContext,
    TelegramQueryReceipt,
)
from finbot.application.errors import ApplicationError

type DraftIngressContextFactory = Callable[[int | None, Message], TelegramDraftIngressContext]
type FinanceMessageContextFactory = Callable[[int | None, Message], TelegramQueryContext]
type DraftIngressDirectDelivery = Callable[[Message, DraftIngressReceiptSnapshot], Awaitable[None]]
type FinanceMessageDirectDelivery = Callable[[Message, TelegramQueryReceipt], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class FinanceMessageRouter:
    """Invoke atomic controllers and perform only post-commit direct delivery."""

    draft_ingress: DraftIngressController = field(repr=False)
    finance_queries: FinanceQueryController = field(repr=False)
    draft_context: DraftIngressContextFactory = field(repr=False)
    finance_context: FinanceMessageContextFactory = field(repr=False)
    deliver_draft_untracked: DraftIngressDirectDelivery = field(repr=False)
    deliver_finance_untracked: FinanceMessageDirectDelivery = field(repr=False)
    budget_queries: TelegramBudgetController | None = field(default=None, repr=False)
    budget_context: FinanceMessageContextFactory | None = field(default=None, repr=False)
    exchange_rate_queries: TelegramExchangeRateController | None = field(
        default=None,
        repr=False,
    )
    exchange_rate_context: FinanceMessageContextFactory | None = field(
        default=None,
        repr=False,
    )

    def register(self, dispatcher: Dispatcher) -> None:
        """Register before the broad legacy text-input handler."""

        observer = dispatcher.message
        observer.register(self.start_wizard, Command("wizard"))
        observer.register(
            self.start_wizard,
            F.text.in_({"➕ Добавить", "➕ Добавить операцию", "➕ Новая операция", "🧙 Мастер"}),
        )
        observer.register(self.report, Command("today", "month"))
        observer.register(self.report, F.text.in_({"📅 Сегодня", "📊 Месяц"}))
        observer.register(self.history, Command("last"))
        observer.register(
            self.history,
            F.text.in_({"🧾 Операции", "🧾 Все операции", "🧾 История", "🧾 Последние"}),
        )
        if self.budget_queries is not None and self.budget_context is not None:
            observer.register(self.budgets, Command("budgets"))
            observer.register(self.budgets, F.text.in_({"🎯 Бюджеты"}))
        if self.exchange_rate_queries is not None and self.exchange_rate_context is not None:
            observer.register(self.exchange_rates, Command("rates"))
            observer.register(self.exchange_rates, F.text.in_({"💱 Курсы"}))

    async def start_wizard(
        self,
        message: Message,
        finbot_update_id: int | None = None,
    ) -> None:
        try:
            receipt = await self.draft_ingress.begin_wizard(
                self.draft_context(finbot_update_id, message)
            )
        except ApplicationError as error:
            await message.answer(str(error))
            return
        if receipt is not None and finbot_update_id is None:
            await self.deliver_draft_untracked(message, receipt)

    async def report(
        self,
        message: Message,
        finbot_update_id: int | None = None,
    ) -> None:
        period = (
            FinanceReportPeriod.MONTH
            if message.text and (message.text.startswith("/month") or message.text == "📊 Месяц")
            else FinanceReportPeriod.TODAY
        )
        receipt = await self.finance_queries.open_report(
            self.finance_context(finbot_update_id, message),
            period,
        )
        if receipt is not None and finbot_update_id is None:
            await self.deliver_finance_untracked(message, receipt)

    async def history(
        self,
        message: Message,
        finbot_update_id: int | None = None,
    ) -> None:
        receipt = await self.finance_queries.open_history(
            self.finance_context(finbot_update_id, message)
        )
        if receipt is not None and finbot_update_id is None:
            await self.deliver_finance_untracked(message, receipt)

    async def budgets(
        self,
        message: Message,
        finbot_update_id: int | None = None,
    ) -> None:
        if self.budget_queries is None or self.budget_context is None:
            return
        receipt = await self.budget_queries.open(self.budget_context(finbot_update_id, message))
        if receipt is not None and finbot_update_id is None:
            await self.deliver_finance_untracked(message, receipt)

    async def exchange_rates(
        self,
        message: Message,
        finbot_update_id: int | None = None,
    ) -> None:
        if self.exchange_rate_queries is None or self.exchange_rate_context is None:
            return
        receipt = await self.exchange_rate_queries.open(
            self.exchange_rate_context(finbot_update_id, message)
        )
        if receipt is not None and finbot_update_id is None:
            await self.deliver_finance_untracked(message, receipt)


__all__ = ["FinanceMessageRouter"]
