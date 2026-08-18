import logging
from dataclasses import dataclass, field
from functools import partial
from html import escape
from typing import cast
from uuid import UUID

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finbot.adapters.ai.disabled import DisabledLocalAiSuggestionProvider
from finbot.adapters.ai.ollama import OllamaLocalAiSuggestionProvider
from finbot.adapters.bank_import import HmacBankImportDigester, StrictBankCsvParser
from finbot.adapters.database.repositories.bank_imports import SqlAlchemyBankImportRepository
from finbot.adapters.database.repositories.budgets import SqlAlchemyBudgetRepository
from finbot.adapters.database.repositories.catalogs import SqlAlchemyCatalogRepository
from finbot.adapters.database.repositories.draft_conflicts import (
    SqlAlchemyDraftConflictReplacementTargets,
)
from finbot.adapters.database.repositories.draft_navigation import (
    SqlAlchemyDraftNavigationCatalogRepository,
)
from finbot.adapters.database.repositories.draft_preparation import (
    SqlAlchemyDraftPreparationRepository,
)
from finbot.adapters.database.repositories.draft_presentations import (
    SqlAlchemyDraftConflictPresentationGuard,
    SqlAlchemyDraftPresentationGuard,
    TelegramDraftPresentationContext,
    lock_telegram_draft_presentation_context,
    lock_telegram_draft_presentation_context_by_revision,
    lock_telegram_draft_presentation_message,
)
from finbot.adapters.database.repositories.drafts import SqlAlchemyDraftRepository
from finbot.adapters.database.repositories.exchange_rates import (
    SqlAlchemyExchangeRateRepository,
)
from finbot.adapters.database.repositories.finance_draft_text_input import (
    SqlAlchemyFinanceDraftTextInputCatalogRepository,
    SqlAlchemyFinanceDraftTextTargetRepository,
)
from finbot.adapters.database.repositories.ocr_queue import SqlAlchemyOcrQueueCommandRepository
from finbot.adapters.database.repositories.queries import SqlAlchemyQueryRepository
from finbot.adapters.database.repositories.recurring import SqlAlchemyRecurringRepository
from finbot.adapters.database.repositories.settings_mutations import (
    SqlAlchemySettingsMutationRepository,
)
from finbot.adapters.database.repositories.settings_text_input import (
    SqlAlchemySettingsTextInputRepository,
)
from finbot.adapters.database.repositories.transaction_draft_selection import (
    SqlAlchemyTransactionDraftSelectionRepository,
)
from finbot.adapters.database.repositories.transaction_edit_ingress import (
    SqlAlchemyTransactionEditTargetReader,
)
from finbot.adapters.database.repositories.transaction_edit_text_input import (
    SqlAlchemyTransactionEditTextTargetRepository,
)
from finbot.adapters.database.repositories.transactions import (
    SqlAlchemyTransactionCommandRepository,
)
from finbot.adapters.database.repositories.undo import SqlAlchemyUndoRepository
from finbot.adapters.database.services.outbox import (
    bind_telegram_draft_presentation,
    queue_edit_message_text,
    queue_send_message,
)
from finbot.adapters.database.session import session_factory
from finbot.adapters.ocr.tesseract import TesseractTextExtractor
from finbot.adapters.telegram.bank_import_delivery import (
    BoundedTelegramBankCsvDownloader,
    TelegramBankImportAccountResolver,
)
from finbot.adapters.telegram.controllers.bank_imports import TelegramBankImportController
from finbot.adapters.telegram.controllers.budgets import TelegramBudgetController
from finbot.adapters.telegram.controllers.catalogs import (
    AccountCatalogReceiptSnapshot,
    CatalogController,
    CatalogOperation,
    CatalogReceiptSnapshot,
    CatalogSessionUseCases,
    CategoryCatalogReceiptSnapshot,
    TelegramCatalogContext,
)
from finbot.adapters.telegram.controllers.draft_completion import (
    DraftCompletionController,
    DraftCompletionKind,
    DraftCompletionReceiptSnapshot,
    DraftCompletionSessionUseCases,
    TelegramDraftCompletionContext,
)
from finbot.adapters.telegram.controllers.draft_conflicts import (
    DraftConflictController,
    DraftConflictPresentationContextReader,
    DraftConflictReceiptSnapshot,
    DraftConflictSessionUseCases,
    TelegramDraftConflictContext,
)
from finbot.adapters.telegram.controllers.draft_ingress import (
    DraftIngressController,
    DraftIngressReceiptSnapshot,
    DraftIngressSessionUseCases,
    TelegramDraftIngressContext,
)
from finbot.adapters.telegram.controllers.draft_navigation import (
    DraftNavigationController,
    DraftNavigationReceiptSnapshot,
    DraftNavigationSessionUseCases,
    TelegramDraftNavigationContext,
)
from finbot.adapters.telegram.controllers.draft_rules import (
    DraftRuleController,
    DraftRuleReceiptSnapshot,
    DraftRuleSessionUseCases,
    TelegramDraftRuleContext,
)
from finbot.adapters.telegram.controllers.drafts import (
    DraftController,
    DraftSessionUseCases,
    DraftSettingsReceiptSnapshot,
    TelegramDraftContext,
)
from finbot.adapters.telegram.controllers.exchange_rates import (
    TelegramExchangeRateController,
)
from finbot.adapters.telegram.controllers.exports import CsvExportController
from finbot.adapters.telegram.controllers.finance_draft_text_input import (
    FinanceDraftTextInputController,
    FinanceDraftTextInputReceiptSnapshot,
    FinanceDraftTextInputSessionUseCases,
    TelegramFinanceDraftTextInputContext,
)
from finbot.adapters.telegram.controllers.finance_queries import (
    FinanceQueryController,
    TelegramQueryContext,
    TelegramQueryReceipt,
)
from finbot.adapters.telegram.controllers.local_ai import (
    LocalAiDraftController,
    LocalAiDraftOutcome,
    LocalAiDraftReceiptSnapshot,
    LocalAiDraftSessionUseCases,
    local_ai_notice_text,
)
from finbot.adapters.telegram.controllers.main_menu import MainMenuController
from finbot.adapters.telegram.controllers.ocr_images import (
    OcrImageController,
    OcrImageReceiptSnapshot,
    OcrImageSessionUseCases,
    TelegramOcrImageContext,
)
from finbot.adapters.telegram.controllers.ocr_queue import (
    OcrQueueOperation,
    OcrQueueReceiptSnapshot,
    OcrQueueSessionUseCases,
)
from finbot.adapters.telegram.controllers.plain_drafts import (
    PlainDraftOperation,
    PlainDraftReceiptSnapshot,
    PlainDraftSessionUseCases,
)
from finbot.adapters.telegram.controllers.recurring import TelegramRecurringController
from finbot.adapters.telegram.controllers.settings_mutations import (
    SettingsMutationController,
    SettingsMutationSessionUseCases,
    TelegramSettingsMutationContext,
)
from finbot.adapters.telegram.controllers.settings_queries import (
    SettingsQueryController,
    TelegramSettingsQueryContext,
    TelegramSettingsQueryReceipt,
)
from finbot.adapters.telegram.controllers.settings_text_input import (
    SettingsTextInputController,
    SettingsTextInputReceiptSnapshot,
    SettingsTextInputSessionUseCases,
    SettingsTextPresentationContextReader,
    TelegramSettingsTextInputContext,
)
from finbot.adapters.telegram.controllers.transaction_draft_navigation import (
    TelegramTransactionDraftNavigationContext,
    TransactionDraftNavigationController,
    TransactionDraftNavigationReceiptSnapshot,
    TransactionDraftNavigationSessionUseCases,
)
from finbot.adapters.telegram.controllers.transaction_draft_selection import (
    TelegramTransactionDraftSelectionContext,
    TransactionDraftSelectionController,
    TransactionDraftSelectionReceiptSnapshot,
    TransactionDraftSelectionSessionUseCases,
)
from finbot.adapters.telegram.controllers.transaction_edit_ingress import (
    TelegramTransactionEditContext,
    TransactionEditIngressController,
    TransactionEditReceiptSnapshot,
    TransactionEditSessionUseCases,
)
from finbot.adapters.telegram.controllers.transaction_edit_text_input import (
    TelegramTransactionEditTextInputContext,
    TransactionEditTextInputController,
    TransactionEditTextInputReceiptSnapshot,
    TransactionEditTextInputSessionUseCases,
    TransactionEditTextPresentationContextReader,
)
from finbot.adapters.telegram.controllers.transaction_lifecycle import (
    TelegramTransactionLifecycleContext,
    TransactionLifecycleController,
    TransactionLifecycleOperation,
    TransactionLifecycleReceiptSnapshot,
    TransactionLifecycleSessionUseCases,
)
from finbot.adapters.telegram.controllers.undo import (
    TelegramUndoContext,
    UndoController,
    UndoReceiptSnapshot,
    UndoSessionUseCases,
)
from finbot.adapters.telegram.delivery import ReliableDeliveryMiddleware
from finbot.adapters.telegram.draft_preparation import DeterministicQuickDraftParser
from finbot.adapters.telegram.executor import (
    TelegramMutationExecutor,
    TelegramMutationRequest,
    TelegramMutationSession,
)
from finbot.adapters.telegram.export_delivery import (
    CsvExportDelivery,
    enqueue_csv_export_receipt,
)
from finbot.adapters.telegram.input_delivery import (
    BoundedTelegramImageDownloader,
    OcrImageDirectDelivery,
    ProcessedTelegramUpdateReader,
    TelegramTextInputDelivery,
)
from finbot.adapters.telegram.main_menu_delivery import (
    MainMenuDirectDelivery,
    enqueue_main_menu_receipt,
)
from finbot.adapters.telegram.middlewares.auth import OwnerOnlyMiddleware
from finbot.adapters.telegram.miniapp_menu import MiniAppMenuConfigurator
from finbot.adapters.telegram.outbox import (
    TelegramResponseOutboxMiddleware,
    deliver_pending_responses,
)
from finbot.adapters.telegram.polling import run as run_polling
from finbot.adapters.telegram.presenters import (
    transaction_snapshot_card,
)
from finbot.adapters.telegram.presenters import (
    wizard_summary as render_wizard_summary,
)
from finbot.adapters.telegram.routers import (
    CatalogCallbackRouter,
    CsvExportRequestDefaults,
    CsvExportRouter,
    DraftInteractionContexts,
    DraftInteractionControllers,
    DraftInteractionDelivery,
    DraftInteractionRenderers,
    DraftInteractionRouter,
    FallbackCallbackHandlers,
    FinanceQueryCallbackHandlers,
    MainMenuRequestDefaults,
    MainMenuRouter,
    OcrImageRouter,
    ReadyCallbackHandlers,
    TextInputContextFactories,
    TextInputRouter,
    TransactionLifecycleContexts,
    TransactionLifecycleControllers,
    TransactionLifecycleDeliveries,
    TransactionLifecycleRouter,
    register_late_callback_fallbacks,
    register_ready_callbacks,
    reject_legacy_draft_callback,
    reject_stale_callback,
)
from finbot.adapters.telegram.routers.bank_imports import TelegramBankImportRouter
from finbot.adapters.telegram.routers.draft_interactions import DraftPresentationBinder
from finbot.adapters.telegram.routers.finance_messages import FinanceMessageRouter
from finbot.adapters.telegram.routers.local_ai import LocalAiRouter
from finbot.adapters.telegram.routers.recurring import RecurringRouter
from finbot.adapters.telegram.routers.settings import (
    SettingsRouter,
    register_settings_routes,
)
from finbot.adapters.telegram.ui import (
    Choice,
    account_keyboard,
    category_keyboard,
    draft_conflict_keyboard,
    edit_date_keyboard,
    edit_input_keyboard,
    edit_keyboard,
    restore_keyboard,
    resume_draft_keyboard,
    review_date_keyboard,
    review_type_keyboard,
    settings_account_keyboard,
    settings_accounts_keyboard,
    settings_category_keyboard,
    settings_category_list_keyboard,
    settings_keyboard,
    settings_text_input_keyboard,
    transaction_keyboard,
    wizard_confirm_keyboard,
    wizard_date_keyboard,
    wizard_description_keyboard,
    wizard_input_keyboard,
    wizard_type_keyboard,
)
from finbot.application.draft_ingress import (
    DraftIngressOperation,
    DraftIngressStatus,
)
from finbot.application.draft_navigation import (
    DraftNavigationAction as ApplicationDraftNavigationAction,
)
from finbot.application.draft_navigation import (
    DraftNavigationChoices,
    DraftNavigationResult,
    DraftNavigationStatus,
)
from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    DraftRef,
    OcrImageIngressStatus,
    OcrQueueStatus,
    OwnerSnapshot,
)
from finbot.application.finance_draft_text_input import (
    FinanceDraftTextInputError,
    FinanceDraftTextInputStatus,
)
from finbot.application.local_ai import LocalAiSuggestionProvider
from finbot.application.ocr import (
    ImageTextExtractor,
)
from finbot.application.settings_text_input import (
    SettingsTextInputError,
    SettingsTextInputOperation,
    SettingsTextInputStatus,
)
from finbot.application.transaction_draft_navigation import (
    TransactionDraftNavigationAction,
    TransactionDraftNavigationChoices,
    TransactionDraftNavigationResult,
)
from finbot.application.transaction_draft_selection import (
    TransactionDraftSelectionAction,
    TransactionDraftSelectionStatus,
)
from finbot.application.transaction_edit_ingress import TransactionEditIngressStatus
from finbot.application.transaction_edit_text_input import (
    TransactionEditTextInputError,
    TransactionEditTextInputStatus,
)
from finbot.application.undo import UndoAction
from finbot.application.use_cases.bank_imports import BankImportPreparer, BankImportUseCases
from finbot.application.use_cases.catalogs import CatalogUseCases
from finbot.application.use_cases.draft_conflicts import (
    PrepareDraftConflictReplacement,
    ResolveDraftConflict,
)
from finbot.application.use_cases.draft_ingress import DraftIngressUseCases
from finbot.application.use_cases.draft_navigation import DraftNavigationUseCases
from finbot.application.use_cases.draft_preparation import (
    PrepareParsedDraft,
    PrepareQuickDraft,
    SystemDraftPreparationClock,
)
from finbot.application.use_cases.draft_rules import DraftRuleUseCases
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.application.use_cases.finance_draft_text_input import FinanceDraftTextInputUseCase
from finbot.application.use_cases.local_ai import CreateLocalAiDraft, SuggestLocalTransaction
from finbot.application.use_cases.ocr_queue import (
    CancelOcrQueue,
    ConfirmOcrQueueItem,
    ProcessOcrImage,
    SharedOcrDraftPreparer,
    SkipOcrQueueItem,
)
from finbot.application.use_cases.queries import (
    GetOwnerSettings,
    GetTransaction,
    ListAccounts,
    ListCategories,
)
from finbot.application.use_cases.settings_mutations import (
    BeginSettingsInput,
    ChangeSettingsTimezone,
)
from finbot.application.use_cases.settings_text_input import SettingsTextInputUseCase
from finbot.application.use_cases.transaction_draft_navigation import (
    TransactionDraftNavigationUseCases,
)
from finbot.application.use_cases.transaction_draft_selection import (
    TransactionDraftSelectionUseCases,
)
from finbot.application.use_cases.transaction_edit_ingress import BeginTransactionEdit
from finbot.application.use_cases.transaction_edit_text_input import (
    TransactionEditTextInputUseCase,
)
from finbot.application.use_cases.transactions import TransactionUseCases
from finbot.application.use_cases.undo import UndoLastAction
from finbot.config import Settings
from finbot.domain.transactions import TransactionType

