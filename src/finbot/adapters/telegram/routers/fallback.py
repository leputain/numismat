"""Late callback fallbacks for retired and otherwise stale Telegram buttons.

Do not wire this module while the legacy ``w:/d:/e:`` handlers are still
registered ahead of it in the composition root.  Those handlers would consume
retired payloads before the rejection filter.  During the incremental rollout,
keep the existing early legacy rejection in place and register only the ready
callback family; move both fallbacks here after the raw legacy handlers have
been removed or reordered.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from aiogram import Dispatcher, F
from aiogram.types import CallbackQuery

type CallbackFallbackHandler = Callable[[CallbackQuery], Awaitable[None]]


async def reject_legacy_draft_callback(callback: CallbackQuery) -> None:
    """Reject retired wizard/edit callback namespaces without touching state."""

    await callback.answer(
        "Эта кнопка относится к старой версии формы. Откройте актуальный черновик",
        show_alert=True,
    )


async def reject_stale_callback(callback: CallbackQuery) -> None:
    """Fail closed for every callback not claimed by a supported router."""

    await callback.answer(
        "Эта кнопка устарела. Откройте меню или историю",
        show_alert=True,
    )


@dataclass(frozen=True, slots=True)
class FallbackCallbackHandlers:
    """Injected late handlers; their closures are deliberately hidden from repr."""

    reject_legacy_draft: CallbackFallbackHandler = field(repr=False)
    stale_callback: CallbackFallbackHandler = field(repr=False)


def register_late_callback_fallbacks(
    dispatcher: Dispatcher,
    handlers: FallbackCallbackHandlers,
) -> None:
    """Register legacy rejection first and the universal fallback last.

    Call this only after every supported specific callback family, including
    :func:`register_ready_callbacks`, has been registered on the same root
    dispatcher, and only when no retired ``w:/d:/e:`` handler precedes this
    rejection filter.
    """

    observer = dispatcher.callback_query
    observer.register(
        handlers.reject_legacy_draft,
        F.data.startswith("w:") | F.data.startswith("d:") | F.data.startswith("e:"),
    )
    observer.register(handlers.stale_callback)


__all__ = [
    "FallbackCallbackHandlers",
    "register_late_callback_fallbacks",
    "reject_legacy_draft_callback",
    "reject_stale_callback",
]
