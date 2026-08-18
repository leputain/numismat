from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from finbot.adapters.telegram.controllers.finance_queries import TelegramQueryReceipt
from finbot.adapters.telegram.executor import (
    DuplicateTelegramMutation,
    TelegramMutationExecutor,
    TelegramMutationRequest,
    TelegramMutationSession,
)
from finbot.application.bank_imports import (
    BankImportAccountCurrencyMismatchError,
    StageBankImportBatchCommand,
)
from finbot.application.errors import (
    CatalogUnavailableError,
    InvalidStateError,
    ObjectVersionConflictError,
)
from finbot.application.use_cases.bank_imports import BankImportUseCases


class TelegramBankImportOutcome(StrEnum):
    CREATED = "created"
    INVALID_FILE = "invalid_file"
    ACCOUNT_REQUIRED = "account_required"


@dataclass(frozen=True, slots=True, repr=False)
class TelegramBankImportIngress:
    request: TelegramMutationRequest = field(repr=False)
    outcome: TelegramBankImportOutcome
    prepared: StageBankImportBatchCommand | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.request, TelegramMutationRequest):
            raise TypeError("Telegram bank import request is invalid")
        if not isinstance(self.outcome, TelegramBankImportOutcome):
            raise TypeError("Telegram bank import outcome is invalid")
        if (self.outcome is TelegramBankImportOutcome.CREATED) != isinstance(
            self.prepared,
            StageBankImportBatchCommand,
        ):
            raise ValueError("Telegram bank import prepared state is inconsistent")


@dataclass(frozen=True, slots=True, repr=False)
class _TelegramBankImportResult:
    outcome: TelegramBankImportOutcome
    row_count: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.outcome is TelegramBankImportOutcome.CREATED:
            if (
                isinstance(self.row_count, bool)
                or not isinstance(self.row_count, int)
                or not 1 <= self.row_count <= 2000
            ):
                raise ValueError("Created bank import row count is invalid")
        elif self.row_count is not None:
            raise ValueError("Rejected bank import cannot contain a row count")


class TelegramBankImportUseCaseFactory(Protocol):
    def __call__(self, session: TelegramMutationSession, /) -> BankImportUseCases: ...


type TelegramBankImportReceiptEnqueuer = Callable[
    [TelegramMutationSession, TelegramMutationRequest, TelegramQueryReceipt],
    Awaitable[None],
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
    result = f"{public_url}/imports"
    if len(result) > 2048:
        raise ValueError("Mini App bank import URL is too long")
    return result


def _render(result: _TelegramBankImportResult, manage_url: str | None) -> TelegramQueryReceipt:
    if result.outcome is TelegramBankImportOutcome.CREATED:
        text = (
            "<b>CSV подготовлен к сверке</b>\n\n"
            f"Строк: <b>{result.row_count}</b>. "
            "Ничего не сохранено в операциях: откройте импорт и обработайте строки явно."
        )
    elif result.outcome is TelegramBankImportOutcome.ACCOUNT_REQUIRED:
        text = (
            "<b>Нужен основной счёт</b>\n\n"
            "Выберите основной активный счёт или загрузите CSV в Mini App."
        )
    else:
        text = (
            "<b>CSV не прошёл проверку</b>\n\n"
            "Используйте UTF-8, канонические заголовки и не более 2000 строк."
        )
    markup = None
    if manage_url is not None:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Открыть банковский импорт",
                        web_app=WebAppInfo(url=manage_url),
                    )
                ]
            ]
        )
    return TelegramQueryReceipt(text=text, reply_markup=markup)


class TelegramBankImportController:
    """Atomically claim a prepared ingress, stage it, and queue a safe receipt."""

    __slots__ = ("_enqueue_receipt", "_executor", "_manage_url", "_use_cases")

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        use_case_factory: TelegramBankImportUseCaseFactory,
        enqueue_receipt: TelegramBankImportReceiptEnqueuer,
        *,
        miniapp_public_url: str | None,
    ) -> None:
        self._executor = executor
        self._use_cases = use_case_factory
        self._enqueue_receipt = enqueue_receipt
        self._manage_url = _manage_url(miniapp_public_url)

    async def process(self, ingress: TelegramBankImportIngress) -> TelegramQueryReceipt | None:
        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> _TelegramBankImportResult:
            command = ingress.prepared
            if command is None:
                return _TelegramBankImportResult(ingress.outcome)
            if command.owner_id != owner_id:
                raise InvalidStateError("Владелец импорта изменился")
            try:
                batch = await self._use_cases(session).create_prepared(command)
            except CatalogUnavailableError, ObjectVersionConflictError:
                return _TelegramBankImportResult(TelegramBankImportOutcome.ACCOUNT_REQUIRED)
            except BankImportAccountCurrencyMismatchError:
                # The authoritative account-currency check raced the pre-read.
                # Commit a fixed rejection so polling does not redownload forever.
                return _TelegramBankImportResult(TelegramBankImportOutcome.INVALID_FILE)
            return _TelegramBankImportResult(
                TelegramBankImportOutcome.CREATED,
                batch.counts.total,
            )

        execution = await self._executor.execute(
            ingress.request,
            mutate=mutate,
            build_receipt=lambda result: _render(result, self._manage_url),
            enqueue_receipt=self._enqueue_receipt,
        )
        if isinstance(execution, DuplicateTelegramMutation):
            return None
        return execution.receipt


__all__ = [
    "TelegramBankImportController",
    "TelegramBankImportIngress",
    "TelegramBankImportOutcome",
]