PAGE_SIZE = 5


def _currency_code(value: object, fallback: str) -> str:
    code = str(value or fallback).strip().upper()
    if len(code) != 3 or not code.isascii() or not code.isalpha():
        raise ValueError("Некорректная валюта счёта")
    return code


def wizard_summary(payload: dict[str, object], fallback_currency: str, timezone: str) -> str:
    """Render the currency of the selected account, with a pre-selection fallback."""
    return _ocr_batch_header(payload) + render_wizard_summary(
        payload, _currency_code(payload.get("currency"), fallback_currency), timezone
    )


@dataclass(frozen=True, slots=True)
class _CallbackMutationReceipt:
    text: str = field(repr=False)
    reply_markup: InlineKeyboardMarkup | None = field(repr=False)


def _telegram_mutation_request(
    settings: Settings,
    update_id: int | None,
    message: Message,
) -> TelegramMutationRequest:
    return TelegramMutationRequest(
        update_id=update_id,
        owner_telegram_user_id=settings.owner_telegram_user_id,
        chat_id=message.chat.id,
        locale=settings.default_locale,
        timezone=settings.default_timezone,
        currency=settings.default_currency,
    )


def _telegram_query_context(
    settings: Settings,
    update_id: int | None,
    message: Message,
) -> TelegramQueryContext:
    return TelegramQueryContext(
        request=_telegram_mutation_request(settings, update_id, message),
        message_id=message.message_id,
    )


def _telegram_initial_query_context(
    settings: Settings,
    update_id: int | None,
    message: Message,
) -> TelegramQueryContext:
    return TelegramQueryContext(
        request=_telegram_mutation_request(settings, update_id, message),
        message_id=None,
    )


def _telegram_settings_query_context(
    settings: Settings,
    update_id: int | None,
    message: Message,
    *,
    edit_message: bool,
) -> TelegramSettingsQueryContext:
    return TelegramSettingsQueryContext(
        _telegram_mutation_request(settings, update_id, message),
        message_id=message.message_id if edit_message else None,
    )


def _telegram_settings_mutation_context(
    settings: Settings,
    update_id: int | None,
    message: Message,
) -> TelegramSettingsMutationContext:
    return TelegramSettingsMutationContext(
        _telegram_mutation_request(settings, update_id, message),
        message_id=message.message_id,
    )


def _telegram_catalog_context(
    settings: Settings,
    update_id: int | None,
    message: Message,
) -> TelegramCatalogContext:
    return TelegramCatalogContext(
        request=_telegram_mutation_request(settings, update_id, message),
        message_id=message.message_id,
    )


def _telegram_draft_context(
    settings: Settings,
    update_id: int | None,
    message: Message,
) -> TelegramDraftContext:
    return TelegramDraftContext(
        request=_telegram_mutation_request(settings, update_id, message),
        message_id=message.message_id,
    )


def _telegram_draft_navigation_context(
    settings: Settings,
    update_id: int | None,
    message: Message,
) -> TelegramDraftNavigationContext:
    return TelegramDraftNavigationContext(
        request=_telegram_mutation_request(settings, update_id, message),
        message_id=message.message_id,
    )


def _telegram_draft_rule_context(
    settings: Settings,
    update_id: int | None,
    message: Message,
) -> TelegramDraftRuleContext:
    return TelegramDraftRuleContext(
        request=_telegram_mutation_request(settings, update_id, message),
        message_id=message.message_id,
    )


def _telegram_draft_completion_context(
    settings: Settings,
    update_id: int | None,
    message: Message,
) -> TelegramDraftCompletionContext:
    return TelegramDraftCompletionContext(
        request=_telegram_mutation_request(settings, update_id, message),
        message_id=message.message_id,
    )


def _telegram_draft_ingress_context(
    settings: Settings,
    update_id: int | None,
    message: Message,
    *,
    edit_message: bool,
    history_page: int | None = None,
) -> TelegramDraftIngressContext:
    return TelegramDraftIngressContext(
        request=_telegram_mutation_request(settings, update_id, message),
        message_id=message.message_id if edit_message else None,
        history_page=history_page,
    )


def _telegram_finance_draft_text_input_context(
    settings: Settings,
    update_id: int | None,
    message: Message,
) -> TelegramFinanceDraftTextInputContext:
    return TelegramFinanceDraftTextInputContext(
        request=_telegram_mutation_request(settings, update_id, message),
        input_message_id=message.message_id,
    )


def _telegram_transaction_draft_navigation_context(
    settings: Settings,
    update_id: int | None,
    message: Message,
) -> TelegramTransactionDraftNavigationContext:
    return TelegramTransactionDraftNavigationContext(
        request=_telegram_mutation_request(settings, update_id, message),
        message_id=message.message_id,
    )


def _telegram_transaction_draft_selection_context(
    settings: Settings,
    update_id: int | None,
    message: Message,
) -> TelegramTransactionDraftSelectionContext:
    return TelegramTransactionDraftSelectionContext(
        request=_telegram_mutation_request(settings, update_id, message),
        message_id=message.message_id,
    )


def _telegram_transaction_edit_context(
    settings: Settings,
    update_id: int | None,
    message: Message,
    history_page: int,
) -> TelegramTransactionEditContext:
    return TelegramTransactionEditContext(
        request=_telegram_mutation_request(settings, update_id, message),
        message_id=message.message_id,
        history_page=history_page,
    )


def _telegram_transaction_lifecycle_context(
    settings: Settings,
    update_id: int | None,
    message: Message,
    history_page: int | None,
) -> TelegramTransactionLifecycleContext:
    return TelegramTransactionLifecycleContext(
        request=_telegram_mutation_request(settings, update_id, message),
        message_id=message.message_id,
        history_page=history_page,
    )


def _telegram_transaction_edit_text_input_context(
    settings: Settings,
    update_id: int | None,
    message: Message,
) -> TelegramTransactionEditTextInputContext:
    return TelegramTransactionEditTextInputContext(
        request=_telegram_mutation_request(settings, update_id, message),
        input_message_id=message.message_id,
    )


def _telegram_settings_text_input_context(
    settings: Settings,
    update_id: int | None,
    message: Message,
) -> TelegramSettingsTextInputContext:
    return TelegramSettingsTextInputContext(
        request=_telegram_mutation_request(settings, update_id, message),
        input_message_id=message.message_id,
    )


def _telegram_ocr_image_context(
    settings: Settings,
    update_id: int | None,
    message: Message,
    content: bytes,
    mime_type: str,
) -> TelegramOcrImageContext:
    return TelegramOcrImageContext(
        _telegram_mutation_request(settings, update_id, message),
        content,
        mime_type,
    )


def _catalog_session_use_cases(session: TelegramMutationSession) -> CatalogSessionUseCases:
    reader = SqlAlchemyQueryRepository(session)
    return CatalogSessionUseCases(
        catalogs=CatalogUseCases(SqlAlchemyCatalogRepository(session)),
        list_accounts=ListAccounts(reader),
        list_categories=ListCategories(reader),
        get_owner_settings=GetOwnerSettings(reader),
    )


def _draft_session_use_cases(session: TelegramMutationSession) -> DraftSessionUseCases:
    reader = SqlAlchemyQueryRepository(session)
    return DraftSessionUseCases(
        drafts=DraftUseCases(SqlAlchemyDraftRepository(session)),
        get_owner_settings=GetOwnerSettings(reader),
        list_accounts=ListAccounts(reader),
    )


def _draft_navigation_session_use_cases(
    session: TelegramMutationSession,
) -> DraftNavigationSessionUseCases:
    reader = SqlAlchemyQueryRepository(session)
    return DraftNavigationSessionUseCases(
        navigation=DraftNavigationUseCases(
            DraftUseCases(SqlAlchemyDraftRepository(session)),
            GetOwnerSettings(reader),
            ListAccounts(reader),
            ListCategories(reader),
            SqlAlchemyDraftNavigationCatalogRepository(session),
        )
    )


def _draft_rule_session_use_cases(session: TelegramMutationSession) -> DraftRuleSessionUseCases:
    return DraftRuleSessionUseCases(
        rules=DraftRuleUseCases(DraftUseCases(SqlAlchemyDraftRepository(session))),
        get_owner_settings=GetOwnerSettings(SqlAlchemyQueryRepository(session)),
    )


def _ocr_queue_session_use_cases(session: TelegramMutationSession) -> OcrQueueSessionUseCases:
    reader = SqlAlchemyQueryRepository(session)
    preparation_repository = SqlAlchemyDraftPreparationRepository(session)
    preparer = SharedOcrDraftPreparer(
        PrepareParsedDraft(
            reader,
            preparation_repository,
            preparation_repository,
            SystemDraftPreparationClock(),
        )
    )
    drafts = SqlAlchemyDraftRepository(session)
    commands = SqlAlchemyOcrQueueCommandRepository(session)

    async def presentation_is_current(
        owner_id: UUID,
        expected: DraftRef,
        message_id: int,
    ) -> bool:
        return await lock_telegram_draft_presentation_message(
            session,
            owner_id,
            expected,
            message_id,
        )

    return OcrQueueSessionUseCases(
        confirm_current=ConfirmOcrQueueItem(commands, drafts, preparer),
        skip_current=SkipOcrQueueItem(commands, drafts, preparer),
        cancel_queue=CancelOcrQueue(commands, drafts),
        get_owner_settings=GetOwnerSettings(reader),
        list_accounts=ListAccounts(reader),
        list_categories=ListCategories(reader),
        presentation_is_current=presentation_is_current,
    )


def _ocr_image_session_use_cases(
    session: TelegramMutationSession,
    extractor: ImageTextExtractor,
) -> OcrImageSessionUseCases:
    reader = SqlAlchemyQueryRepository(session)
    preparation = SqlAlchemyDraftPreparationRepository(session)
    parsed = PrepareParsedDraft(
        reader,
        preparation,
        preparation,
        SystemDraftPreparationClock(),
    )
    return OcrImageSessionUseCases(
        process_image=ProcessOcrImage(
            extractor,
            SqlAlchemyOcrQueueCommandRepository(session),
            SharedOcrDraftPreparer(parsed),
            DraftUseCases(SqlAlchemyDraftRepository(session)),
        ),
        get_owner_settings=GetOwnerSettings(reader),
        list_accounts=ListAccounts(reader),
        list_categories=ListCategories(reader),
    )


def _transaction_use_cases(session: AsyncSession) -> TransactionUseCases:
    return TransactionUseCases(
        SqlAlchemyTransactionCommandRepository(session),
        SqlAlchemyDraftRepository(session),
    )


def _undo_session_use_cases(session: TelegramMutationSession) -> UndoSessionUseCases:
    return UndoSessionUseCases(
        undo_last=UndoLastAction(SqlAlchemyUndoRepository(session)),
        get_owner_settings=GetOwnerSettings(SqlAlchemyQueryRepository(session)),
    )


def _plain_draft_session_use_cases(session: TelegramMutationSession) -> PlainDraftSessionUseCases:
    return PlainDraftSessionUseCases(
        transactions=_transaction_use_cases(session),
        drafts=DraftUseCases(SqlAlchemyDraftRepository(session)),
        get_owner_settings=GetOwnerSettings(SqlAlchemyQueryRepository(session)),
    )


def _draft_completion_session_use_cases(
    session: TelegramMutationSession,
) -> DraftCompletionSessionUseCases:
    return DraftCompletionSessionUseCases(
        plain=_plain_draft_session_use_cases(session),
        ocr_queue=_ocr_queue_session_use_cases(session),
    )


def _parsed_draft_preparer(session: TelegramMutationSession) -> PrepareParsedDraft:
    reader = SqlAlchemyQueryRepository(session)
    preparation_repository = SqlAlchemyDraftPreparationRepository(session)
    return PrepareParsedDraft(
        reader,
        preparation_repository,
        preparation_repository,
        SystemDraftPreparationClock(),
    )


def _quick_draft_preparer(session: TelegramMutationSession) -> PrepareQuickDraft:
    return PrepareQuickDraft(
        SqlAlchemyQueryRepository(session),
        DeterministicQuickDraftParser(),
        _parsed_draft_preparer(session),
    )


def _local_ai_draft_session_use_cases(
    session: TelegramMutationSession,
) -> LocalAiDraftSessionUseCases:
    reader = SqlAlchemyQueryRepository(session)
    return LocalAiDraftSessionUseCases(
        create_draft=CreateLocalAiDraft(
            DraftUseCases(SqlAlchemyDraftRepository(session)),
            _parsed_draft_preparer(session),
            GetOwnerSettings(reader),
        )
    )


def _local_ai_provider(settings: Settings) -> LocalAiSuggestionProvider:
    if not settings.local_ai_enabled:
        return DisabledLocalAiSuggestionProvider()
    if settings.local_ai_endpoint is None or settings.local_ai_model is None:
        raise RuntimeError("Enabled local AI configuration is incomplete")
    return OllamaLocalAiSuggestionProvider(
        settings.local_ai_endpoint,
        settings.local_ai_model,
    )


def _draft_ingress_session_use_cases(
    session: TelegramMutationSession,
) -> DraftIngressSessionUseCases:
    reader = SqlAlchemyQueryRepository(session)
    return DraftIngressSessionUseCases(
        ingress=DraftIngressUseCases(
            DraftUseCases(SqlAlchemyDraftRepository(session)),
            SqlAlchemyTransactionCommandRepository(session),
            GetOwnerSettings(reader),
            quick_drafts=_quick_draft_preparer(session),
        )
    )


def _draft_conflict_session_use_cases(
    session: TelegramMutationSession,
) -> DraftConflictSessionUseCases:
    reader = SqlAlchemyQueryRepository(session)
    repository = SqlAlchemyDraftRepository(session)
    return DraftConflictSessionUseCases(
        resolve=ResolveDraftConflict(
            repository,
            PrepareDraftConflictReplacement(
                _quick_draft_preparer(session),
                SqlAlchemyDraftConflictReplacementTargets(session),
            ),
        ),
        get_owner_settings=GetOwnerSettings(reader),
        list_accounts=ListAccounts(reader),
        list_categories=ListCategories(reader),
        get_transaction=GetTransaction(reader),
    )


def _finance_draft_text_input_session_use_cases(
    session: TelegramMutationSession,
) -> FinanceDraftTextInputSessionUseCases:
    reader = SqlAlchemyQueryRepository(session)
    return FinanceDraftTextInputSessionUseCases(
        text_input=FinanceDraftTextInputUseCase(
            DraftUseCases(SqlAlchemyDraftRepository(session)),
            GetOwnerSettings(reader),
            ListAccounts(reader),
            ListCategories(reader),
            SqlAlchemyFinanceDraftTextInputCatalogRepository(session),
        )
    )


def _settings_text_input_session_use_cases(
    session: TelegramMutationSession,
) -> SettingsTextInputSessionUseCases:
    targets = SqlAlchemySettingsTextInputRepository(session)
    return SettingsTextInputSessionUseCases(
        text_input=SettingsTextInputUseCase(
            targets,
            CatalogUseCases(SqlAlchemyCatalogRepository(session)),
            DraftUseCases(SqlAlchemyDraftRepository(session)),
        )
    )


def _settings_mutation_session_use_cases(
    session: TelegramMutationSession,
) -> SettingsMutationSessionUseCases:
    repository = SqlAlchemySettingsMutationRepository(session)
    return SettingsMutationSessionUseCases(
        begin_input=BeginSettingsInput(
            repository,
            DraftUseCases(SqlAlchemyDraftRepository(session)),
        ),
        change_timezone=ChangeSettingsTimezone(repository),
    )


