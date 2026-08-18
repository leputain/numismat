"""Register callback families whose controllers are ready for bootstrap wiring.

The helpers intentionally target the root :class:`aiogram.Dispatcher`.  The
legacy composition root still owns broad callback catch-alls, so including a
child router after those handlers would make the extracted routes unreachable.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from aiogram import Dispatcher, F
from aiogram.types import CallbackQuery

type TrackedCallbackHandler = Callable[[CallbackQuery, int | None], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ReadyCallbackHandlers:
    """Injected handlers, kept opaque because closures may capture private context."""

    history_page: TrackedCallbackHandler = field(repr=False)
    view_transaction: TrackedCallbackHandler = field(repr=False)
    confirm_delete_transaction: TrackedCallbackHandler = field(repr=False)
    trash_page: TrackedCallbackHandler = field(repr=False)
    trash_view: TrackedCallbackHandler = field(repr=False)
    set_default_account: TrackedCallbackHandler = field(repr=False)
    archive_account: TrackedCallbackHandler = field(repr=False)
    restore_account: TrackedCallbackHandler = field(repr=False)
    archive_category: TrackedCallbackHandler = field(repr=False)
    restore_category: TrackedCallbackHandler = field(repr=False)
    versioned_draft: TrackedCallbackHandler = field(repr=False)


def register_ready_callbacks(
    dispatcher: Dispatcher,
    handlers: ReadyCallbackHandlers,
) -> None:
    """Register exact ready call sites in their legacy relative order.

    ``d:`` is the retired callback namespace and belongs to the late legacy
    rejection handler.  Excluding it here preserves that response while still
    delegating every compact versioned callback beginning with ``d``.
    """

    observer = dispatcher.callback_query
    observer.register(handlers.history_page, F.data.startswith("h:"))
    observer.register(handlers.view_transaction, F.data.startswith("tx:view:"))
    observer.register(
        handlers.confirm_delete_transaction,
        F.data.startswith("tx:del:ask:"),
    )
    observer.register(handlers.trash_page, F.data.startswith("z:list:"))
    observer.register(handlers.trash_view, F.data.startswith("z:view:"))
    observer.register(handlers.set_default_account, F.data.startswith("sa:default:"))
    observer.register(handlers.archive_account, F.data.startswith("sa:archive:do:"))
    observer.register(handlers.restore_account, F.data.startswith("sa:restore:"))
    observer.register(handlers.archive_category, F.data.startswith("sc:archive:do:"))
    observer.register(handlers.restore_category, F.data.startswith("sc:restore:"))
    observer.register(
        handlers.versioned_draft,
        F.data.startswith("d") & ~F.data.startswith("d:"),
    )
