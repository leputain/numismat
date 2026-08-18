"""Registration helpers for incrementally extracted Telegram callback families."""

from finbot.adapters.telegram.routers.catalog_callbacks import CatalogCallbackRouter
from finbot.adapters.telegram.routers.draft_interactions import (
    DraftInteractionContexts,
    DraftInteractionControllers,
    DraftInteractionDelivery,
    DraftInteractionRenderers,
    DraftInteractionRouter,
)
from finbot.adapters.telegram.routers.exports import (
    CsvExportDirectDelivery,
    CsvExportRequestDefaults,
    CsvExportRouter,
)
from finbot.adapters.telegram.routers.fallback import (
    FallbackCallbackHandlers,
    register_late_callback_fallbacks,
    reject_legacy_draft_callback,
    reject_stale_callback,
)
from finbot.adapters.telegram.routers.finance_messages import FinanceMessageRouter
from finbot.adapters.telegram.routers.finance_queries import FinanceQueryCallbackHandlers
from finbot.adapters.telegram.routers.inputs import (
    OcrImageRouter,
    TextInputContextFactories,
    TextInputRouter,
)
from finbot.adapters.telegram.routers.main_menu import (
    MainMenuReceiptDelivery,
    MainMenuRequestDefaults,
    MainMenuRouter,
)
from finbot.adapters.telegram.routers.ready_callbacks import (
    ReadyCallbackHandlers,
    register_ready_callbacks,
)
from finbot.adapters.telegram.routers.settings import SettingsRouter, register_settings_routes
from finbot.adapters.telegram.routers.transaction_lifecycle import (
    TransactionLifecycleContexts,
    TransactionLifecycleControllers,
    TransactionLifecycleDeliveries,
    TransactionLifecycleRouter,
)

__all__ = [
    "CatalogCallbackRouter",
    "DraftInteractionContexts",
    "DraftInteractionControllers",
    "DraftInteractionDelivery",
    "DraftInteractionRenderers",
    "DraftInteractionRouter",
    "FallbackCallbackHandlers",
    "CsvExportDirectDelivery",
    "CsvExportRequestDefaults",
    "CsvExportRouter",
    "FinanceQueryCallbackHandlers",
    "FinanceMessageRouter",
    "MainMenuReceiptDelivery",
    "MainMenuRequestDefaults",
    "MainMenuRouter",
    "OcrImageRouter",
    "ReadyCallbackHandlers",
    "SettingsRouter",
    "TextInputContextFactories",
    "TextInputRouter",
    "TransactionLifecycleContexts",
    "TransactionLifecycleControllers",
    "TransactionLifecycleDeliveries",
    "TransactionLifecycleRouter",
    "register_late_callback_fallbacks",
    "register_ready_callbacks",
    "register_settings_routes",
    "reject_legacy_draft_callback",
    "reject_stale_callback",
]