def _transaction_draft_navigation_session_use_cases(
    session: TelegramMutationSession,
) -> TransactionDraftNavigationSessionUseCases:
    reader = SqlAlchemyQueryRepository(session)
    return TransactionDraftNavigationSessionUseCases(
        navigation=TransactionDraftNavigationUseCases(
            DraftUseCases(SqlAlchemyDraftRepository(session)),
            GetOwnerSettings(reader),
            GetTransaction(reader),
            ListAccounts(reader),
            ListCategories(reader),
        )
    )


def _transaction_draft_selection_session_use_cases(
    session: TelegramMutationSession,
) -> TransactionDraftSelectionSessionUseCases:
    return TransactionDraftSelectionSessionUseCases(
        selections=TransactionDraftSelectionUseCases(
            DraftUseCases(SqlAlchemyDraftRepository(session)),
            GetOwnerSettings(SqlAlchemyQueryRepository(session)),
            SqlAlchemyTransactionDraftSelectionRepository(session),
        )
    )


def _transaction_edit_session_use_cases(
    session: TelegramMutationSession,
) -> TransactionEditSessionUseCases:
    reader = SqlAlchemyQueryRepository(session)
    return TransactionEditSessionUseCases(
        begin=BeginTransactionEdit(
            DraftUseCases(SqlAlchemyDraftRepository(session)),
            GetOwnerSettings(reader),
            SqlAlchemyTransactionEditTargetReader(session),
        )
    )


def _transaction_lifecycle_session_use_cases(
    session: TelegramMutationSession,
) -> TransactionLifecycleSessionUseCases:
    return TransactionLifecycleSessionUseCases(
        transactions=_transaction_use_cases(session),
        get_owner_settings=GetOwnerSettings(SqlAlchemyQueryRepository(session)),
    )


def _transaction_edit_text_input_session_use_cases(
    session: TelegramMutationSession,
) -> TransactionEditTextInputSessionUseCases:
    drafts = DraftUseCases(SqlAlchemyDraftRepository(session))
    return TransactionEditTextInputSessionUseCases(
        text_input=TransactionEditTextInputUseCase(
            SqlAlchemyTransactionEditTextTargetRepository(session),
            _transaction_use_cases(session),
            drafts,
        )
    )


