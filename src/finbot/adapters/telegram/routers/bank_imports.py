from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from aiogram import Bot, Dispatcher
from aiogram.filters import Filter
from aiogram.types import Message

from finbot.adapters.telegram.bank_import_delivery import (
    TelegramBankImportAccount,
    is_bank_csv_document,
)
from finbot.adapters.telegram.controllers.bank_imports import (
    TelegramBankImportController,
    TelegramBankImportIngress,
    TelegramBankImportOutcome,
)
from finbot.adapters.telegram.executor import TelegramMutationRequest
from finbot.application.bank_imports import (
    BankImportEncoding,
    BankImportProfile,
    BankImportValidationError,
    CreateBankImportCommand,
)
from finbot.application.use_cases.bank_imports import BankImportPreparer


class BankCsvDocumentFilter(Filter):
    async def __call__(self, message: Message) -> bool:
        return is_bank_csv_document(message)


class ProcessedUpdateReader(Protocol):
    async def __call__(self, update_id: int, /) -> bool: ...


class BankCsvDownloader(Protocol):
    async def __call__(self, bot: Bot, message: Message, /) -> bytes | None: ...


class BankImportAccountResolver(Protocol):
    async def __call__(
        self,
        owner_telegram_user_id: int,
        /,
    ) -> TelegramBankImportAccount | None: ...


type TelegramBankImportRequestFactory = Callable[[int | None, Message], TelegramMutationRequest]


class TelegramBankImportRouter:
    """Preflight replay/default-account before bounded download and local prepare."""

    __slots__ = (
        "_accounts",
        "_controller",
        "_download",
        "_is_processed",
        "_preparer",
        "_request",
    )

    def __init__(
        self,
        controller: TelegramBankImportController,
        request_factory: TelegramBankImportRequestFactory,
        account_resolver: BankImportAccountResolver,
        downloader: BankCsvDownloader,
        preparer: BankImportPreparer,
        is_processed: ProcessedUpdateReader,
    ) -> None:
        self._controller = controller
        self._request = request_factory
        self._accounts = account_resolver
        self._download = downloader
        self._preparer = preparer
        self._is_processed = is_processed

    def register(self, dispatcher: Dispatcher) -> None:
        dispatcher.message.register(self.csv_input, BankCsvDocumentFilter())

    async def csv_input(
        self,
        message: Message,
        bot: Bot,
        finbot_update_id: int | None = None,
    ) -> None:
        if finbot_update_id is not None and await self._is_processed(finbot_update_id):
            return
        request = self._request(finbot_update_id, message)
        account = await self._accounts(request.owner_telegram_user_id)
        ingress: TelegramBankImportIngress
        if account is None:
            ingress = TelegramBankImportIngress(
                request,
                TelegramBankImportOutcome.ACCOUNT_REQUIRED,
            )
        else:
            content = await self._download(bot, message)
            if content is None:
                ingress = TelegramBankImportIngress(
                    request,
                    TelegramBankImportOutcome.INVALID_FILE,
                )
            else:
                try:
                    prepared = self._preparer.prepare(
                        CreateBankImportCommand(
                            owner_id=account.owner_id,
                            account_id=account.account_id,
                            expected_account_version=account.account_version,
                            profile=BankImportProfile.CANONICAL_V1,
                            content=content,
                            encoding=BankImportEncoding.UTF8,
                        )
                    )
                except BankImportValidationError, ValueError:
                    ingress = TelegramBankImportIngress(
                        request,
                        TelegramBankImportOutcome.INVALID_FILE,
                    )
                else:
                    ingress = TelegramBankImportIngress(
                        request,
                        TelegramBankImportOutcome.CREATED,
                        prepared,
                    )
                finally:
                    del content
        receipt = await self._controller.process(ingress)
        if receipt is not None and finbot_update_id is None:
            await message.answer(
                receipt.text,
                parse_mode=receipt.parse_mode,
                reply_markup=receipt.reply_markup,
            )


__all__ = ["TelegramBankImportRouter"]
