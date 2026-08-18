"""Compact versioned draft-interaction routing at the Telegram boundary."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from finbot.adapters.telegram.controllers.draft_completion import (
    DraftCompletionController,
    DraftCompletionKind,
    DraftCompletionReceiptSnapshot,
    TelegramDraftCompletionContext,
)
from finbot.adapters.telegram.controllers.draft_conflicts import (
    DraftConflictController,
    DraftConflictReceiptSnapshot,
    TelegramDraftConflictContext,
)
from finbot.adapters.telegram.controllers.draft_navigation import (
    DraftNavigationController,
    DraftNavigationReceiptSnapshot,
    TelegramDraftNavigationContext,
)
from finbot.adapters.telegram.controllers.draft_rules import (
    DraftRuleController,
    DraftRuleReceiptSnapshot,
    TelegramDraftRuleContext,
)
from finbot.adapters.telegram.controllers.drafts import (
    DraftController,
    DraftSettingsReceiptSnapshot,
    TelegramDraftContext,
)
from finbot.adapters.telegram.controllers.ocr_queue import (
    OcrQueueOperation,
    OcrQueueReceiptSnapshot,
)
from finbot.adapters.telegram.controllers.plain_drafts import PlainDraftReceiptSnapshot
from finbot.adapters.telegram.controllers.transaction_draft_navigation import (
    TelegramTransactionDraftNavigationContext,
    TransactionDraftNavigationController,
    TransactionDraftNavigationReceiptSnapshot,
)
from finbot.adapters.telegram.controllers.transaction_draft_selection import (
    TelegramTransactionDraftSelectionContext,
    TransactionDraftSelectionController,
    TransactionDraftSelectionReceiptSnapshot,
)
from finbot.application.draft_navigation import (
    DraftCatalogChoice,
    DraftCatalogRef,
    DraftDateChoice,
    DraftNavigationAction,
    DraftNavigationChoice,
)
from finbot.application.draft_rules import DraftRuleAction
from finbot.application.dto import DraftConflictResolution, DraftRef, OcrQueueStatus
from finbot.application.errors import ApplicationError, DraftRevisionConflictError
from finbot.application.interactions import (
    DraftAction,
    DraftInteraction,
    InteractionCodecError,
)
from finbot.application.transaction_draft_navigation import (
    TransactionDraftNavigationAction,
)
from finbot.application.transaction_draft_selection import (
    TransactionDraftSelectionAction,
    TransactionDraftSelectionStatus,
)
from finbot.domain.transactions import TransactionType


class RenderedDraftInteractionReceipt(Protocol):
    """Structural renderer result, independent of bootstrap-private DTOs."""

    @property
    def text(self) -> str: ...

    @property
    def reply_markup(self) -> InlineKeyboardMarkup | None: ...


class DraftPresentationBinder(Protocol):
    """Persist the post-delivery Telegram binding outside the router."""

    async def __call__(
        self,
        message: Message,
        draft: DraftRef,
        delivered_message_id: int,
        *,
        history_page: int | None = None,
        override_context: bool = False,
    ) -> None: ...


type DraftContextFactory = Callable[[int | None, Message], TelegramDraftContext]
type DraftRuleContextFactory = Callable[[int | None, Message], TelegramDraftRuleContext]
type DraftNavigationContextFactory = Callable[[int | None, Message], TelegramDraftNavigationContext]
type TransactionDraftNavigationContextFactory = Callable[
    [int | None, Message], TelegramTransactionDraftNavigationContext
]
type TransactionDraftSelectionContextFactory = Callable[
    [int | None, Message], TelegramTransactionDraftSelectionContext
]
type DraftCompletionContextFactory = Callable[[int | None, Message], TelegramDraftCompletionContext]
type DraftConflictContextFactory = Callable[[int | None, Message], TelegramDraftConflictContext]

type DraftSettingsRenderer = Callable[
    [DraftSettingsReceiptSnapshot], RenderedDraftInteractionReceipt
]
type DraftRuleRenderer = Callable[
    [DraftRuleReceiptSnapshot], tuple[RenderedDraftInteractionReceipt, DraftRef]
]
type DraftNavigationRenderer = Callable[
    [DraftNavigationReceiptSnapshot],
    tuple[RenderedDraftInteractionReceipt, DraftRef | None],
]
type TransactionDraftNavigationRenderer = Callable[
    [TransactionDraftNavigationReceiptSnapshot],
    tuple[RenderedDraftInteractionReceipt, DraftRef],
]
type TransactionDraftSelectionRenderer = Callable[
    [TransactionDraftSelectionReceiptSnapshot],
    tuple[RenderedDraftInteractionReceipt, DraftRef | None],
]
type PlainDraftRenderer = Callable[[PlainDraftReceiptSnapshot], RenderedDraftInteractionReceipt]
type OcrQueueRenderer = Callable[
    [OcrQueueReceiptSnapshot], tuple[RenderedDraftInteractionReceipt, DraftRef | None]
]
type DraftConflictRenderer = Callable[
    [DraftConflictReceiptSnapshot], tuple[RenderedDraftInteractionReceipt, DraftRef]
]
type MessageReplacer = Callable[[Message, str, InlineKeyboardMarkup | None], Awaitable[int]]


@dataclass(frozen=True, slots=True)
class DraftInteractionControllers:
    drafts: DraftController = field(repr=False)
    rules: DraftRuleController = field(repr=False)
    navigation: DraftNavigationController = field(repr=False)
    transaction_navigation: TransactionDraftNavigationController = field(repr=False)
    transaction_selection: TransactionDraftSelectionController = field(repr=False)
    completion: DraftCompletionController = field(repr=False)
    conflicts: DraftConflictController = field(repr=False)


@dataclass(frozen=True, slots=True)
class DraftInteractionContexts:
    drafts: DraftContextFactory = field(repr=False)
    rules: DraftRuleContextFactory = field(repr=False)
    navigation: DraftNavigationContextFactory = field(repr=False)
    transaction_navigation: TransactionDraftNavigationContextFactory = field(repr=False)
    transaction_selection: TransactionDraftSelectionContextFactory = field(repr=False)
    completion: DraftCompletionContextFactory = field(repr=False)
    conflicts: DraftConflictContextFactory = field(repr=False)


@dataclass(frozen=True, slots=True)
class DraftInteractionRenderers:
    drafts: DraftSettingsRenderer = field(repr=False)
    rules: DraftRuleRenderer = field(repr=False)
    navigation: DraftNavigationRenderer = field(repr=False)
    transaction_navigation: TransactionDraftNavigationRenderer = field(repr=False)
    transaction_selection: TransactionDraftSelectionRenderer = field(repr=False)
    plain_completion: PlainDraftRenderer = field(repr=False)
    ocr_completion: OcrQueueRenderer = field(repr=False)
    conflicts: DraftConflictRenderer = field(repr=False)


@dataclass(frozen=True, slots=True)
class DraftInteractionDelivery:
    replace_message: MessageReplacer = field(repr=False)
    bind_presentation: DraftPresentationBinder = field(repr=False)


_RULE_ACTIONS = {
    DraftAction.RULE_GLOBAL: DraftRuleAction.GLOBAL,
    DraftAction.RULE_ACCOUNT: DraftRuleAction.ACCOUNT,
    DraftAction.RULE_REMOVE: DraftRuleAction.REMOVE,
}

_NAVIGATION_ACTIONS = {
    DraftAction.EDIT_TYPE: DraftNavigationAction.EDIT_TYPE,
    DraftAction.EDIT_AMOUNT: DraftNavigationAction.EDIT_AMOUNT,
    DraftAction.EDIT_CATEGORY: DraftNavigationAction.EDIT_CATEGORY,
    DraftAction.EDIT_ACCOUNT: DraftNavigationAction.EDIT_ACCOUNT,
    DraftAction.EDIT_DATE: DraftNavigationAction.EDIT_DATE,
    DraftAction.EDIT_DESCRIPTION: DraftNavigationAction.EDIT_DESCRIPTION,
    DraftAction.SKIP_DESCRIPTION: DraftNavigationAction.SKIP_DESCRIPTION,
    DraftAction.BACK: DraftNavigationAction.BACK,
    DraftAction.SELECT_TYPE: DraftNavigationAction.SELECT_TYPE,
    DraftAction.SELECT_CATEGORY: DraftNavigationAction.SELECT_CATEGORY,
    DraftAction.SELECT_ACCOUNT: DraftNavigationAction.SELECT_ACCOUNT,
    DraftAction.SELECT_DATE: DraftNavigationAction.SELECT_DATE,
}

_TRANSACTION_NAVIGATION_ACTIONS = {
    DraftAction.TX_EDIT_AMOUNT: TransactionDraftNavigationAction.EDIT_AMOUNT,
    DraftAction.TX_EDIT_CATEGORY: TransactionDraftNavigationAction.EDIT_CATEGORY,
    DraftAction.TX_EDIT_ACCOUNT: TransactionDraftNavigationAction.EDIT_ACCOUNT,
    DraftAction.TX_EDIT_DATE: TransactionDraftNavigationAction.EDIT_DATE,
    DraftAction.TX_EDIT_DESCRIPTION: TransactionDraftNavigationAction.EDIT_DESCRIPTION,
    DraftAction.TX_DATE_BACK: TransactionDraftNavigationAction.DATE_BACK,
    DraftAction.TX_BACK: TransactionDraftNavigationAction.BACK,
}

_TRANSACTION_SELECTION_ACTIONS = {
    DraftAction.TX_SELECT_CATEGORY: TransactionDraftSelectionAction.CATEGORY,
    DraftAction.TX_SELECT_ACCOUNT: TransactionDraftSelectionAction.ACCOUNT,
    DraftAction.TX_SELECT_DATE: TransactionDraftSelectionAction.DATE,
}

_CONFLICT_RESOLUTIONS = {
    DraftAction.RESUME: DraftConflictResolution.RESUME,
    DraftAction.REPLACE: DraftConflictResolution.REPLACE,
    DraftAction.KEEP: DraftConflictResolution.KEEP,
}

_DATE_CHOICES = {
    0: DraftDateChoice.TODAY,
    1: DraftDateChoice.YESTERDAY,
    2: DraftDateChoice.CUSTOM,
}

_REVISION_CONFLICT_TEXT = "Форма уже изменилась. Откройте актуальный черновик"


@dataclass(frozen=True, slots=True)
class DraftInteractionRouter:
    """Dispatch compact ``d*`` callbacks to atomic controllers.

    Context factories and renderers must be side-effect-free. Direct Telegram
    delivery and presentation binding run only after a controller returned its
    committed receipt; tracked updates rely on the durable response outbox.
    """

    controllers: DraftInteractionControllers = field(repr=False)
    contexts: DraftInteractionContexts = field(repr=False)
    renderers: DraftInteractionRenderers = field(repr=False)
    delivery: DraftInteractionDelivery = field(repr=False)

    @staticmethod
    def _message(callback: CallbackQuery) -> Message | None:
        return callback.message if isinstance(callback.message, Message) else None

    async def _replace(
        self,
        message: Message,
        rendered: RenderedDraftInteractionReceipt,
    ) -> int:
        return await self.delivery.replace_message(
            message,
            rendered.text,
            rendered.reply_markup,
        )

    async def _replace_and_bind(
        self,
        message: Message,
        rendered: RenderedDraftInteractionReceipt,
        draft: DraftRef | None,
        *,
        history_page: int | None = None,
        override_context: bool = False,
    ) -> None:
        delivered_message_id = await self._replace(message, rendered)
        if draft is not None:
            await self.delivery.bind_presentation(
                message,
                draft,
                delivered_message_id,
                history_page=history_page,
                override_context=override_context,
            )

    @staticmethod
    async def _answer_revision_conflict(callback: CallbackQuery) -> None:
        await callback.answer(_REVISION_CONFLICT_TEXT, show_alert=True)

    async def _discard(
        self,
        callback: CallbackQuery,
        message: Message,
        interaction: DraftInteraction,
        update_id: int | None,
    ) -> None:
        try:
            receipt = await self.controllers.drafts.discard(
                self.contexts.drafts(update_id, message),
                DraftRef(interaction.draft_id, interaction.revision),
            )
        except DraftRevisionConflictError:
            await self._answer_revision_conflict(callback)
            return
        except ApplicationError as error:
            await callback.answer(str(error), show_alert=True)
            return
        if receipt is None:
            await callback.answer("Уже обработано")
            return
        if update_id is None:
            await self._replace(message, self.renderers.drafts(receipt))
        await callback.answer("Незавершённый ввод сброшен")

    async def _apply_rule(
        self,
        callback: CallbackQuery,
        message: Message,
        interaction: DraftInteraction,
        update_id: int | None,
        action: DraftRuleAction,
    ) -> None:
        try:
            receipt = await self.controllers.rules.execute(
                self.contexts.rules(update_id, message),
                DraftRef(interaction.draft_id, interaction.revision),
                action,
            )
        except DraftRevisionConflictError:
            await self._answer_revision_conflict(callback)
            return
        except ApplicationError as error:
            await callback.answer(str(error), show_alert=True)
            return
        if receipt is None:
            await callback.answer("Уже обработано")
            return
        if update_id is None:
            rendered, draft = self.renderers.rules(receipt)
            await self._replace_and_bind(message, rendered, draft)
        await callback.answer(
            "Правило не будет сохранено"
            if action is DraftRuleAction.REMOVE
            else "Правило будет сохранено вместе с операцией"
        )

    @staticmethod
    def _navigation_choice(interaction: DraftInteraction) -> DraftNavigationChoice | None:
        if interaction.action is DraftAction.SELECT_TYPE:
            if interaction.page not in {0, 1} or interaction.object_id is not None:
                raise ValueError("invalid type choice")
            return TransactionType.INCOME if interaction.page == 1 else TransactionType.EXPENSE
        if interaction.action in {
            DraftAction.SELECT_CATEGORY,
            DraftAction.SELECT_ACCOUNT,
        }:
            if interaction.page is not None:
                raise ValueError("invalid catalog choice")
            if interaction.object_id is None:
                return DraftCatalogChoice.CUSTOM
            if interaction.object_version is None:  # pragma: no cover - codec invariant
                raise ValueError("invalid catalog reference")
            return DraftCatalogRef(interaction.object_id, interaction.object_version)
        if interaction.action is DraftAction.SELECT_DATE:
            if interaction.object_id is not None:
                raise ValueError("invalid date choice")
            page = interaction.page
            if page is None:
                raise ValueError("invalid date choice")
            choice = _DATE_CHOICES.get(page)
            if choice is None:
                raise ValueError("invalid date choice")
            return choice
        return None

    async def _navigate(
        self,
        callback: CallbackQuery,
        message: Message,
        interaction: DraftInteraction,
        update_id: int | None,
        action: DraftNavigationAction,
    ) -> None:
        try:
            choice = self._navigation_choice(interaction)
        except ValueError:
            await callback.answer("Кнопка повреждена", show_alert=True)
            return
        try:
            receipt = await self.controllers.navigation.execute(
                self.contexts.navigation(update_id, message),
                DraftRef(interaction.draft_id, interaction.revision),
                action,
                choice,
            )
        except DraftRevisionConflictError:
            await self._answer_revision_conflict(callback)
            return
        except ApplicationError as error:
            await callback.answer(str(error), show_alert=True)
            return
        if receipt is None:
            await callback.answer("Уже обработано")
            return
        if update_id is None:
            rendered, draft = self.renderers.navigation(receipt)
            await self._replace_and_bind(message, rendered, draft)
        await callback.answer()

    async def _navigate_transaction(
        self,
        callback: CallbackQuery,
        message: Message,
        interaction: DraftInteraction,
        update_id: int | None,
        action: TransactionDraftNavigationAction,
    ) -> None:
        try:
            receipt = await self.controllers.transaction_navigation.execute(
                self.contexts.transaction_navigation(update_id, message),
                DraftRef(interaction.draft_id, interaction.revision),
                action,
                interaction.page or 0,
            )
        except DraftRevisionConflictError:
            await self._answer_revision_conflict(callback)
            return
        except ApplicationError as error:
            await callback.answer(str(error), show_alert=True)
            return
        if receipt is None:
            await callback.answer("Уже обработано")
            return
        if update_id is None:
            rendered, draft = self.renderers.transaction_navigation(receipt)
            await self._replace_and_bind(
                message,
                rendered,
                draft,
                history_page=receipt.history_page,
                override_context=True,
            )
        await callback.answer()

    @staticmethod
    def _transaction_selection_choice(
        interaction: DraftInteraction,
        action: TransactionDraftSelectionAction,
    ) -> tuple[DraftCatalogRef | DraftDateChoice, int]:
        if action in {
            TransactionDraftSelectionAction.CATEGORY,
            TransactionDraftSelectionAction.ACCOUNT,
        }:
            if interaction.object_id is None or interaction.object_version is None:
                raise ValueError("catalog")
            return (
                DraftCatalogRef(interaction.object_id, interaction.object_version),
                interaction.page or 0,
            )
        if interaction.object_id is not None or interaction.page is None:
            raise ValueError("date")
        date_choice = _DATE_CHOICES.get(interaction.page % 3)
        if date_choice is None:  # pragma: no cover - modulo domain is closed
            raise ValueError("date")
        return date_choice, interaction.page // 3

    async def _select_transaction_value(
        self,
        callback: CallbackQuery,
        message: Message,
        interaction: DraftInteraction,
        update_id: int | None,
        action: TransactionDraftSelectionAction,
    ) -> None:
        try:
            choice, history_page = self._transaction_selection_choice(interaction, action)
        except ValueError as error:
            label = "даты" if error.args == ("date",) else "выбора"
            await callback.answer(f"Кнопка {label} повреждена", show_alert=True)
            return
        try:
            receipt = await self.controllers.transaction_selection.execute(
                self.contexts.transaction_selection(update_id, message),
                DraftRef(interaction.draft_id, interaction.revision),
                action,
                choice,
                history_page,
            )
        except DraftRevisionConflictError:
            await self._answer_revision_conflict(callback)
            return
        except ApplicationError as error:
            await callback.answer(str(error), show_alert=True)
            return
        if receipt is None:
            await callback.answer("Уже обработано")
            return
        if update_id is None:
            rendered, draft = self.renderers.transaction_selection(receipt)
            await self._replace_and_bind(
                message,
                rendered,
                draft,
                history_page=receipt.history_page,
                override_context=True,
            )
        await callback.answer(
            "Введите дату"
            if receipt.result.status is TransactionDraftSelectionStatus.DATE_INPUT_REQUIRED
            else "Изменено"
        )

    async def _complete(
        self,
        callback: CallbackQuery,
        message: Message,
        interaction: DraftInteraction,
        update_id: int | None,
    ) -> None:
        expected = DraftRef(interaction.draft_id, interaction.revision)
        context = self.contexts.completion(update_id, message)
        try:
            if interaction.action is DraftAction.CONFIRM:
                receipt = await self.controllers.completion.confirm(context, expected)
            elif interaction.action is DraftAction.SKIP_OCR_ITEM:
                receipt = await self.controllers.completion.skip_ocr_item(context, expected)
            else:
                receipt = await self.controllers.completion.cancel(context, expected)
        except DraftRevisionConflictError:
            await self._answer_revision_conflict(callback)
            return
        except ApplicationError as error:
            await callback.answer(str(error), show_alert=True)
            return
        if receipt is None:
            await callback.answer("Уже обработано")
            return
        if update_id is None:
            if receipt.kind is DraftCompletionKind.PLAIN:
                if receipt.plain is None:  # pragma: no cover - DTO invariant
                    raise RuntimeError("Plain completion receipt is missing")
                rendered = self.renderers.plain_completion(receipt.plain)
                draft = None
            else:
                if receipt.ocr_queue is None:  # pragma: no cover - DTO invariant
                    raise RuntimeError("OCR completion receipt is missing")
                rendered, draft = self.renderers.ocr_completion(receipt.ocr_queue)
            await self._replace_and_bind(message, rendered, draft)
        await callback.answer(self._completion_answer(interaction.action, receipt))

    @staticmethod
    def _completion_answer(
        action: DraftAction,
        receipt: DraftCompletionReceiptSnapshot,
    ) -> str:
        if receipt.kind is DraftCompletionKind.PLAIN:
            return "Сохранено" if action is DraftAction.CONFIRM else "Отменено"
        if receipt.ocr_queue is None:  # pragma: no cover - DTO invariant
            raise RuntimeError("OCR completion receipt is missing")
        ocr_receipt = receipt.ocr_queue
        return {
            OcrQueueOperation.CONFIRMED: (
                "Сохранено, проверьте следующую"
                if ocr_receipt.result.status is OcrQueueStatus.ADVANCED
                else "Сохранено"
            ),
            OcrQueueOperation.SKIPPED: "Пропущено",
            OcrQueueOperation.CANCELLED: "Отменено",
        }[ocr_receipt.operation]

    async def _resolve_conflict(
        self,
        callback: CallbackQuery,
        message: Message,
        interaction: DraftInteraction,
        update_id: int | None,
        resolution: DraftConflictResolution,
    ) -> None:
        try:
            receipt = await self.controllers.conflicts.resolve(
                self.contexts.conflicts(update_id, message),
                DraftRef(interaction.draft_id, interaction.revision),
                resolution,
            )
        except DraftRevisionConflictError:
            await self._answer_revision_conflict(callback)
            return
        except (ApplicationError, ValueError) as error:
            await callback.answer(str(error), show_alert=True)
            return
        if receipt is None:
            await callback.answer("Уже обработано")
            return
        if update_id is None:
            rendered, draft = self.renderers.conflicts(receipt)
            await self._replace_and_bind(
                message,
                rendered,
                draft,
                history_page=receipt.history_page,
                override_context=True,
            )
        await callback.answer(
            {
                DraftConflictResolution.RESUME: "Черновик продолжен",
                DraftConflictResolution.KEEP: "Текущий черновик сохранён",
                DraftConflictResolution.REPLACE: "Начат новый ввод",
            }[resolution]
        )

    async def versioned_draft(
        self,
        callback: CallbackQuery,
        finbot_update_id: int | None = None,
    ) -> None:
        """Handle one strictly decoded compact draft callback."""

        message = self._message(callback)
        if message is None or callback.data is None:
            await callback.answer()
            return
        try:
            interaction = DraftInteraction.decode(callback.data)
        except InteractionCodecError:
            await callback.answer("Кнопка повреждена или устарела", show_alert=True)
            return

        if interaction.action is DraftAction.DISCARD:
            await self._discard(callback, message, interaction, finbot_update_id)
            return
        if (rule_action := _RULE_ACTIONS.get(interaction.action)) is not None:
            await self._apply_rule(
                callback,
                message,
                interaction,
                finbot_update_id,
                rule_action,
            )
            return
        if (navigation_action := _NAVIGATION_ACTIONS.get(interaction.action)) is not None:
            await self._navigate(
                callback,
                message,
                interaction,
                finbot_update_id,
                navigation_action,
            )
            return
        if (
            transaction_navigation_action := _TRANSACTION_NAVIGATION_ACTIONS.get(interaction.action)
        ) is not None:
            await self._navigate_transaction(
                callback,
                message,
                interaction,
                finbot_update_id,
                transaction_navigation_action,
            )
            return
        if (
            transaction_selection_action := _TRANSACTION_SELECTION_ACTIONS.get(interaction.action)
        ) is not None:
            await self._select_transaction_value(
                callback,
                message,
                interaction,
                finbot_update_id,
                transaction_selection_action,
            )
            return
        if interaction.action in {
            DraftAction.CONFIRM,
            DraftAction.CANCEL,
            DraftAction.SKIP_OCR_ITEM,
        }:
            await self._complete(callback, message, interaction, finbot_update_id)
            return
        resolution = _CONFLICT_RESOLUTIONS.get(interaction.action)
        if resolution is None:
            await callback.answer("Действие больше не поддерживается", show_alert=True)
            return
        await self._resolve_conflict(
            callback,
            message,
            interaction,
            finbot_update_id,
            resolution,
        )


__all__ = [
    "DraftInteractionContexts",
    "DraftInteractionControllers",
    "DraftInteractionDelivery",
    "DraftInteractionRenderers",
    "DraftInteractionRouter",
    "DraftPresentationBinder",
    "RenderedDraftInteractionReceipt",
]