async def _replace_message(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> int:
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
        return message.message_id
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return message.message_id
        sent = await message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
        return sent.message_id


async def _deliver_untracked_finance_query_callback(
    message: Message,
    receipt: TelegramQueryReceipt,
) -> None:
    await _replace_message(message, receipt.text, receipt.reply_markup)


async def _deliver_untracked_finance_message(
    sessions: async_sessionmaker[AsyncSession],
    message: Message,
    receipt: TelegramQueryReceipt,
) -> None:
    sent = await message.answer(
        receipt.text,
        parse_mode=receipt.parse_mode,
        reply_markup=receipt.reply_markup,
    )
    if receipt.suspended_draft is None:
        return
    async with sessions() as session, session.begin():
        await _bind_draft_presentation_preserving_context(
            session,
            receipt.suspended_draft,
            chat_id=message.chat.id,
            message_id=sent.message_id,
        )


async def _prior_draft_presentation_context(
    session: AsyncSession,
    draft: DraftRef,
) -> TelegramDraftPresentationContext:
    if draft.revision <= 1:
        return TelegramDraftPresentationContext()
    context = await lock_telegram_draft_presentation_context_by_revision(
        session,
        draft.draft_id,
        draft.revision - 1,
    )
    return context or TelegramDraftPresentationContext()


async def _current_or_prior_draft_presentation_context(
    session: AsyncSession,
    draft: DraftRef,
) -> TelegramDraftPresentationContext:
    """Preserve context for both unchanged and newly revised draft receipts."""

    context = await lock_telegram_draft_presentation_context_by_revision(
        session,
        draft.draft_id,
        draft.revision,
    )
    if context is not None:
        return context
    return await _prior_draft_presentation_context(session, draft)


async def _bind_draft_presentation_preserving_context(
    session: AsyncSession,
    draft: DraftRef,
    *,
    chat_id: int,
    message_id: int,
    history_page: int | None = None,
    pending_history_page: int | None = None,
    override_context: bool = False,
) -> None:
    if not override_context:
        context = await _prior_draft_presentation_context(session, draft)
        history_page = context.history_page
        pending_history_page = context.pending_history_page
    await bind_telegram_draft_presentation(
        session,
        draft_id=draft.draft_id,
        draft_revision=draft.revision,
        chat_id=chat_id,
        message_id=message_id,
        history_page=history_page,
        pending_history_page=pending_history_page,
    )


async def _bind_untracked_draft_interaction_presentation(
    sessions: async_sessionmaker[AsyncSession],
    message: Message,
    draft: DraftRef,
    delivered_message_id: int,
    *,
    history_page: int | None = None,
    override_context: bool = False,
) -> None:
    """Bind a direct post-commit callback receipt to its exact draft revision."""

    async with sessions() as session, session.begin():
        await _bind_draft_presentation_preserving_context(
            session,
            draft,
            chat_id=message.chat.id,
            message_id=delivered_message_id,
            history_page=history_page,
            override_context=override_context,
        )


async def _deliver_untracked_settings_query_receipt(
    sessions: async_sessionmaker[AsyncSession],
    message: Message,
    receipt: TelegramSettingsQueryReceipt,
) -> None:
    if receipt.message_id is None:
        delivered = await message.answer(
            receipt.text,
            parse_mode=receipt.parse_mode,
            reply_markup=receipt.reply_markup,
        )
        delivered_message_id = delivered.message_id
    else:
        delivered_message_id = await _replace_message(
            message,
            receipt.text,
            receipt.reply_markup,
        )
    if receipt.draft_ref is None:
        return
    async with sessions() as session, session.begin():
        context = await _current_or_prior_draft_presentation_context(
            session,
            receipt.draft_ref,
        )
        await _bind_draft_presentation_preserving_context(
            session,
            receipt.draft_ref,
            chat_id=message.chat.id,
            message_id=delivered_message_id,
            history_page=context.history_page,
            pending_history_page=context.pending_history_page,
            override_context=True,
        )


def _queue_callback_receipt(
    session: AsyncSession,
    settings: Settings,
    update_id: int | None,
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    draft_id: UUID | None = None,
    draft_revision: int | None = None,
) -> bool:
    if update_id is None:
        return False
    queue_edit_message_text(
        session,
        update_id=update_id,
        owner_telegram_user_id=settings.owner_telegram_user_id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        text=text,
        parse_mode="HTML",
        reply_markup=reply_markup,
        draft_id=draft_id,
        draft_revision=draft_revision,
    )
    return True


async def _enqueue_tracked_callback_receipt(
    session: AsyncSession,
    request: TelegramMutationRequest,
    message_id: int,
    receipt: _CallbackMutationReceipt,
) -> None:
    update_id = request.update_id
    if update_id is None:
        raise RuntimeError("tracked receipt requires an update id")
    queue_edit_message_text(
        session,
        update_id=update_id,
        owner_telegram_user_id=request.owner_telegram_user_id,
        chat_id=request.chat_id,
        message_id=message_id,
        text=receipt.text,
        parse_mode="HTML",
        reply_markup=receipt.reply_markup,
    )


def _transaction_lifecycle_receipt(
    receipt: TransactionLifecycleReceiptSnapshot,
) -> _CallbackMutationReceipt:
    transaction = receipt.mutation.transaction
    history_page = receipt.history_page or 0
    if receipt.operation is TransactionLifecycleOperation.DELETE:
        return _CallbackMutationReceipt(
            transaction_snapshot_card(
                transaction,
                receipt.owner.timezone,
                title="🗑 Операция удалена",
            ),
            restore_keyboard(
                transaction.transaction_id,
                transaction.version,
                history_page,
            ),
        )
    return _CallbackMutationReceipt(
        transaction_snapshot_card(
            transaction,
            receipt.owner.timezone,
            title="✅ Операция восстановлена",
        ),
        transaction_keyboard(
            transaction.transaction_id,
            transaction.version,
            history_page,
        ),
    )


async def _enqueue_transaction_lifecycle_receipt(
    session: TelegramMutationSession,
    request: TelegramMutationRequest,
    receipt: TransactionLifecycleReceiptSnapshot,
) -> None:
    await _enqueue_tracked_callback_receipt(
        session,
        request,
        receipt.message_id,
        _transaction_lifecycle_receipt(receipt),
    )


async def _deliver_untracked_transaction_lifecycle(
    message: Message,
    receipt: TransactionLifecycleReceiptSnapshot,
) -> None:
    rendered = _transaction_lifecycle_receipt(receipt)
    await _replace_message(message, rendered.text, rendered.reply_markup)


async def _enqueue_finance_query_receipt(
    session: TelegramMutationSession,
    request: TelegramMutationRequest,
    receipt: TelegramQueryReceipt,
) -> None:
    update_id = request.update_id
    if update_id is None:
        raise RuntimeError("tracked receipt requires an update id")
    if receipt.message_id is None:
        context = (
            await _prior_draft_presentation_context(session, receipt.suspended_draft)
            if receipt.suspended_draft is not None
            else TelegramDraftPresentationContext()
        )
        queue_send_message(
            session,
            update_id=update_id,
            owner_telegram_user_id=request.owner_telegram_user_id,
            chat_id=request.chat_id,
            text=receipt.text,
            parse_mode=receipt.parse_mode,
            reply_markup=receipt.reply_markup,
            draft_id=(
                receipt.suspended_draft.draft_id if receipt.suspended_draft is not None else None
            ),
            draft_revision=(
                receipt.suspended_draft.revision if receipt.suspended_draft is not None else None
            ),
            history_page=context.history_page,
            pending_history_page=context.pending_history_page,
        )
        return
    queue_edit_message_text(
        session,
        update_id=update_id,
        owner_telegram_user_id=request.owner_telegram_user_id,
        chat_id=request.chat_id,
        message_id=receipt.message_id,
        text=receipt.text,
        parse_mode=receipt.parse_mode,
        reply_markup=receipt.reply_markup,
    )


async def _enqueue_settings_query_receipt(
    session: TelegramMutationSession,
    request: TelegramMutationRequest,
    receipt: TelegramSettingsQueryReceipt,
) -> None:
    update_id = request.update_id
    if update_id is None:
        raise RuntimeError("tracked receipt requires an update id")
    presentation = (
        await _current_or_prior_draft_presentation_context(session, receipt.draft_ref)
        if receipt.draft_ref is not None
        else TelegramDraftPresentationContext()
    )
    draft_id = receipt.draft_ref.draft_id if receipt.draft_ref is not None else None
    draft_revision = receipt.draft_ref.revision if receipt.draft_ref is not None else None
    if receipt.message_id is None:
        queue_send_message(
            session,
            update_id=update_id,
            owner_telegram_user_id=request.owner_telegram_user_id,
            chat_id=request.chat_id,
            text=receipt.text,
            parse_mode=receipt.parse_mode,
            reply_markup=receipt.reply_markup,
            draft_id=draft_id,
            draft_revision=draft_revision,
            history_page=presentation.history_page,
            pending_history_page=presentation.pending_history_page,
        )
        return
    queue_edit_message_text(
        session,
        update_id=update_id,
        owner_telegram_user_id=request.owner_telegram_user_id,
        chat_id=request.chat_id,
        message_id=receipt.message_id,
        text=receipt.text,
        parse_mode=receipt.parse_mode,
        reply_markup=receipt.reply_markup,
        draft_id=draft_id,
        draft_revision=draft_revision,
        history_page=presentation.history_page,
        pending_history_page=presentation.pending_history_page,
    )


async def _enqueue_catalog_receipt(
    session: TelegramMutationSession,
    request: TelegramMutationRequest,
    receipt: CatalogReceiptSnapshot,
) -> None:
    update_id = request.update_id
    if update_id is None:
        raise RuntimeError("tracked receipt requires an update id")
    rendered = _catalog_callback_receipt(receipt)
    if receipt.message_id is None:
        queue_send_message(
            session,
            update_id=update_id,
            owner_telegram_user_id=request.owner_telegram_user_id,
            chat_id=request.chat_id,
            text=rendered.text,
            parse_mode="HTML",
            reply_markup=rendered.reply_markup,
        )
        return
    queue_edit_message_text(
        session,
        update_id=update_id,
        owner_telegram_user_id=request.owner_telegram_user_id,
        chat_id=request.chat_id,
        message_id=receipt.message_id,
        text=rendered.text,
        parse_mode="HTML",
        reply_markup=rendered.reply_markup,
    )


async def _enqueue_draft_receipt(
    session: TelegramMutationSession,
    request: TelegramMutationRequest,
    receipt: DraftSettingsReceiptSnapshot,
) -> None:
    update_id = request.update_id
    if update_id is None:
        raise RuntimeError("tracked receipt requires an update id")
    rendered = _draft_settings_receipt(receipt)
    if receipt.message_id is None:
        queue_send_message(
            session,
            update_id=update_id,
            owner_telegram_user_id=request.owner_telegram_user_id,
            chat_id=request.chat_id,
            text=rendered.text,
            parse_mode="HTML",
            reply_markup=rendered.reply_markup,
        )
        return
    queue_edit_message_text(
        session,
        update_id=update_id,
        owner_telegram_user_id=request.owner_telegram_user_id,
        chat_id=request.chat_id,
        message_id=receipt.message_id,
        text=rendered.text,
        parse_mode="HTML",
        reply_markup=rendered.reply_markup,
    )


def _draft_navigation_receipt(
    receipt: DraftNavigationReceiptSnapshot,
) -> tuple[_CallbackMutationReceipt, DraftRef | None]:
    result = receipt.result
    if result.status is DraftNavigationStatus.CLOSED:
        return (
            _CallbackMutationReceipt(
                "<b>Быстрый ввод</b>\n\n"
                "Напишите операцию заново, например <code>1450 ресторан</code>.",
                None,
            ),
            None,
        )
    draft = result.draft
    if draft is None:  # pragma: no cover - DTO invariant
        raise RuntimeError("Updated draft navigation receipt has no draft")
    state = draft.state
    payload = dict(draft.payload)
    if state == "wizard_type":
        rendered = _CallbackMutationReceipt(
            "<b>Новая операция · 1/6</b>\n\nЭто расход или доход?",
            wizard_type_keyboard(draft.draft_id, draft.revision),
        )
    elif state == "wizard_amount":
        rendered = _CallbackMutationReceipt(
            "<b>Новая операция · 2/6</b>\n\nВведите сумму заново, например <code>1450</code>.",
            wizard_input_keyboard(draft.draft_id, draft.revision),
        )
    elif state == "review_amount":
        rendered = _CallbackMutationReceipt(
            "<b>Изменить сумму</b>\n\nВведите положительную сумму.",
            wizard_input_keyboard(draft.draft_id, draft.revision),
        )
    elif state == "review_type":
        rendered = _CallbackMutationReceipt(
            "<b>Изменить тип</b>\n\nЭто расход или доход?",
            review_type_keyboard(draft.draft_id, draft.revision),
        )
    elif state in {"wizard_category", "quick_category", "category_required", "review_category"}:
        choices = [
            Choice(item.category_id, item.name, item.emoji, item.version)
            for item in result.choices.categories
        ]
        text = {
            "wizard_category": "<b>Новая операция · 3/6</b>\n\nВыберите категорию:",
            "review_category": "<b>Изменить категорию</b>\n\nВыберите категорию:",
        }.get(state, "<b>Уточните категорию</b>\n\nВыберите категорию:")
        rendered = _CallbackMutationReceipt(
            text,
            category_keyboard(
                choices,
                allow_custom=payload.get("flow") != "bank_import",
                draft_id=draft.draft_id,
                revision=draft.revision,
            ),
        )
    elif state == "custom_category":
        rendered = _CallbackMutationReceipt(
            "<b>Своя категория</b>\n\nВведите короткое название, например «Питомцы».",
            wizard_input_keyboard(draft.draft_id, draft.revision),
        )
    elif state in {"wizard_account", "quick_account", "account_required", "review_account"}:
        choices = [
            Choice(item.account_id, item.name, version=item.version)
            for item in result.choices.accounts
        ]
        text = {
            "wizard_account": "<b>Новая операция · 4/6</b>\n\nНа какой счёт записать?",
            "review_account": "<b>Изменить счёт</b>\n\nВыберите счёт:",
        }.get(state, "<b>Уточните счёт</b>\n\nВыберите счёт:")
        rendered = _CallbackMutationReceipt(
            text,
            account_keyboard(choices, draft_id=draft.draft_id, revision=draft.revision),
        )
    elif state == "custom_account":
        rendered = _CallbackMutationReceipt(
            "<b>Свой счёт</b>\n\nВведите название, например «Наличные».",
            wizard_input_keyboard(draft.draft_id, draft.revision),
        )
    elif state == "wizard_date":
        rendered = _CallbackMutationReceipt(
            "<b>Новая операция · 5/6</b>\n\nКогда произошла операция?",
            wizard_date_keyboard(draft.draft_id, draft.revision),
        )
    elif state == "custom_date":
        rendered = _CallbackMutationReceipt(
            "<b>Новая операция · 5/6</b>\n\n"
            "Введите дату: <code>ДД.ММ</code> или <code>ДД.ММ.ГГГГ</code>.",
            wizard_input_keyboard(draft.draft_id, draft.revision),
        )
    elif state == "review_date":
        rendered = _CallbackMutationReceipt(
            "<b>Изменить дату</b>\n\nВыберите дату или введите свою:",
            review_date_keyboard(draft.draft_id, draft.revision),
        )
    elif state == "review_date_input":
        rendered = _CallbackMutationReceipt(
            "<b>Изменить дату</b>\n\nВведите <code>ДД.ММ</code> или <code>ДД.ММ.ГГГГ</code>.",
            wizard_input_keyboard(draft.draft_id, draft.revision),
        )
    elif state == "wizard_description":
        current = str(payload.get("description", "")).strip()
        current_text = f"\n\nСейчас: <i>{escape(current)}</i>" if current else ""
        prompt = (
            "<b>Комментарий</b>\n\nВведите комментарий. "
            "Отправьте дефис <code>-</code>, чтобы очистить."
            if result.action is ApplicationDraftNavigationAction.EDIT_DESCRIPTION
            else "<b>Новая операция · 6/6</b>\n\n"
            "Введите комментарий или нажмите «Без комментария»."
            f"{current_text}"
        )
        rendered = _CallbackMutationReceipt(
            prompt,
            wizard_description_keyboard(draft.draft_id, draft.revision),
        )
    elif state in {"wizard_confirm", "quick_confirm", "review"}:
        rendered = _CallbackMutationReceipt(
            wizard_summary(payload, result.owner.base_currency, result.owner.timezone),
            _review_keyboard(payload, draft.draft_id, draft.revision),
        )
    else:  # pragma: no cover - application transition matrix is closed
        raise RuntimeError("Unsupported draft navigation receipt state")
    return rendered, draft.ref


async def _enqueue_draft_navigation_receipt(
    session: TelegramMutationSession,
    request: TelegramMutationRequest,
    receipt: DraftNavigationReceiptSnapshot,
) -> None:
    update_id = request.update_id
    if update_id is None:
        raise RuntimeError("tracked receipt requires an update id")
    rendered, next_draft = _draft_navigation_receipt(receipt)
    context = (
        await _prior_draft_presentation_context(session, next_draft)
        if next_draft is not None
        else TelegramDraftPresentationContext()
    )
    queue_edit_message_text(
        session,
        update_id=update_id,
        owner_telegram_user_id=request.owner_telegram_user_id,
        chat_id=request.chat_id,
        message_id=receipt.message_id,
        text=rendered.text,
        parse_mode="HTML",
        reply_markup=rendered.reply_markup,
        draft_id=next_draft.draft_id if next_draft is not None else None,
        draft_revision=next_draft.revision if next_draft is not None else None,
        history_page=context.history_page,
        pending_history_page=context.pending_history_page,
    )


def _draft_rule_receipt(
    receipt: DraftRuleReceiptSnapshot,
) -> tuple[_CallbackMutationReceipt, DraftRef]:
    payload = dict(receipt.draft.payload)
    rendered = _CallbackMutationReceipt(
        wizard_summary(payload, receipt.owner.base_currency, receipt.owner.timezone),
        _review_keyboard(payload, receipt.draft.draft_id, receipt.draft.revision),
    )
    return rendered, receipt.draft.ref


async def _enqueue_draft_rule_receipt(
    session: TelegramMutationSession,
    request: TelegramMutationRequest,
    receipt: DraftRuleReceiptSnapshot,
) -> None:
    update_id = request.update_id
    if update_id is None:
        raise RuntimeError("tracked receipt requires an update id")
    rendered, draft = _draft_rule_receipt(receipt)
    context = await _prior_draft_presentation_context(session, draft)
    queue_edit_message_text(
        session,
        update_id=update_id,
        owner_telegram_user_id=request.owner_telegram_user_id,
        chat_id=request.chat_id,
        message_id=receipt.message_id,
        text=rendered.text,
        parse_mode="HTML",
        reply_markup=rendered.reply_markup,
        draft_id=draft.draft_id,
        draft_revision=draft.revision,
        history_page=context.history_page,
        pending_history_page=context.pending_history_page,
    )


def _draft_ingress_receipt(
    receipt: DraftIngressReceiptSnapshot,
) -> tuple[_CallbackMutationReceipt, DraftRef]:
    result = receipt.result
    draft = result.draft
    if result.status is DraftIngressStatus.BLOCKED_BANK_IMPORT:
        rendered = _CallbackMutationReceipt(
            "<b>Банковская строка уже на проверке</b>\n\n"
            "Сначала завершите или отмените её, затем начните другое действие.",
            None,
        )
    elif result.status is DraftIngressStatus.CONFLICT:
        subject = (
            "начать новую операцию"
            if result.operation is DraftIngressOperation.WIZARD
            else (
                "обработать новый текст"
                if result.operation is DraftIngressOperation.QUICK
                else (
                    "обработать предложение локального AI"
                    if result.operation is DraftIngressOperation.LOCAL_AI
                    else "повторить операцию"
                )
            )
        )
        rendered = _CallbackMutationReceipt(
            f"<b>Есть незавершённый ввод</b>\n\nПродолжите его или сбросьте, чтобы {subject}.",
            draft_conflict_keyboard(draft.draft_id, draft.revision),
        )
    elif result.operation is DraftIngressOperation.WIZARD:
        rendered = _CallbackMutationReceipt(
            "<b>Новая операция · 1/6</b>\n\nЭто расход или доход?",
            wizard_type_keyboard(draft.draft_id, draft.revision),
        )
    else:
        payload = dict(draft.payload)
        rendered = _CallbackMutationReceipt(
            wizard_summary(payload, result.owner.base_currency, result.owner.timezone),
            _review_keyboard(payload, draft.draft_id, draft.revision),
        )
    return rendered, receipt.draft_ref


async def _draft_ingress_receipt_with_catalogs(
    session: TelegramMutationSession,
    receipt: DraftIngressReceiptSnapshot,
) -> tuple[_CallbackMutationReceipt, DraftRef]:
    result = receipt.result
    draft = result.draft
    if (
        result.operation not in {DraftIngressOperation.QUICK, DraftIngressOperation.LOCAL_AI}
        or result.status is not DraftIngressStatus.STARTED
        or draft.state not in {"category_required", "account_required"}
    ):
        return _draft_ingress_receipt(receipt)

    reader = SqlAlchemyQueryRepository(session)
    choices = DraftNavigationChoices()
    if draft.state == "category_required":
        choices = DraftNavigationChoices(
            categories=await ListCategories(reader)(
                result.owner.owner_id,
                kind=str(draft.payload["type"]),
            )
        )
    else:
        choices = DraftNavigationChoices(accounts=await ListAccounts(reader)(result.owner.owner_id))
    navigation = DraftNavigationReceiptSnapshot(
        expected=DraftRef(draft.draft_id, max(draft.revision - 1, 1)),
        result=DraftNavigationResult(
            action=ApplicationDraftNavigationAction.BACK,
            status=DraftNavigationStatus.UPDATED,
            owner=result.owner,
            draft=draft,
            choices=choices,
        ),
        message_id=receipt.message_id or 1,
    )
    rendered, rendered_draft = _draft_navigation_receipt(navigation)
    if rendered_draft is None:  # pragma: no cover - these states always retain a draft
        raise RuntimeError("Quick draft ingress unexpectedly closed the draft")
    return rendered, rendered_draft


async def _enqueue_draft_ingress_receipt(
    session: TelegramMutationSession,
    request: TelegramMutationRequest,
    receipt: DraftIngressReceiptSnapshot,
) -> None:
    update_id = request.update_id
    if update_id is None:
        raise RuntimeError("tracked receipt requires an update id")
    rendered, draft = await _draft_ingress_receipt_with_catalogs(session, receipt)
    history_page: int | None = receipt.history_page
    pending_history_page: int | None = None
    if receipt.result.status is DraftIngressStatus.CONFLICT:
        prior_context = await lock_telegram_draft_presentation_context_by_revision(
            session,
            draft.draft_id,
            draft.revision - 1,
        )
        history_page = prior_context.history_page if prior_context is not None else None
        pending_history_page = receipt.history_page
    queue = queue_send_message if receipt.message_id is None else queue_edit_message_text
    queue(
        session,
        update_id=update_id,
        owner_telegram_user_id=request.owner_telegram_user_id,
        chat_id=request.chat_id,
        **({} if receipt.message_id is None else {"message_id": receipt.message_id}),
        text=rendered.text,
        parse_mode="HTML",
        reply_markup=rendered.reply_markup,
        draft_id=draft.draft_id,
        draft_revision=draft.revision,
        history_page=history_page,
        pending_history_page=pending_history_page,
    )


async def _enqueue_local_ai_receipt(
    session: TelegramMutationSession,
    request: TelegramMutationRequest,
    receipt: LocalAiDraftReceiptSnapshot,
) -> None:
    if receipt.outcome is LocalAiDraftOutcome.CREATED:
        if receipt.ingress is None:  # pragma: no cover - receipt invariant
            raise RuntimeError("Created local AI receipt has no draft")
        await _enqueue_draft_ingress_receipt(session, request, receipt.ingress)
        return
    update_id = request.update_id
    if update_id is None:
        raise RuntimeError("tracked receipt requires an update id")
    queue_send_message(
        session,
        update_id=update_id,
        owner_telegram_user_id=request.owner_telegram_user_id,
        chat_id=request.chat_id,
        text=local_ai_notice_text(receipt.outcome),
    )


async def _render_untracked_draft_ingress_receipt(
    sessions: async_sessionmaker[AsyncSession],
    receipt: DraftIngressReceiptSnapshot,
) -> tuple[_CallbackMutationReceipt, DraftRef]:
    async with sessions() as session:
        return await _draft_ingress_receipt_with_catalogs(session, receipt)


async def _bind_untracked_draft_ingress_presentation(
    sessions: async_sessionmaker[AsyncSession],
    receipt: DraftIngressReceiptSnapshot,
    draft: DraftRef,
    *,
    chat_id: int,
    message_id: int,
) -> None:
    async with sessions() as session, session.begin():
        history_page = receipt.history_page
        pending_history_page: int | None = None
        if receipt.result.status is DraftIngressStatus.CONFLICT:
            prior_context = await lock_telegram_draft_presentation_context_by_revision(
                session,
                draft.draft_id,
                draft.revision - 1,
            )
            history_page = prior_context.history_page if prior_context is not None else None
            pending_history_page = receipt.history_page
        await bind_telegram_draft_presentation(
            session,
            draft_id=draft.draft_id,
            draft_revision=draft.revision,
            chat_id=chat_id,
            message_id=message_id,
            history_page=history_page,
            pending_history_page=pending_history_page,
        )


async def _deliver_untracked_repeat_transaction(
    sessions: async_sessionmaker[AsyncSession],
    message: Message,
    receipt: DraftIngressReceiptSnapshot,
) -> None:
    rendered, draft = await _render_untracked_draft_ingress_receipt(sessions, receipt)
    delivered_message_id = await _replace_message(
        message,
        rendered.text,
        rendered.reply_markup,
    )
    await _bind_untracked_draft_ingress_presentation(
        sessions,
        receipt,
        draft,
        chat_id=message.chat.id,
        message_id=delivered_message_id,
    )


async def _deliver_untracked_wizard_message(
    sessions: async_sessionmaker[AsyncSession],
    message: Message,
    receipt: DraftIngressReceiptSnapshot,
) -> None:
    rendered, draft = await _render_untracked_draft_ingress_receipt(
        sessions,
        receipt,
    )
    sent = await message.answer(
        rendered.text,
        parse_mode="HTML",
        reply_markup=rendered.reply_markup,
    )
    await _bind_untracked_draft_ingress_presentation(
        sessions,
        receipt,
        draft,
        chat_id=message.chat.id,
        message_id=sent.message_id,
    )


async def _deliver_untracked_local_ai(
    sessions: async_sessionmaker[AsyncSession],
    message: Message,
    receipt: LocalAiDraftReceiptSnapshot,
) -> None:
    if receipt.outcome is LocalAiDraftOutcome.CREATED:
        if receipt.ingress is None:  # pragma: no cover - receipt invariant
            raise RuntimeError("Created local AI receipt has no draft")
        await _deliver_untracked_wizard_message(sessions, message, receipt.ingress)
        return
    await message.answer(local_ai_notice_text(receipt.outcome))


def _draft_conflict_receipt(
    receipt: DraftConflictReceiptSnapshot,
) -> tuple[_CallbackMutationReceipt, DraftRef]:
    result = receipt.result
    draft = result.draft
    state = draft.state
    if state.startswith("edit_"):
        transaction = receipt.transaction
        if transaction is None:  # pragma: no cover - receipt invariant
            raise RuntimeError("Edit conflict receipt has no transaction")
        if state == "edit_date":
            return (
                _CallbackMutationReceipt(
                    "<b>Изменить дату</b>\n\n"
                    "Введите <code>ДД.ММ</code> или <code>ДД.ММ.ГГГГ</code>.",
                    edit_input_keyboard(
                        date_menu=True,
                        history_page=receipt.history_page,
                        draft_id=draft.draft_id,
                        revision=draft.revision,
                    ),
                ),
                receipt.draft_ref,
            )
        action = {
            "edit_menu": TransactionDraftNavigationAction.BACK,
            "edit_amount": TransactionDraftNavigationAction.EDIT_AMOUNT,
            "edit_category": TransactionDraftNavigationAction.EDIT_CATEGORY,
            "edit_account": TransactionDraftNavigationAction.EDIT_ACCOUNT,
            "edit_date_menu": TransactionDraftNavigationAction.EDIT_DATE,
            "edit_description": TransactionDraftNavigationAction.EDIT_DESCRIPTION,
        }.get(state)
        if action is None:  # pragma: no cover - receipt state validation is closed
            raise RuntimeError("Unsupported transaction conflict draft state")
        choices = TransactionDraftNavigationChoices(
            accounts=receipt.choices.accounts,
            categories=receipt.choices.categories,
        )
        return _transaction_draft_navigation_receipt(
            TransactionDraftNavigationReceiptSnapshot(
                expected=receipt.expected,
                result=TransactionDraftNavigationResult(
                    action=action,
                    owner=receipt.owner,
                    draft=draft,
                    transaction=transaction,
                    choices=choices,
                ),
                message_id=receipt.message_id,
                history_page=receipt.history_page,
            )
        )

    finance_action = (
        ApplicationDraftNavigationAction.EDIT_DESCRIPTION
        if state == "wizard_description" and "return_state" in draft.payload
        else ApplicationDraftNavigationAction.BACK
    )
    rendered, next_draft = _draft_navigation_receipt(
        DraftNavigationReceiptSnapshot(
            expected=receipt.expected,
            result=DraftNavigationResult(
                action=finance_action,
                status=DraftNavigationStatus.UPDATED,
                owner=receipt.owner,
                draft=draft,
                choices=DraftNavigationChoices(
                    accounts=receipt.choices.accounts,
                    categories=receipt.choices.categories,
                ),
            ),
            message_id=receipt.message_id,
        )
    )
    if next_draft is None:  # pragma: no cover - resolved conflict always has a draft
        raise RuntimeError("Resolved conflict unexpectedly closed the draft")
    return rendered, next_draft


async def _enqueue_draft_conflict_receipt(
    session: TelegramMutationSession,
    request: TelegramMutationRequest,
    receipt: DraftConflictReceiptSnapshot,
) -> None:
    update_id = request.update_id
    if update_id is None:
        raise RuntimeError("tracked receipt requires an update id")
    rendered, draft = _draft_conflict_receipt(receipt)
    queue_edit_message_text(
        session,
        update_id=update_id,
        owner_telegram_user_id=request.owner_telegram_user_id,
        chat_id=request.chat_id,
        message_id=receipt.message_id,
        text=rendered.text,
        parse_mode="HTML",
        reply_markup=rendered.reply_markup,
        draft_id=draft.draft_id,
        draft_revision=draft.revision,
        history_page=receipt.history_page,
    )


def _finance_draft_text_input_receipt(
    receipt: FinanceDraftTextInputReceiptSnapshot,
) -> tuple[_CallbackMutationReceipt, DraftRef]:
    result = receipt.result
    navigation_receipt = DraftNavigationReceiptSnapshot(
        expected=DraftRef(result.draft.draft_id, max(result.draft.revision - 1, 1)),
        result=DraftNavigationResult(
            action=ApplicationDraftNavigationAction.BACK,
            status=DraftNavigationStatus.UPDATED,
            owner=result.owner,
            draft=result.draft,
            choices=DraftNavigationChoices(
                accounts=result.choices.accounts,
                categories=result.choices.categories,
            ),
        ),
        message_id=receipt.target_message_id or receipt.input_message_id,
    )
    rendered, draft = _draft_navigation_receipt(navigation_receipt)
    if draft is None:  # pragma: no cover - finance text transitions never close a draft
        raise RuntimeError("Finance draft text receipt unexpectedly closed the draft")
    if result.status is FinanceDraftTextInputStatus.RETRY:
        retry_error = result.retry_error
        if retry_error is None:  # pragma: no cover - DTO enforces the pair
            raise RuntimeError("Finance draft retry reason is missing")
        retry = {
            FinanceDraftTextInputError.INVALID_AMOUNT: (
                "Введите положительную сумму, например 1450."
            ),
            FinanceDraftTextInputError.INVALID_CATEGORY_NAME: "Введите другое название категории.",
            FinanceDraftTextInputError.CATEGORY_UNAVAILABLE: (
                "Категория недоступна; выберите другое название."
            ),
            FinanceDraftTextInputError.INVALID_ACCOUNT_NAME: "Введите другое название счёта.",
            FinanceDraftTextInputError.ACCOUNT_UNAVAILABLE: (
                "Счёт недоступен; выберите другое название."
            ),
            FinanceDraftTextInputError.INVALID_DATE: "Введите дату в формате ДД.ММ или ДД.ММ.ГГГГ.",
            FinanceDraftTextInputError.INVALID_DESCRIPTION: (
                "Комментарий должен быть короче 500 символов."
            ),
        }.get(retry_error)
        if retry is None:  # pragma: no cover - DTO enum is exhaustive
            raise RuntimeError("Finance draft retry reason is missing")
        rendered = _CallbackMutationReceipt(
            f"⚠️ {retry}\n\n{rendered.text}",
            rendered.reply_markup,
        )
    return rendered, draft


async def _enqueue_finance_draft_text_input_receipt(
    session: TelegramMutationSession,
    request: TelegramMutationRequest,
    receipt: FinanceDraftTextInputReceiptSnapshot,
) -> None:
    update_id = request.update_id
    if update_id is None:
        raise RuntimeError("tracked receipt requires an update id")
    rendered, draft = _finance_draft_text_input_receipt(receipt)
    context = await _prior_draft_presentation_context(session, draft)
    queue = queue_send_message if receipt.target_message_id is None else queue_edit_message_text
    queue(
        session,
        update_id=update_id,
        owner_telegram_user_id=request.owner_telegram_user_id,
        chat_id=request.chat_id,
        **({} if receipt.target_message_id is None else {"message_id": receipt.target_message_id}),
        text=rendered.text,
        parse_mode="HTML",
        reply_markup=rendered.reply_markup,
        draft_id=draft.draft_id,
        draft_revision=draft.revision,
        history_page=context.history_page,
        pending_history_page=context.pending_history_page,
    )


async def _deliver_untracked_finance_draft_text_input(
    message: Message,
    receipt: FinanceDraftTextInputReceiptSnapshot,
) -> int:
    rendered, _draft = _finance_draft_text_input_receipt(receipt)
    bot = message.bot
    if bot is None:  # pragma: no cover - aiogram dispatch always binds the bot
        raise RuntimeError("Telegram bot is not bound to the message")
    target_message_id = receipt.target_message_id
    if target_message_id is not None:
        try:
            edited = await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=target_message_id,
                text=rendered.text,
                parse_mode="HTML",
                reply_markup=rendered.reply_markup,
            )
            return edited.message_id if isinstance(edited, Message) else target_message_id
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).casefold():
                return target_message_id
    sent = await message.answer(
        rendered.text,
        parse_mode="HTML",
        reply_markup=rendered.reply_markup,
    )
    return sent.message_id


