from __future__ import annotations

from collections.abc import Awaitable, Callable
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
from finbot.application.exchange_rates import ExchangeRateReader, RateSourceSnapshot
from finbot.application.use_cases.exchange_rates import ListExchangeRateSources


class TelegramExchangeRateReader(ExchangeRateReader, Protocol):
    """Bounded owner-scoped exchange-rate reads."""


type TelegramExchangeRateReaderFactory = Callable[
    [TelegramMutationSession], TelegramExchangeRateReader
]
type TelegramExchangeRateReceiptEnqueuer = Callable[
    [TelegramMutationSession, TelegramMutationRequest, TelegramQueryReceipt], Awaitable[None]
]


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
    result = f"{public_url}/rates"
    if len(result) > 2048:
        raise ValueError("Mini App exchange-rate URL is too long")
    return result


def _render(
    sources: tuple[RateSourceSnapshot, ...],
    manage_url: str | None,
) -> TelegramQueryReceipt:
    lines = ["<b>💱 Курсы валют</b>", ""]
    if not sources:
        lines.append("Ручные источники курсов ещё не настроены.")
    else:
        for source in sources[:5]:
            lines.append(
                f"• База <b>{escape(source.target_currency)}</b> · версия {source.latest_version}"
            )
        if len(sources) > 5:
            lines.extend(("", "Показаны первые 5 источников; полный список — в Mini App."))
        lines.extend(
            (
                "",
                "Конвертация отчёта всегда использует явно выбранную неизменяемую версию.",
            )
        )
    markup = None
    if manage_url is not None:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Управлять курсами",
                        web_app=WebAppInfo(url=manage_url),
                    )
                ]
            ]
        )
    return TelegramQueryReceipt(text="\n".join(lines), reply_markup=markup)


class TelegramExchangeRateController:
    """Execute a bounded read and enqueue its response in the Telegram UoW."""

    __slots__ = ("_enqueue_receipt", "_executor", "_manage_url", "_readers")

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        reader_factory: TelegramExchangeRateReaderFactory,
        enqueue_receipt: TelegramExchangeRateReceiptEnqueuer,
        *,
        miniapp_public_url: str | None,
    ) -> None:
        self._executor = executor
        self._readers = reader_factory
        self._enqueue_receipt = enqueue_receipt
        self._manage_url = _manage_url(miniapp_public_url)

    async def open(self, context: TelegramQueryContext) -> TelegramQueryReceipt | None:
        async def query(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> tuple[RateSourceSnapshot, ...]:
            return await ListExchangeRateSources(self._readers(session))(owner_id)

        execution = await self._executor.execute(
            context.request,
            mutate=query,
            build_receipt=lambda sources: _render(sources, self._manage_url),
            enqueue_receipt=self._enqueue_receipt,
        )
        if isinstance(execution, DuplicateTelegramMutation):
            return None
        return execution.receipt


__all__ = ["TelegramExchangeRateController"]
