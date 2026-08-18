from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finbot.adapters.database.repositories.draft_presentations import (
    TelegramDraftPresentationContext,
    lock_current_or_prior_telegram_draft_presentation_context,
)
from finbot.adapters.database.services.outbox import (
    bind_telegram_draft_presentation,
    queue_send_message,
)
from finbot.adapters.telegram.controllers.main_menu import (
    MainMenuReceiptSnapshot,
    render_main_menu,
)
from finbot.adapters.telegram.executor import (
    TelegramMutationRequest,
    TelegramMutationSession,
)
from finbot.adapters.telegram.ui import resume_draft_keyboard

_RESUME_NOTICE = "Незавершённый ввод сохранён и приостановлен."


async def _presentation_context(
    session: TelegramMutationSession,
    receipt: MainMenuReceiptSnapshot,
) -> TelegramDraftPresentationContext:
    suspended = receipt.suspended_draft
    if suspended is None:  # pragma: no cover - guarded by both callers
        return TelegramDraftPresentationContext()
    return await lock_current_or_prior_telegram_draft_presentation_context(
        session,
        suspended,
    )


async def enqueue_main_menu_receipt(
    session: TelegramMutationSession,
    request: TelegramMutationRequest,
    receipt: MainMenuReceiptSnapshot,
) -> None:
    if request.update_id is None:
        raise ValueError("Tracked main-menu receipt requires an update id")
    message = render_main_menu(receipt.action)
    queue_send_message(
        session,
        update_id=request.update_id,
        owner_telegram_user_id=request.owner_telegram_user_id,
        chat_id=request.chat_id,
        text=message.text,
        parse_mode=message.parse_mode,
        reply_markup=message.reply_markup,
    )
    if receipt.suspended_draft is None:
        return
    context = await _presentation_context(session, receipt)
    queue_send_message(
        session,
        update_id=request.update_id,
        owner_telegram_user_id=request.owner_telegram_user_id,
        chat_id=request.chat_id,
        text=_RESUME_NOTICE,
        reply_markup=resume_draft_keyboard(
            receipt.suspended_draft.draft_id,
            receipt.suspended_draft.revision,
        ),
        draft_id=receipt.suspended_draft.draft_id,
        draft_revision=receipt.suspended_draft.revision,
        history_page=context.history_page,
        pending_history_page=context.pending_history_page,
        sequence=1,
    )


class MainMenuDirectDelivery:
    """Deliver untracked receipts only after the controller committed its UoW."""

    __slots__ = ("_sessions",)

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def deliver(
        self,
        message: Message,
        receipt: MainMenuReceiptSnapshot,
    ) -> None:
        primary = render_main_menu(receipt.action)
        await message.answer(
            primary.text,
            parse_mode=primary.parse_mode,
            reply_markup=primary.reply_markup,
        )
        if receipt.suspended_draft is None:
            return
        sent = await message.answer(
            _RESUME_NOTICE,
            reply_markup=resume_draft_keyboard(
                receipt.suspended_draft.draft_id,
                receipt.suspended_draft.revision,
            ),
        )
        async with self._sessions() as session, session.begin():
            context = await _presentation_context(session, receipt)
            await bind_telegram_draft_presentation(
                session,
                draft_id=receipt.suspended_draft.draft_id,
                draft_revision=receipt.suspended_draft.revision,
                chat_id=message.chat.id,
                message_id=sent.message_id,
                history_page=context.history_page,
                pending_history_page=context.pending_history_page,
            )


__all__ = ["MainMenuDirectDelivery", "enqueue_main_menu_receipt"]