def _settings_text_input_receipt(
    receipt: SettingsTextInputReceiptSnapshot,
) -> tuple[_CallbackMutationReceipt, DraftRef | None]:
    result = receipt.result
    if result.status is SettingsTextInputStatus.RETRY:
        draft = result.draft
        retry_error = result.retry_error
        if draft is None or retry_error is None:  # pragma: no cover - DTO invariant
            raise RuntimeError("Settings retry receipt is incomplete")
        title = {
            SettingsTextInputOperation.ACCOUNT_CREATE: "Новый счёт",
            SettingsTextInputOperation.ACCOUNT_RENAME: "Переименовать счёт",
            SettingsTextInputOperation.CATEGORY_CREATE: "Новая категория",
            SettingsTextInputOperation.CATEGORY_RENAME: "Переименовать категорию",
        }[result.operation]
        prompt = {
            SettingsTextInputError.INVALID_NAME: "Введите другое название.",
            SettingsTextInputError.VERSION_CONFLICT: (
                "Объект изменился. Повторите ввод для актуальной версии."
            ),
            SettingsTextInputError.TARGET_UNAVAILABLE: (
                "Объект больше недоступен. Вернитесь в настройки или отмените ввод."
            ),
        }[retry_error]
        return (
            _CallbackMutationReceipt(
                f"<b>{title}</b>\n\n⚠️ {prompt}",
                settings_text_input_keyboard(draft.draft_id, draft.revision),
            ),
            draft.ref,
        )
    account = result.account
    if account is not None:
        is_default = account.account_id == result.owner.default_account_id
        return (
            _CallbackMutationReceipt(
                _account_snapshot_card(account, is_default),
                settings_account_keyboard(
                    account.account_id,
                    account.version,
                    is_default=is_default,
                ),
            ),
            None,
        )
    category = result.category
    if category is None:  # pragma: no cover - DTO invariant
        raise RuntimeError("Successful settings receipt has no catalog snapshot")
    return (
        _CallbackMutationReceipt(
            _category_snapshot_card(category),
            settings_category_keyboard(
                category.category_id,
                category.kind.value,
                category.version,
            ),
        ),
        None,
    )


async def _enqueue_settings_text_input_receipt(
    session: TelegramMutationSession,
    request: TelegramMutationRequest,
    receipt: SettingsTextInputReceiptSnapshot,
) -> None:
    update_id = request.update_id
    if update_id is None:
        raise RuntimeError("tracked receipt requires an update id")
    rendered, draft = _settings_text_input_receipt(receipt)
    queue = queue_send_message if receipt.target_message_id is None else queue_edit_message_text
    queue(
        session,
        update_id=update_id,
        owner_telegram_user_id=request.owner_telegram_user_id,
        chat_id=request.chat_id,
        **({} if receipt.target_message_id is None else {"message_id": receipt.target_message_id}),
        text=rendered.text,
        parse_mode="HTML",
        reply_markup=rendered.reply_markup,
        draft_id=draft.draft_id if draft is not None else None,
        draft_revision=draft.revision if draft is not None else None,
        history_page=receipt.history_page if draft is not None else None,
        pending_history_page=receipt.pending_history_page if draft is not None else None,
    )


async def _deliver_untracked_settings_text_input(
    message: Message,
    receipt: SettingsTextInputReceiptSnapshot,
) -> tuple[int, DraftRef | None]:
    rendered, draft = _settings_text_input_receipt(receipt)
    bot = message.bot
    if bot is None:  # pragma: no cover - aiogram dispatch always binds the bot
        raise RuntimeError("Telegram bot is not bound to the message")
    target_message_id = receipt.target_message_id
    if target_message_id is not None:
        try:
            edited = await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=target_message_id,
                text=rendered.text,
                parse_mode="HTML",
                reply_markup=rendered.reply_markup,
            )
            delivered_message_id = (
                edited.message_id if isinstance(edited, Message) else target_message_id
            )
            return delivered_message_id, draft
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).casefold():
                return target_message_id, draft
    sent = await message.answer(
        rendered.text,
        parse_mode="HTML",
        reply_markup=rendered.reply_markup,
    )
    return sent.message_id, draft


def _transaction_draft_navigation_receipt(
    receipt: TransactionDraftNavigationReceiptSnapshot,
) -> tuple[_CallbackMutationReceipt, DraftRef]:
    result = receipt.result
    draft = result.draft
    transaction = result.transaction
    action = result.action
    if action is TransactionDraftNavigationAction.EDIT_AMOUNT:
        rendered = _CallbackMutationReceipt(
            "<b>Изменить сумму</b>\n\nВведите новую сумму, например <code>1750</code>.",
            edit_input_keyboard(
                history_page=receipt.history_page,
                draft_id=draft.draft_id,
                revision=draft.revision,
            ),
        )
    elif action is TransactionDraftNavigationAction.EDIT_DESCRIPTION:
        rendered = _CallbackMutationReceipt(
            "<b>Изменить комментарий</b>\n\nВведите новый текст. "
            "Дефис <code>-</code> очистит комментарий.",
            edit_input_keyboard(
                history_page=receipt.history_page,
                draft_id=draft.draft_id,
                revision=draft.revision,
            ),
        )
    elif action is TransactionDraftNavigationAction.EDIT_CATEGORY:
        choices = [
            Choice(item.category_id, item.name, item.emoji, item.version)
            for item in result.choices.categories
        ]
        rendered = _CallbackMutationReceipt(
            "<b>Изменить категорию</b>\n\nВыберите новую категорию:",
            category_keyboard(
                choices,
                edit=True,
                history_page=receipt.history_page,
                draft_id=draft.draft_id,
                revision=draft.revision,
            ),
        )
    elif action is TransactionDraftNavigationAction.EDIT_ACCOUNT:
        choices = [
            Choice(item.account_id, item.name, version=item.version)
            for item in result.choices.accounts
        ]
        rendered = _CallbackMutationReceipt(
            "<b>Изменить счёт</b>\n\nВыберите новый счёт:",
            account_keyboard(
                choices,
                edit=True,
                history_page=receipt.history_page,
                draft_id=draft.draft_id,
                revision=draft.revision,
            ),
        )
    elif action in {
        TransactionDraftNavigationAction.EDIT_DATE,
        TransactionDraftNavigationAction.DATE_BACK,
    }:
        rendered = _CallbackMutationReceipt(
            "<b>Изменить дату</b>\n\nВыберите дату или введите свою:",
            edit_date_keyboard(
                transaction.transaction_id,
                transaction.version,
                receipt.history_page,
                draft_id=draft.draft_id,
                revision=draft.revision,
            ),
        )
    else:
        rendered = _CallbackMutationReceipt(
            transaction_snapshot_card(transaction, result.owner.timezone, title="Что изменить?"),
            edit_keyboard(
                transaction.transaction_id,
                transaction.version,
                receipt.history_page,
                draft_id=draft.draft_id,
                revision=draft.revision,
            ),
        )
    return rendered, draft.ref


async def _enqueue_transaction_draft_navigation_receipt(
    session: TelegramMutationSession,
    request: TelegramMutationRequest,
    receipt: TransactionDraftNavigationReceiptSnapshot,
) -> None:
    update_id = request.update_id
    if update_id is None:
        raise RuntimeError("tracked receipt requires an update id")
    rendered, draft = _transaction_draft_navigation_receipt(receipt)
    queue_edit_message_text(
        session,
        update_id=update_id,
        owner_telegram_user_id=request.owner_telegram_user_id,
        chat_id=request.chat_id,
        message_id=receipt.message_id,
        text=rendered.text,
        parse_mode="HTML",
        reply_markup=rendered.reply_markup,
        draft_id=draft.draft_id,
        draft_revision=draft.revision,
        history_page=receipt.history_page,
    )


def _transaction_draft_selection_receipt(
    receipt: TransactionDraftSelectionReceiptSnapshot,
) -> tuple[_CallbackMutationReceipt, DraftRef | None]:
    result = receipt.result
    if result.status is TransactionDraftSelectionStatus.DATE_INPUT_REQUIRED:
        draft = result.draft
        if draft is None:  # pragma: no cover - DTO invariant
            raise RuntimeError("Date input receipt has no draft")
        return (
            _CallbackMutationReceipt(
                "<b>Изменить дату</b>\n\nВведите <code>ДД.ММ</code> или <code>ДД.ММ.ГГГГ</code>.",
                edit_input_keyboard(
                    date_menu=True,
                    history_page=receipt.history_page,
                    draft_id=draft.draft_id,
                    revision=draft.revision,
                ),
            ),
            draft.ref,
        )
    transaction = result.transaction
    if transaction is None:  # pragma: no cover - DTO invariant
        raise RuntimeError("Updated transaction receipt has no transaction")
    title = {
        TransactionDraftSelectionAction.CATEGORY: "✅ Категория изменена",
        TransactionDraftSelectionAction.ACCOUNT: "✅ Счёт изменён",
        TransactionDraftSelectionAction.DATE: "✅ Дата изменена",
    }[result.action]
    return (
        _CallbackMutationReceipt(
            transaction_snapshot_card(transaction, result.owner.timezone, title=title),
            transaction_keyboard(
                transaction.transaction_id,
                transaction.version,
                receipt.history_page,
            ),
        ),
        None,
    )


