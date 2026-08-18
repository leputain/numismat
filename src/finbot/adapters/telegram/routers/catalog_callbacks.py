"""Focused settings catalog mutation callbacks."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import UUID

from aiogram.types import CallbackQuery, Message

from finbot.adapters.telegram.controllers.catalogs import (
    CatalogController,
    CatalogReceiptSnapshot,
    TelegramCatalogContext,
    VersionedAccountInput,
    VersionedCategoryInput,
)
from finbot.application.errors import ApplicationError, ObjectVersionConflictError
from finbot.application.interactions import MAX_OBJECT_VERSION

type CatalogContextFactory = Callable[[int | None, Message], TelegramCatalogContext]
type CatalogReceiptDelivery = Callable[
    [Message, CatalogReceiptSnapshot],
    Awaitable[None],
]
type CatalogOperation[CatalogInput] = Callable[
    [TelegramCatalogContext, CatalogInput],
    Awaitable[CatalogReceiptSnapshot | None],
]
type CatalogInputFactory[CatalogInput] = Callable[[UUID, int], CatalogInput]


def _parse_versioned_target(data: str, prefix: str) -> tuple[UUID, int]:
    """Preserve the legacy UUID/version callback contract exactly."""

    if not data.startswith(prefix):
        raise ValueError("invalid catalog callback")
    parts = data.removeprefix(prefix).split(":")
    if len(parts) != 2:
        raise ValueError("invalid catalog callback")
    entity_id = UUID(parts[0])
    version = int(parts[1])
    if str(entity_id) != parts[0] or str(version) != parts[1]:
        raise ValueError("non-canonical catalog callback")
    if not 1 <= version <= MAX_OBJECT_VERSION:
        raise ValueError("invalid catalog version")
    return entity_id, version


@dataclass(frozen=True, slots=True)
class CatalogCallbackRouter:
    """Parse and execute the five versioned catalog mutation callbacks."""

    controller: CatalogController = field(repr=False)
    context: CatalogContextFactory = field(repr=False)
    deliver_untracked: CatalogReceiptDelivery = field(repr=False)

    @staticmethod
    def _message(callback: CallbackQuery) -> Message | None:
        return callback.message if isinstance(callback.message, Message) else None

    async def _finish(
        self,
        callback: CallbackQuery,
        message: Message,
        update_id: int | None,
        receipt: CatalogReceiptSnapshot | None,
        success_text: str,
    ) -> None:
        if receipt is None:
            await callback.answer("Уже обработано")
            return
        if update_id is None:
            await self.deliver_untracked(message, receipt)
        await callback.answer(success_text)

    async def _run[CatalogInput](
        self,
        callback: CallbackQuery,
        update_id: int | None,
        *,
        prefix: str,
        input_factory: CatalogInputFactory[CatalogInput],
        operation: CatalogOperation[CatalogInput],
        conflict_text: str,
        success_text: str,
    ) -> None:
        message = self._message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            entity_id, expected_version = _parse_versioned_target(callback.data, prefix)
        except ValueError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        try:
            receipt = await operation(
                self.context(update_id, message),
                input_factory(entity_id, expected_version),
            )
        except ObjectVersionConflictError:
            await callback.answer(conflict_text, show_alert=True)
            return
        except ApplicationError as error:
            await callback.answer(str(error), show_alert=True)
            return
        await self._finish(callback, message, update_id, receipt, success_text)

    async def settings_account_default(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        await self._run(
            callback,
            finbot_update_id,
            prefix="sa:default:",
            input_factory=VersionedAccountInput,
            operation=self.controller.set_default_account,
            conflict_text="Счёт уже изменён. Обновите список",
            success_text="Основной счёт изменён",
        )

    async def settings_account_archive_do(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        await self._run(
            callback,
            finbot_update_id,
            prefix="sa:archive:do:",
            input_factory=VersionedAccountInput,
            operation=self.controller.archive_account,
            conflict_text="Счёт уже изменён. Обновите список",
            success_text="Счёт перенесён в архив",
        )

    async def settings_account_restore(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        await self._run(
            callback,
            finbot_update_id,
            prefix="sa:restore:",
            input_factory=VersionedAccountInput,
            operation=self.controller.restore_account,
            conflict_text="Счёт уже изменён. Обновите список",
            success_text="Счёт восстановлен",
        )

    async def settings_category_archive_do(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        await self._run(
            callback,
            finbot_update_id,
            prefix="sc:archive:do:",
            input_factory=VersionedCategoryInput,
            operation=self.controller.archive_category,
            conflict_text="Категория уже изменена. Обновите список",
            success_text="Категория перенесена в архив",
        )

    async def settings_category_restore(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        await self._run(
            callback,
            finbot_update_id,
            prefix="sc:restore:",
            input_factory=VersionedCategoryInput,
            operation=self.controller.restore_category,
            conflict_text="Категория уже изменена. Обновите список",
            success_text="Категория восстановлена",
        )


__all__ = ["CatalogCallbackRouter"]