async def _enqueue_transaction_draft_selection_receipt(
    session: TelegramMutationSession,
    request: TelegramMutationRequest,
    receipt: TransactionDraftSelectionReceiptSnapshot,
) -> None:
    update_id = request.update_id
    if update_id is None:
        raise RuntimeError("tracked receipt requires an update id")
    rendered, draft = _transaction_draft_selection_receipt(receipt)
    queue_edit_message_text(
        session,
        update_id=update_id,
        owner_telegram_user_id=request.owner_telegram_user_id,
        chat_id=request.chat_id,
        message_id=receipt.message_id,
        text=rendered.text,
        parse_mode="HTML",
        reply_markup=rendered.reply_markup,
        draft_id=draft.draft_id if draft is not None else None,
        draft_revision=draft.revision if draft is not None else None,
        history_page=receipt.history_page if draft is not None else None,
    )


def _transaction_edit_receipt(
    receipt: TransactionEditReceiptSnapshot,
) -> tuple[_CallbackMutationReceipt, DraftRef]:
    result = receipt.result
    draft = result.draft
    if result.status is TransactionEditIngressStatus.BLOCKED_BANK_IMPORT:
        rendered = _CallbackMutationReceipt(
            "<b>Банковская строка уже на проверке</b>\n\n"
            "Сначала завершите или отмените её, затем редактируйте другую операцию.",
            None,
        )
    elif result.status is TransactionEditIngressStatus.DRAFT_CREATED:
        rendered = _CallbackMutationReceipt(
            transaction_snapshot_card(
                result.transaction,
                result.owner.timezone,
                title="Что изменить?",
            ),
            edit_keyboard(
                result.transaction.transaction_id,
                result.transaction.version,
                receipt.history_page,
                draft_id=draft.draft_id,
                revision=draft.revision,
            ),
        )
    else:
        rendered = _CallbackMutationReceipt(
            "<b>Есть незавершённый ввод</b>\n\nРедактирование не заменит его без вашего решения.",
            draft_conflict_keyboard(draft.draft_id, draft.revision),
        )
    return rendered, receipt.draft_ref


async def _enqueue_transaction_edit_receipt(
    session: TelegramMutationSession,
    request: TelegramMutationRequest,
    receipt: TransactionEditReceiptSnapshot,
) -> None:
    update_id = request.update_id
    if update_id is None:
        raise RuntimeError("tracked receipt requires an update id")
    rendered, draft = _transaction_edit_receipt(receipt)
    history_page: int | None = receipt.history_page
    pending_history_page: int | None = None
    if receipt.result.status is TransactionEditIngressStatus.CONFLICT_STAGED:
        context = await _prior_draft_presentation_context(session, draft)
        history_page = context.history_page
        pending_history_page = receipt.history_page
    queue_edit_message_text(
        session,
        update_id=update_id,
        owner_telegram_user_id=request.owner_telegram_user_id,
        chat_id=request.chat_id,
        message_id=receipt.message_id,
        text=rendered.text,
        parse_mode="HTML",
        reply_markup=rendered.reply_markup,
        draft_id=draft.draft_id,
        draft_revision=draft.revision,
        history_page=history_page,
        pending_history_page=pending_history_page,
    )


async def _deliver_untracked_transaction_edit(
    sessions: async_sessionmaker[AsyncSession],
    message: Message,
    receipt: TransactionEditReceiptSnapshot,
) -> None:
    rendered, draft = _transaction_edit_receipt(receipt)
    delivered_message_id = await _replace_message(
        message,
        rendered.text,
        rendered.reply_markup,
    )
    async with sessions() as session, session.begin():
        bound_history_page: int | None = receipt.history_page
        pending_history_page: int | None = None
        if receipt.result.status is TransactionEditIngressStatus.CONFLICT_STAGED:
            context = await _prior_draft_presentation_context(session, draft)
            bound_history_page = context.history_page
            pending_history_page = receipt.history_page
        await _bind_draft_presentation_preserving_context(
            session,
            draft,
            chat_id=message.chat.id,
            message_id=delivered_message_id,
            history_page=bound_history_page,
            pending_history_page=pending_history_page,
            override_context=True,
        )


def _transaction_edit_text_input_receipt(
    receipt: TransactionEditTextInputReceiptSnapshot,
) -> tuple[_CallbackMutationReceipt, DraftRef | None]:
    result = receipt.result
    draft = result.draft
    if result.status is TransactionEditTextInputStatus.RETRY:
        if draft is None or result.retry_error is None:  # pragma: no cover - DTO invariant
            raise RuntimeError("Transaction edit retry receipt is incomplete")
        prompt = {
            TransactionEditTextInputError.INVALID_AMOUNT: (
                "⚠️ Введите положительную сумму, например <code>1750</code>."
            ),
            TransactionEditTextInputError.INVALID_DATE: (
                "⚠️ Введите дату в формате <code>ДД.ММ</code> или <code>ДД.ММ.ГГГГ</code>."
            ),
            TransactionEditTextInputError.INVALID_DESCRIPTION: (
                "⚠️ Комментарий должен быть короче 500 символов."
            ),
        }[result.retry_error]
        return (
            _CallbackMutationReceipt(
                prompt,
                edit_input_keyboard(
                    date_menu=draft.state == "edit_date",
                    history_page=receipt.history_page,
                    draft_id=draft.draft_id,
                    revision=draft.revision,
                ),
            ),
            draft.ref,
        )
    return (
        _CallbackMutationReceipt(
            transaction_snapshot_card(
                result.transaction,
                result.owner.timezone,
                title="✅ Операция изменена",
            ),
            transaction_keyboard(
                result.transaction.transaction_id,
                result.transaction.version,
                receipt.history_page,
            ),
        ),
        None,
    )


async def _enqueue_transaction_edit_text_input_receipt(
    session: TelegramMutationSession,
    request: TelegramMutationRequest,
    receipt: TransactionEditTextInputReceiptSnapshot,
) -> None:
    update_id = request.update_id
    if update_id is None:
        raise RuntimeError("tracked receipt requires an update id")
    rendered, draft = _transaction_edit_text_input_receipt(receipt)
    queue_edit_message_text(
        session,
        update_id=update_id,
        owner_telegram_user_id=request.owner_telegram_user_id,
        chat_id=request.chat_id,
        message_id=receipt.target_message_id,
        text=rendered.text,
        parse_mode="HTML",
        reply_markup=rendered.reply_markup,
        draft_id=draft.draft_id if draft is not None else None,
        draft_revision=draft.revision if draft is not None else None,
        history_page=receipt.history_page if draft is not None else None,
    )


async def _deliver_untracked_transaction_edit_text_input(
    message: Message,
    receipt: TransactionEditTextInputReceiptSnapshot,
) -> tuple[int, DraftRef | None]:
    rendered, draft = _transaction_edit_text_input_receipt(receipt)
    bot = message.bot
    if bot is None:  # pragma: no cover - aiogram dispatch always binds the bot
        raise RuntimeError("Telegram bot is not bound to the message")
    try:
        edited = await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=receipt.target_message_id,
            text=rendered.text,
            parse_mode="HTML",
            reply_markup=rendered.reply_markup,
        )
        delivered_message_id = (
            edited.message_id if isinstance(edited, Message) else receipt.target_message_id
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).casefold():
            delivered_message_id = receipt.target_message_id
        else:
            sent = await message.answer(
                rendered.text,
                parse_mode="HTML",
                reply_markup=rendered.reply_markup,
            )
            delivered_message_id = sent.message_id
    return delivered_message_id, draft


async def _deliver_tracked_text_input_receipt(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    message: Message,
    update_id: int,
    *,
    special_delivery: CsvExportDelivery,
) -> bool:
    bot = message.bot
    if bot is None:  # pragma: no cover - aiogram dispatch always binds the bot
        raise RuntimeError("Telegram bot is not bound to the message")
    delivered = await deliver_pending_responses(
        sessions,
        bot,
        update_id=update_id,
        owner_telegram_user_id=settings.owner_telegram_user_id,
        chat_id=message.chat.id,
        special_delivery=special_delivery.deliver_job,
    )
    return delivered > 0


async def _bind_untracked_finance_text_input(
    sessions: async_sessionmaker[AsyncSession],
    message: Message,
    receipt: FinanceDraftTextInputReceiptSnapshot,
    message_id: int,
) -> None:
    async with sessions() as session, session.begin():
        await _bind_draft_presentation_preserving_context(
            session,
            receipt.draft_ref,
            chat_id=message.chat.id,
            message_id=message_id,
        )


async def _bind_untracked_transaction_text_input(
    sessions: async_sessionmaker[AsyncSession],
    message: Message,
    receipt: TransactionEditTextInputReceiptSnapshot,
    message_id: int,
    draft: DraftRef,
) -> None:
    async with sessions() as session, session.begin():
        await _bind_draft_presentation_preserving_context(
            session,
            draft,
            chat_id=message.chat.id,
            message_id=message_id,
            history_page=receipt.history_page,
            override_context=True,
        )


async def _bind_untracked_settings_text_input(
    sessions: async_sessionmaker[AsyncSession],
    message: Message,
    receipt: SettingsTextInputReceiptSnapshot,
    message_id: int,
    draft: DraftRef,
) -> None:
    async with sessions() as session, session.begin():
        await _bind_draft_presentation_preserving_context(
            session,
            draft,
            chat_id=message.chat.id,
            message_id=message_id,
            history_page=receipt.history_page,
            pending_history_page=receipt.pending_history_page,
            override_context=True,
        )


async def _deliver_untracked_quick_text_input(
    sessions: async_sessionmaker[AsyncSession],
    message: Message,
    receipt: DraftIngressReceiptSnapshot,
) -> None:
    rendered, draft = await _render_untracked_draft_ingress_receipt(sessions, receipt)
    sent = await message.answer(
        rendered.text,
        parse_mode="HTML",
        reply_markup=rendered.reply_markup,
    )
    await _bind_untracked_draft_ingress_presentation(
        sessions,
        receipt,
        draft,
        chat_id=message.chat.id,
        message_id=sent.message_id,
    )


def _plain_draft_receipt(receipt: PlainDraftReceiptSnapshot) -> _CallbackMutationReceipt:
    if receipt.operation is PlainDraftOperation.CANCELLED:
        return _CallbackMutationReceipt("<b>Ввод отменён</b>\nЧерновик удалён.", None)
    transaction = receipt.transaction
    if transaction is None:  # pragma: no cover - DTO invariant
        raise RuntimeError("Confirmed plain draft receipt has no transaction")
    return _CallbackMutationReceipt(
        transaction_snapshot_card(
            transaction,
            receipt.owner.timezone,
            title="✅ Операция сохранена",
        ),
        transaction_keyboard(transaction.transaction_id, transaction.version),
    )


async def _enqueue_plain_draft_receipt(
    session: TelegramMutationSession,
    request: TelegramMutationRequest,
    receipt: PlainDraftReceiptSnapshot,
) -> None:
    update_id = request.update_id
    if update_id is None:
        raise RuntimeError("tracked receipt requires an update id")
    rendered = _plain_draft_receipt(receipt)
    queue_edit_message_text(
        session,
        update_id=update_id,
        owner_telegram_user_id=request.owner_telegram_user_id,
        chat_id=request.chat_id,
        message_id=receipt.message_id,
        text=rendered.text,
        parse_mode="HTML",
        reply_markup=rendered.reply_markup,
    )


_UNDO_LABELS = {
    UndoAction.CREATE: "Создание операции отменено",
    UndoAction.DELETE: "Удаление отменено",
    UndoAction.RESTORE: "Восстановление отменено",
    UndoAction.UPDATE: "Последнее изменение отменено",
}


def _undo_receipt(receipt: UndoReceiptSnapshot) -> _CallbackMutationReceipt:
    result = receipt.result
    if result is None:
        return _CallbackMutationReceipt("Отменять пока нечего.", None)
    transaction = result.transaction
    return _CallbackMutationReceipt(
        transaction_snapshot_card(
            transaction,
            receipt.owner.timezone,
            title=f"↩️ {_UNDO_LABELS[result.action]}",
        ),
        (
            restore_keyboard(transaction.transaction_id, transaction.version)
            if transaction.deleted_at is not None
            else transaction_keyboard(transaction.transaction_id, transaction.version)
        ),
    )


async def _enqueue_undo_receipt(
    session: TelegramMutationSession,
    request: TelegramMutationRequest,
    receipt: UndoReceiptSnapshot,
) -> None:
    update_id = request.update_id
    if update_id is None:
        raise RuntimeError("tracked receipt requires an update id")
    rendered = _undo_receipt(receipt)
    queue_send_message(
        session,
        update_id=update_id,
        owner_telegram_user_id=request.owner_telegram_user_id,
        chat_id=request.chat_id,
        text=rendered.text,
        parse_mode="HTML",
        reply_markup=rendered.reply_markup,
    )


async def _deliver_untracked_undo(
    message: Message,
    receipt: UndoReceiptSnapshot,
) -> None:
    rendered = _undo_receipt(receipt)
    await message.answer(
        rendered.text,
        parse_mode="HTML",
        reply_markup=rendered.reply_markup,
    )


def _ocr_queue_receipt(
    receipt: OcrQueueReceiptSnapshot,
) -> tuple[_CallbackMutationReceipt, DraftRef | None]:
    result = receipt.result
    if result.status is OcrQueueStatus.ADVANCED:
        draft = result.draft
        if draft is None:  # pragma: no cover - DTO invariant
            raise RuntimeError("Advanced OCR receipt has no draft")
        payload = dict(draft.payload)
        if draft.state == "category_required":
            choices = [
                Choice(item.category_id, item.name, item.emoji, item.version)
                for item in receipt.active_categories
            ]
            body = "<b>Уточните категорию</b>\n\nВыберите существующую категорию:"
            keyboard = category_keyboard(
                choices,
                draft_id=draft.draft_id,
                revision=draft.revision,
            )
        elif draft.state == "account_required":
            choices = [
                Choice(item.account_id, item.name, version=item.version)
                for item in receipt.active_accounts
            ]
            body = "<b>Уточните счёт</b>\n\nВыберите существующий счёт:"
            keyboard = account_keyboard(
                choices,
                draft_id=draft.draft_id,
                revision=draft.revision,
            )
        elif draft.state == "review":
            body = wizard_summary(payload, receipt.owner.base_currency, receipt.owner.timezone)
            keyboard = _review_keyboard(payload, draft.draft_id, draft.revision)
        else:
            raise RuntimeError("Unsupported prepared OCR draft state")
        prefix = (
            "✅ Предыдущая операция сохранена.\n\n"
            if receipt.operation is OcrQueueOperation.CONFIRMED
            else "⏭ Операция пропущена.\n\n"
        )
        return _CallbackMutationReceipt(prefix + body, keyboard), draft.ref

    if result.status is OcrQueueStatus.CANCELLED:
        return (
            _CallbackMutationReceipt(
                "<b>Импорт завершён</b>\n"
                f"Сохранено: {result.saved}. Пропущено: {result.skipped}. "
                "Текущая и оставшиеся операции отменены.",
                None,
            ),
            None,
        )
    if receipt.operation is OcrQueueOperation.CONFIRMED:
        transaction = result.transaction
        if transaction is None:
            raise RuntimeError("Completed confirmation receipt has no transaction")
        skipped = f", пропущено {result.skipped}" if result.skipped else ""
        title = f"✅ Импорт завершён · сохранено {result.saved}{skipped}"
        return (
            _CallbackMutationReceipt(
                transaction_snapshot_card(transaction, receipt.owner.timezone, title=title),
                transaction_keyboard(transaction.transaction_id, transaction.version),
            ),
            None,
        )
    return (
        _CallbackMutationReceipt(
            "<b>Импорт завершён</b>\n\n"
            f"Сохранено: <b>{result.saved}</b>. Пропущено: <b>{result.skipped}</b>.",
            None,
        ),
        None,
    )


async def _enqueue_ocr_queue_receipt(
    session: TelegramMutationSession,
    request: TelegramMutationRequest,
    receipt: OcrQueueReceiptSnapshot,
) -> None:
    update_id = request.update_id
    if update_id is None:
        raise RuntimeError("tracked receipt requires an update id")
    rendered, next_draft = _ocr_queue_receipt(receipt)
    context = (
        await _prior_draft_presentation_context(session, next_draft)
        if next_draft is not None
        else TelegramDraftPresentationContext()
    )
    enqueue = queue_send_message if receipt.message_id is None else queue_edit_message_text
    kwargs: dict[str, object] = {
        "update_id": update_id,
        "owner_telegram_user_id": request.owner_telegram_user_id,
        "chat_id": request.chat_id,
        "text": rendered.text,
        "parse_mode": "HTML",
        "reply_markup": rendered.reply_markup,
        "draft_id": next_draft.draft_id if next_draft is not None else None,
        "draft_revision": next_draft.revision if next_draft is not None else None,
        "history_page": context.history_page,
        "pending_history_page": context.pending_history_page,
    }
    if receipt.message_id is not None:
        kwargs["message_id"] = receipt.message_id
    enqueue(session, **kwargs)  # type: ignore[arg-type]


def _ocr_image_receipt(
    receipt: OcrImageReceiptSnapshot,
) -> tuple[_CallbackMutationReceipt, DraftRef | None]:
    result = receipt.result
    if result.status is OcrImageIngressStatus.REJECTED:
        return (
            _CallbackMutationReceipt(
                "<b>Не получилось распознать операцию</b>\n\n"
                "Попробуйте более чёткий скриншот или отправьте операцию текстом.",
                None,
            ),
            None,
        )
    draft = result.draft
    if draft is None:  # pragma: no cover - DTO invariant
        raise RuntimeError("OCR image receipt has no draft")
    if result.status is OcrImageIngressStatus.ACTIVE_DRAFT:
        return (
            _CallbackMutationReceipt(
                "<b>Есть незавершённый ввод</b>\n\n"
                "Завершите или отмените текущий черновик, затем отправьте изображение ещё раз.",
                resume_draft_keyboard(draft.draft_id, draft.revision),
            ),
            draft.ref,
        )
    if result.queue_item is None:  # pragma: no cover - DTO invariant
        raise RuntimeError("Started OCR image receipt has no queue item")
    # Initial OCR has no "previous item" prefix, so render the three canonical
    # states directly instead of pretending that a queue mutation occurred.
    payload = dict(draft.payload)
    if draft.state == "category_required":
        choices = [
            Choice(item.category_id, item.name, item.emoji, item.version)
            for item in receipt.active_categories
        ]
        rendered = _CallbackMutationReceipt(
            "<b>Уточните категорию</b>\n\nВыберите существующую категорию:",
            category_keyboard(choices, draft_id=draft.draft_id, revision=draft.revision),
        )
    elif draft.state == "account_required":
        choices = [
            Choice(item.account_id, item.name, version=item.version)
            for item in receipt.active_accounts
        ]
        rendered = _CallbackMutationReceipt(
            "<b>Уточните счёт</b>\n\nВыберите существующий счёт:",
            account_keyboard(choices, draft_id=draft.draft_id, revision=draft.revision),
        )
    elif draft.state == "review":
        rendered = _CallbackMutationReceipt(
            wizard_summary(payload, receipt.owner.base_currency, receipt.owner.timezone),
            _review_keyboard(payload, draft.draft_id, draft.revision),
        )
    else:  # pragma: no cover - receipt validation rejects this
        raise RuntimeError("Unsupported prepared OCR draft state")
    return rendered, draft.ref


async def _enqueue_ocr_image_receipt(
    session: TelegramMutationSession,
    request: TelegramMutationRequest,
    receipt: OcrImageReceiptSnapshot,
) -> None:
    update_id = request.update_id
    if update_id is None:
        raise RuntimeError("tracked receipt requires an update id")
    rendered, draft = _ocr_image_receipt(receipt)
    context = TelegramDraftPresentationContext()
    if draft is not None and receipt.result.status is OcrImageIngressStatus.ACTIVE_DRAFT:
        current = await lock_telegram_draft_presentation_context_by_revision(
            session,
            draft.draft_id,
            draft.revision,
        )
        if current is not None:
            context = current
    queue_send_message(
        session,
        update_id=update_id,
        owner_telegram_user_id=request.owner_telegram_user_id,
        chat_id=request.chat_id,
        text=rendered.text,
        parse_mode="HTML",
        reply_markup=rendered.reply_markup,
        draft_id=draft.draft_id if draft is not None else None,
        draft_revision=draft.revision if draft is not None else None,
        history_page=context.history_page,
        pending_history_page=context.pending_history_page,
    )


async def _enqueue_draft_completion_receipt(
    session: TelegramMutationSession,
    request: TelegramMutationRequest,
    receipt: DraftCompletionReceiptSnapshot,
) -> None:
    if receipt.kind is DraftCompletionKind.PLAIN:
        if receipt.plain is None:  # pragma: no cover - DTO invariant
            raise RuntimeError("Plain completion receipt is missing")
        await _enqueue_plain_draft_receipt(session, request, receipt.plain)
        return
    if receipt.ocr_queue is None:  # pragma: no cover - DTO invariant
        raise RuntimeError("OCR completion receipt is missing")
    await _enqueue_ocr_queue_receipt(session, request, receipt.ocr_queue)


def _callback_message(callback: CallbackQuery) -> Message | None:
    return callback.message if isinstance(callback.message, Message) else None


def _account_snapshot_card(account: AccountSnapshot, is_default: bool) -> str:
    status = "⭐ Основной счёт" if is_default else "Активный счёт"
    return (
        f"<b>💳 {escape(account.name)}</b>\n\n"
        f"{status}\n"
        f"Валюта: <b>{escape(account.currency)}</b>\n\n"
        "Архивация не удаляет операции с этого счёта."
    )


def _category_snapshot_card(category: CategorySnapshot) -> str:
    kind = "Расход" if category.kind is TransactionType.EXPENSE else "Доход"
    return (
        f"<b>{category.emoji or '▫️'} {escape(category.name)}</b>\n\n"
        f"Тип: <b>{kind}</b>\n\n"
        "Архивация скрывает категорию при новом вводе, но сохраняет её в истории."
    )


def _account_catalog_receipt(
    receipt: AccountCatalogReceiptSnapshot,
) -> _CallbackMutationReceipt:
    accounts = receipt.active_accounts + receipt.archived_accounts
    account = next(
        (item for item in accounts if item.account_id == receipt.mutation.entity_id),
        None,
    )
    if account is None or account.version != receipt.mutation.version:
        raise RuntimeError("catalog account receipt is inconsistent")
    if receipt.operation is CatalogOperation.ACCOUNT_ARCHIVED:
        choices = [
            Choice(item.account_id, item.name, version=item.version)
            for item in receipt.active_accounts
        ]
        return _CallbackMutationReceipt(
            text=(
                "<b>Счета</b>\n\n"
                "⭐ — основной счёт для операций без <code>@счёт</code>.\n"
                "Откройте счёт, чтобы переименовать его или изменить основной."
            ),
            reply_markup=settings_accounts_keyboard(
                choices,
                receipt.owner.default_account_id,
                len(receipt.archived_accounts),
            ),
        )
    is_default = account.account_id == receipt.owner.default_account_id
    return _CallbackMutationReceipt(
        text=_account_snapshot_card(account, is_default),
        reply_markup=settings_account_keyboard(
            account.account_id,
            account.version,
            is_default=is_default,
        ),
    )


def _category_catalog_receipt(
    receipt: CategoryCatalogReceiptSnapshot,
) -> _CallbackMutationReceipt:
    categories = receipt.active_categories + receipt.archived_categories
    category = next(
        (item for item in categories if item.category_id == receipt.mutation.entity_id),
        None,
    )
    if category is None or category.version != receipt.mutation.version:
        raise RuntimeError("catalog category receipt is inconsistent")
    if receipt.operation is CatalogOperation.CATEGORY_ARCHIVED:
        active = tuple(item for item in receipt.active_categories if item.kind is category.kind)
        archived_count = sum(item.kind is category.kind for item in receipt.archived_categories)
        choices = [Choice(item.category_id, item.name, item.emoji, item.version) for item in active]
        kind = category.kind.value
        title = (
            "Категории расходов"
            if category.kind is TransactionType.EXPENSE
            else "Категории доходов"
        )
        return _CallbackMutationReceipt(
            text=f"<b>{title}</b>\n\nОткройте категорию для переименования или архивации.",
            reply_markup=settings_category_list_keyboard(
                choices,
                kind,
                archived_count,
            ),
        )
    return _CallbackMutationReceipt(
        text=_category_snapshot_card(category),
        reply_markup=settings_category_keyboard(
            category.category_id,
            category.kind.value,
            category.version,
        ),
    )


def _catalog_callback_receipt(receipt: CatalogReceiptSnapshot) -> _CallbackMutationReceipt:
    if isinstance(receipt, AccountCatalogReceiptSnapshot):
        return _account_catalog_receipt(receipt)
    return _category_catalog_receipt(receipt)


def _draft_settings_receipt(
    receipt: DraftSettingsReceiptSnapshot,
) -> _CallbackMutationReceipt:
    account_name = (
        receipt.default_account.name if receipt.default_account is not None else "не выбран"
    )
    return _CallbackMutationReceipt(
        text=_settings_text(receipt.owner, account_name),
        reply_markup=settings_keyboard(receipt.owner.fast_mode),
    )


async def _deliver_untracked_catalog_receipt(
    message: Message,
    receipt: CatalogReceiptSnapshot,
) -> None:
    rendered = _catalog_callback_receipt(receipt)
    await _replace_message(message, rendered.text, rendered.reply_markup)


def _review_keyboard(
    payload: dict[str, object],
    draft_id: UUID | None = None,
    revision: int | None = None,
) -> InlineKeyboardMarkup:
    offer = str(payload.get("rule_offer_pattern", "")).strip() or None
    pending = payload.get("pending_rule")
    scope = str(pending.get("scope")) if isinstance(pending, dict) else None
    return wizard_confirm_keyboard(
        bool(payload.get("description")),
        rule_offer=offer,
        rule_scope=scope,
        draft_id=draft_id,
        revision=revision,
        ocr_batch=_ocr_batch(payload) is not None,
        ocr_has_more=_ocr_batch_has_more(payload),
        restricted_import=payload.get("flow") == "bank_import",
    )


def _ocr_batch(payload: dict[str, object]) -> dict[str, object] | None:
    batch = payload.get("ocr_batch")
    return batch if isinstance(batch, dict) else None


def _ocr_batch_has_more(payload: dict[str, object]) -> bool:
    batch = _ocr_batch(payload)
    return bool(_ocr_batch_remaining(batch))


def _ocr_batch_remaining(batch: dict[str, object] | None) -> list[object]:
    if batch is None:
        return []
    remaining = batch.get("remaining")
    return list(remaining) if isinstance(remaining, list) else []


def _ocr_batch_header(payload: dict[str, object]) -> str:
    batch = _ocr_batch(payload)
    if batch is None:
        return ""
    index = int(str(batch.get("index", batch.get("position", 1))))
    total = int(str(batch.get("total", 1)))
    return f"<b>Из изображения · операция {index} из {total}</b>\n\n"


def _settings_text(user: OwnerSnapshot, account_name: str) -> str:
    return (
        "<b>Настройки</b>\n\n"
        f"💳 Основной счёт: <b>{escape(account_name)}</b>\n"
        f"🕒 Часовой пояс: <b>{escape(user.timezone)}</b>\n"
        f"💱 Валюта: <b>{escape(user.base_currency)}</b>\n\n"
        "Перед записью любой операции Finbot показывает карточку проверки."
    )


def build_dispatcher(
    settings: Settings,
    image_text_extractor: ImageTextExtractor | None = None,
    *,
    miniapp_menu: MiniAppMenuConfigurator | None = None,
) -> Dispatcher:
    dp = Dispatcher()
    sessions = session_factory(settings)
    mutation_executor = TelegramMutationExecutor(sessions)
    catalog_controller = CatalogController(
        mutation_executor,
        _catalog_session_use_cases,
        _enqueue_catalog_receipt,
    )
    draft_controller = DraftController(
        mutation_executor,
        _draft_session_use_cases,
        SqlAlchemyDraftConflictPresentationGuard(),
        _enqueue_draft_receipt,
    )
    draft_navigation_controller = DraftNavigationController(
        mutation_executor,
        _draft_navigation_session_use_cases,
        SqlAlchemyDraftPresentationGuard(),
        _enqueue_draft_navigation_receipt,
    )
    draft_rule_controller = DraftRuleController(
        mutation_executor,
        _draft_rule_session_use_cases,
        SqlAlchemyDraftPresentationGuard(),
        _enqueue_draft_rule_receipt,
    )
    draft_ingress_controller = DraftIngressController(
        mutation_executor,
        _draft_ingress_session_use_cases,
        _enqueue_draft_ingress_receipt,
    )
    local_ai_draft_controller = LocalAiDraftController(
        mutation_executor,
        SuggestLocalTransaction(_local_ai_provider(settings)),
        _local_ai_draft_session_use_cases,
        _enqueue_local_ai_receipt,
    )
    draft_conflict_controller = DraftConflictController(
        mutation_executor,
        _draft_conflict_session_use_cases,
        cast(
            DraftConflictPresentationContextReader,
            lock_telegram_draft_presentation_context,
        ),
        _enqueue_draft_conflict_receipt,
    )
    finance_draft_text_input_controller = FinanceDraftTextInputController(
        mutation_executor,
        _finance_draft_text_input_session_use_cases,
        SqlAlchemyFinanceDraftTextTargetRepository,
        _enqueue_finance_draft_text_input_receipt,
    )
    settings_text_input_controller = SettingsTextInputController(
        mutation_executor,
        _settings_text_input_session_use_cases,
        SqlAlchemySettingsTextInputRepository,
        cast(
            SettingsTextPresentationContextReader,
            lock_telegram_draft_presentation_context,
        ),
        _enqueue_settings_text_input_receipt,
    )
    settings_query_controller = SettingsQueryController(
        mutation_executor,
        SqlAlchemyQueryRepository,
        lambda session: DraftUseCases(SqlAlchemyDraftRepository(session)),
        _enqueue_settings_query_receipt,
    )
    settings_mutation_controller = SettingsMutationController(
        mutation_executor,
        _settings_mutation_session_use_cases,
        SqlAlchemyQueryRepository,
        lambda session: DraftUseCases(SqlAlchemyDraftRepository(session)),
        _enqueue_settings_query_receipt,
    )
    transaction_draft_navigation_controller = TransactionDraftNavigationController(
        mutation_executor,
        _transaction_draft_navigation_session_use_cases,
        SqlAlchemyDraftPresentationGuard(),
        _enqueue_transaction_draft_navigation_receipt,
    )
    transaction_draft_selection_controller = TransactionDraftSelectionController(
        mutation_executor,
        _transaction_draft_selection_session_use_cases,
        SqlAlchemyDraftPresentationGuard(),
        _enqueue_transaction_draft_selection_receipt,
    )
    transaction_edit_ingress_controller = TransactionEditIngressController(
        mutation_executor,
        _transaction_edit_session_use_cases,
        _enqueue_transaction_edit_receipt,
    )
    transaction_edit_text_input_controller = TransactionEditTextInputController(
        mutation_executor,
        _transaction_edit_text_input_session_use_cases,
        SqlAlchemyTransactionEditTextTargetRepository,
        cast(
            TransactionEditTextPresentationContextReader,
            lock_telegram_draft_presentation_context,
        ),
        _enqueue_transaction_edit_text_input_receipt,
    )
    draft_completion_controller = DraftCompletionController(
        mutation_executor,
        _draft_completion_session_use_cases,
        SqlAlchemyDraftPresentationGuard(),
        _enqueue_draft_completion_receipt,
    )
    undo_controller = UndoController(
        mutation_executor,
        _undo_session_use_cases,
        _enqueue_undo_receipt,
    )
    transaction_lifecycle_controller = TransactionLifecycleController(
        mutation_executor,
        _transaction_lifecycle_session_use_cases,
        _enqueue_transaction_lifecycle_receipt,
    )
    finance_query_controller = FinanceQueryController(
        mutation_executor,
        SqlAlchemyQueryRepository,
        _enqueue_finance_query_receipt,
        draft_factory=lambda session: DraftUseCases(SqlAlchemyDraftRepository(session)),
    )
    budget_controller = TelegramBudgetController(
        mutation_executor,
        SqlAlchemyBudgetRepository,
        lambda session: DraftUseCases(SqlAlchemyDraftRepository(session)),
        _enqueue_finance_query_receipt,
        miniapp_public_url=settings.miniapp_public_url,
    )
    recurring_controller = TelegramRecurringController(
        mutation_executor,
        SqlAlchemyRecurringRepository,
        _enqueue_finance_query_receipt,
        miniapp_public_url=settings.miniapp_public_url,
    )
    exchange_rate_controller = TelegramExchangeRateController(
        mutation_executor,
        SqlAlchemyExchangeRateRepository,
        _enqueue_finance_query_receipt,
        miniapp_public_url=settings.miniapp_public_url,
    )
    recurring_router = RecurringRouter(
        controller=recurring_controller,
        context_factory=lambda update_id, message, edit_message: (
            _telegram_query_context(settings, update_id, message)
            if edit_message
            else _telegram_initial_query_context(settings, update_id, message)
        ),
        deliver_message=lambda message, receipt: _deliver_untracked_finance_message(
            sessions,
            message,
            receipt,
        ),
        deliver_callback=_deliver_untracked_finance_query_callback,
    )
    finance_query_callbacks = FinanceQueryCallbackHandlers(
        finance_query_controller,
        lambda update_id, message: _telegram_query_context(settings, update_id, message),
        _deliver_untracked_finance_query_callback,
    )
    transaction_lifecycle_router = TransactionLifecycleRouter(
        controllers=TransactionLifecycleControllers(
            transactions=transaction_lifecycle_controller,
            repeats=draft_ingress_controller,
            edits=transaction_edit_ingress_controller,
            undo=undo_controller,
        ),
        contexts=TransactionLifecycleContexts(
            transaction=lambda update_id, message, history_page: (
                _telegram_transaction_lifecycle_context(
                    settings,
                    update_id,
                    message,
                    history_page,
                )
            ),
            repeat=lambda update_id, message, history_page: _telegram_draft_ingress_context(
                settings,
                update_id,
                message,
                edit_message=True,
                history_page=history_page,
            ),
            edit=lambda update_id, message, history_page: _telegram_transaction_edit_context(
                settings,
                update_id,
                message,
                history_page,
            ),
            undo=lambda update_id, message: TelegramUndoContext(
                _telegram_mutation_request(settings, update_id, message)
            ),
        ),
        deliveries=TransactionLifecycleDeliveries(
            transaction=_deliver_untracked_transaction_lifecycle,
            repeat=lambda message, receipt: _deliver_untracked_repeat_transaction(
                sessions,
                message,
                receipt,
            ),
            edit=lambda message, receipt: _deliver_untracked_transaction_edit(
                sessions,
                message,
                receipt,
            ),
            undo=_deliver_untracked_undo,
        ),
        confirm_legacy_delete=finance_query_callbacks.confirm_delete_transaction,
    )
    finance_message_router = FinanceMessageRouter(
        draft_ingress=draft_ingress_controller,
        finance_queries=finance_query_controller,
        draft_context=lambda update_id, message: _telegram_draft_ingress_context(
            settings,
            update_id,
            message,
            edit_message=False,
        ),
        finance_context=lambda update_id, message: _telegram_initial_query_context(
            settings,
            update_id,
            message,
        ),
        deliver_draft_untracked=lambda message, receipt: _deliver_untracked_wizard_message(
            sessions, message, receipt
        ),
        deliver_finance_untracked=lambda message, receipt: _deliver_untracked_finance_message(
            sessions, message, receipt
        ),
        budget_queries=budget_controller,
        budget_context=lambda update_id, message: _telegram_initial_query_context(
            settings,
            update_id,
            message,
        ),
        exchange_rate_queries=exchange_rate_controller,
        exchange_rate_context=lambda update_id, message: _telegram_initial_query_context(
            settings,
            update_id,
            message,
        ),
    )
    local_ai_router = LocalAiRouter(
        local_ai_draft_controller,
        lambda update_id, message: _telegram_draft_ingress_context(
            settings,
            update_id,
            message,
            edit_message=False,
        ),
        lambda message, receipt: _deliver_untracked_local_ai(
            sessions,
            message,
            receipt,
        ),
        ProcessedTelegramUpdateReader(sessions),
    )
    ocr = image_text_extractor or TesseractTextExtractor()
    ocr_image_controller = OcrImageController(
        mutation_executor,
        lambda session: _ocr_image_session_use_cases(session, ocr),
        _enqueue_ocr_image_receipt,
    )
    bank_import_key = settings.bank_import_key_bytes
    if bank_import_key is None:
        raise RuntimeError("Bank import security configuration is required")
    bank_import_preparer = BankImportPreparer(
        StrictBankCsvParser(),
        HmacBankImportDigester(bank_import_key),
    )
    bank_import_controller = TelegramBankImportController(
        mutation_executor,
        lambda session: BankImportUseCases(SqlAlchemyBankImportRepository(session)),
        _enqueue_finance_query_receipt,
        miniapp_public_url=settings.miniapp_public_url,
    )
    main_menu_router = MainMenuRouter(
        MainMenuController(
            mutation_executor,
            SqlAlchemyDraftRepository,
            enqueue_main_menu_receipt,
        ),
        MainMenuDirectDelivery(sessions),
        MainMenuRequestDefaults(
            owner_telegram_user_id=settings.owner_telegram_user_id,
            locale=settings.default_locale,
            timezone=settings.default_timezone,
            currency=settings.default_currency,
        ),
        miniapp_menu,
    )
    csv_export_delivery = CsvExportDelivery(sessions)
    csv_export_router = CsvExportRouter(
        CsvExportController(
            mutation_executor,
            SqlAlchemyDraftRepository,
            enqueue_csv_export_receipt,
        ),
        csv_export_delivery,
        CsvExportRequestDefaults(
            owner_telegram_user_id=settings.owner_telegram_user_id,
            locale=settings.default_locale,
            timezone=settings.default_timezone,
            currency=settings.default_currency,
        ),
    )
    text_input_router = TextInputRouter(
        finance_draft_text_input_controller,
        transaction_edit_text_input_controller,
        settings_text_input_controller,
        draft_ingress_controller,
        TextInputContextFactories(
            finance=lambda update_id, message: _telegram_finance_draft_text_input_context(
                settings,
                update_id,
                message,
            ),
            transaction=lambda update_id, message: _telegram_transaction_edit_text_input_context(
                settings,
                update_id,
                message,
            ),
            settings=lambda update_id, message: _telegram_settings_text_input_context(
                settings,
                update_id,
                message,
            ),
            quick=lambda update_id, message: _telegram_draft_ingress_context(
                settings,
                update_id,
                message,
                edit_message=False,
            ),
        ),
        TelegramTextInputDelivery(
            tracked=lambda message, update_id: _deliver_tracked_text_input_receipt(
                sessions,
                settings,
                message,
                update_id,
                special_delivery=csv_export_delivery,
            ),
            finance_direct=_deliver_untracked_finance_draft_text_input,
            bind_finance=lambda message, receipt, message_id: _bind_untracked_finance_text_input(
                sessions,
                message,
                receipt,
                message_id,
            ),
            transaction_direct=_deliver_untracked_transaction_edit_text_input,
            bind_transaction=lambda message, receipt, message_id, draft: (
                _bind_untracked_transaction_text_input(
                    sessions,
                    message,
                    receipt,
                    message_id,
                    draft,
                )
            ),
            settings_direct=_deliver_untracked_settings_text_input,
            bind_settings=lambda message, receipt, message_id, draft: (
                _bind_untracked_settings_text_input(
                    sessions,
                    message,
                    receipt,
                    message_id,
                    draft,
                )
            ),
            quick_direct=lambda message, receipt: _deliver_untracked_quick_text_input(
                sessions,
                message,
                receipt,
            ),
        ),
    )
    ocr_image_router = OcrImageRouter(
        ocr_image_controller,
        lambda update_id, message, content, mime_type: _telegram_ocr_image_context(
            settings,
            update_id,
            message,
            content,
            mime_type,
        ),
        BoundedTelegramImageDownloader(),
        ProcessedTelegramUpdateReader(sessions),
        OcrImageDirectDelivery(sessions, _ocr_image_receipt),
    )
    bank_import_router = TelegramBankImportRouter(
        bank_import_controller,
        lambda update_id, message: _telegram_mutation_request(
            settings,
            update_id,
            message,
        ),
        TelegramBankImportAccountResolver(sessions),
        BoundedTelegramBankCsvDownloader(),
        bank_import_preparer,
        ProcessedTelegramUpdateReader(sessions),
    )
    middleware = OwnerOnlyMiddleware(settings, sessions)
    outbox_middleware = TelegramResponseOutboxMiddleware(
        sessions,
        csv_export_delivery.deliver_job,
    )
    dp.message.outer_middleware(middleware)
    dp.message.outer_middleware(outbox_middleware)
    dp.callback_query.outer_middleware(middleware)
    dp.callback_query.outer_middleware(outbox_middleware)

    main_menu_router.register(dp)
    csv_export_router.register(dp)
    finance_message_router.register(dp)
    recurring_router.register(dp)
    local_ai_router.register(dp)

    transaction_lifecycle_router.register(dp)
    settings_router = SettingsRouter(
        query_controller=settings_query_controller,
        mutation_controller=settings_mutation_controller,
        query_context=lambda update_id, message, edit_message: _telegram_settings_query_context(
            settings,
            update_id,
            message,
            edit_message=edit_message,
        ),
        mutation_context=lambda update_id, message: _telegram_settings_mutation_context(
            settings,
            update_id,
            message,
        ),
        deliver_untracked=lambda message, receipt: _deliver_untracked_settings_query_receipt(
            sessions, message, receipt
        ),
    )
    catalog_callback_router = CatalogCallbackRouter(
        controller=catalog_controller,
        context=lambda update_id, message: _telegram_catalog_context(
            settings,
            update_id,
            message,
        ),
        deliver_untracked=_deliver_untracked_catalog_receipt,
    )
    draft_interaction_router = DraftInteractionRouter(
        controllers=DraftInteractionControllers(
            drafts=draft_controller,
            rules=draft_rule_controller,
            navigation=draft_navigation_controller,
            transaction_navigation=transaction_draft_navigation_controller,
            transaction_selection=transaction_draft_selection_controller,
            completion=draft_completion_controller,
            conflicts=draft_conflict_controller,
        ),
        contexts=DraftInteractionContexts(
            drafts=lambda update_id, message: _telegram_draft_context(
                settings,
                update_id,
                message,
            ),
            rules=lambda update_id, message: _telegram_draft_rule_context(
                settings,
                update_id,
                message,
            ),
            navigation=lambda update_id, message: _telegram_draft_navigation_context(
                settings,
                update_id,
                message,
            ),
            transaction_navigation=lambda update_id, message: (
                _telegram_transaction_draft_navigation_context(
                    settings,
                    update_id,
                    message,
                )
            ),
            transaction_selection=lambda update_id, message: (
                _telegram_transaction_draft_selection_context(
                    settings,
                    update_id,
                    message,
                )
            ),
            completion=lambda update_id, message: _telegram_draft_completion_context(
                settings,
                update_id,
                message,
            ),
            conflicts=lambda update_id, message: TelegramDraftConflictContext(
                _telegram_mutation_request(settings, update_id, message),
                message.message_id,
            ),
        ),
        renderers=DraftInteractionRenderers(
            drafts=_draft_settings_receipt,
            rules=_draft_rule_receipt,
            navigation=_draft_navigation_receipt,
            transaction_navigation=_transaction_draft_navigation_receipt,
            transaction_selection=_transaction_draft_selection_receipt,
            plain_completion=_plain_draft_receipt,
            ocr_completion=_ocr_queue_receipt,
            conflicts=_draft_conflict_receipt,
        ),
        delivery=DraftInteractionDelivery(
            replace_message=_replace_message,
            bind_presentation=cast(
                DraftPresentationBinder,
                partial(_bind_untracked_draft_interaction_presentation, sessions),
            ),
        ),
    )
    register_settings_routes(dp, settings_router)
    # CSV must be claimed before the generic OCR document route.
    bank_import_router.register(dp)
    ocr_image_router.register(dp)
    text_input_router.register(dp)

    register_ready_callbacks(
        dp,
        ReadyCallbackHandlers(
            history_page=finance_query_callbacks.history_page,
            view_transaction=finance_query_callbacks.view_transaction,
            confirm_delete_transaction=finance_query_callbacks.confirm_delete_transaction,
            trash_page=finance_query_callbacks.trash_page,
            trash_view=finance_query_callbacks.trash_view,
            set_default_account=catalog_callback_router.settings_account_default,
            archive_account=catalog_callback_router.settings_account_archive_do,
            restore_account=catalog_callback_router.settings_account_restore,
            archive_category=catalog_callback_router.settings_category_archive_do,
            restore_category=catalog_callback_router.settings_category_restore,
            versioned_draft=draft_interaction_router.versioned_draft,
        ),
    )

    register_late_callback_fallbacks(
        dp,
        FallbackCallbackHandlers(
            reject_legacy_draft=reject_legacy_draft_callback,
            stale_callback=reject_stale_callback,
        ),
    )

    return dp


async def run(settings: Settings) -> None:
    bot = Bot(settings.telegram_bot_token)
    bot.session.middleware(ReliableDeliveryMiddleware())
    miniapp_menu = MiniAppMenuConfigurator(
        owner_telegram_user_id=settings.owner_telegram_user_id,
        public_url=settings.miniapp_public_url,
    )
    dispatcher = build_dispatcher(settings, miniapp_menu=miniapp_menu)
    logger = logging.getLogger("finbot.lifecycle")
    try:
        # Keep one visible interface: the persistent reply keyboard. Slash commands
        # still work manually; the private owner menu launches the Mini App.
        await bot.delete_my_commands()
        await miniapp_menu.configure_startup(bot)
        logger.info("polling_started")
        try:
            await run_polling(bot, dispatcher)
        finally:
            logger.info("polling_stopped")
    finally:
        await bot.session.close()
