from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import Draft, TelegramDraftPresentation, User
from finbot.application.dto import DraftRef
from finbot.application.interactions import MAX_PAGE


def _validate_history_page(name: str, value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not 0 <= value <= MAX_PAGE:
        raise ValueError(f"{name} must be between 0 and {MAX_PAGE}")


@dataclass(frozen=True, slots=True)
class TelegramDraftPresentationContext:
    """Bounded adapter-only navigation state for one exact rendered revision."""

    history_page: int | None = field(default=None, repr=False)
    pending_history_page: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _validate_history_page("History page", self.history_page)
        _validate_history_page("Pending history page", self.pending_history_page)


async def lock_telegram_draft_presentation_context_by_revision(
    session: AsyncSession,
    draft_id: UUID,
    rendered_revision: int,
) -> TelegramDraftPresentationContext | None:
    """Return the locked adapter context for one exact historical projection.

    This supports conflict producers after they increment the business draft:
    the prior rendered revision still carries the current-flow page while the
    newly requested action contributes its independent pending page.
    """
    if not isinstance(draft_id, UUID):
        raise TypeError("Draft id must be a UUID")
    if (
        isinstance(rendered_revision, bool)
        or not isinstance(rendered_revision, int)
        or rendered_revision < 1
    ):
        raise ValueError("Rendered draft revision must be positive")
    projection = (
        await session.execute(
            select(
                TelegramDraftPresentation.history_page,
                TelegramDraftPresentation.pending_history_page,
            )
            .where(
                TelegramDraftPresentation.draft_id == draft_id,
                TelegramDraftPresentation.rendered_revision == rendered_revision,
            )
            .with_for_update()
        )
    ).one_or_none()
    if projection is None:
        return None
    history_page, pending_history_page = projection._t
    return TelegramDraftPresentationContext(history_page, pending_history_page)


async def lock_current_or_prior_telegram_draft_presentation_context(
    session: AsyncSession,
    expected: DraftRef,
) -> TelegramDraftPresentationContext:
    """Preserve context whether suspension kept or incremented the draft revision."""

    current = await lock_telegram_draft_presentation_context_by_revision(
        session,
        expected.draft_id,
        expected.revision,
    )
    if current is not None or expected.revision == 1:
        return current or TelegramDraftPresentationContext()
    prior = await lock_telegram_draft_presentation_context_by_revision(
        session,
        expected.draft_id,
        expected.revision - 1,
    )
    return prior or TelegramDraftPresentationContext()


async def lock_telegram_draft_presentation_context(
    session: AsyncSession,
    owner_id: UUID,
    expected: DraftRef,
    chat_id: int,
    message_id: int,
    *,
    allow_suspended: bool = False,
) -> TelegramDraftPresentationContext | None:
    """Lock and return context only for the exact owner/draft/message projection.

    A rolling-deploy legacy binding remains valid but has no durable navigation
    context, so it returns a context whose two fields are ``None``. A present but
    mismatched projection is authoritative and fails closed.
    """
    owner_exists = await session.scalar(
        select(User.id)
        .where(User.id == owner_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if owner_exists is None:
        return None
    draft = await session.scalar(
        select(Draft)
        .where(Draft.user_id == owner_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        draft is None
        or draft.id != expected.draft_id
        or draft.revision != expected.revision
        or (draft.suspended and not allow_suspended)
    ):
        return None

    projection = (
        await session.execute(
            select(
                TelegramDraftPresentation.chat_id,
                TelegramDraftPresentation.message_id,
                TelegramDraftPresentation.history_page,
                TelegramDraftPresentation.pending_history_page,
            ).where(
                TelegramDraftPresentation.draft_id == draft.id,
                TelegramDraftPresentation.rendered_revision == draft.revision,
            )
        )
    ).one_or_none()
    if projection is not None:
        (
            projected_chat_id,
            projected_message_id,
            history_page,
            pending_history_page,
        ) = projection._t
        if projected_chat_id != chat_id or projected_message_id != message_id:
            return None
        return TelegramDraftPresentationContext(history_page, pending_history_page)

    legacy_message_id = draft.payload.get("ui_message_id")
    if legacy_message_id is None:
        legacy_message_id = draft.presentation_ref
    try:
        matches = legacy_message_id is not None and int(str(legacy_message_id)) == message_id
    except ValueError:
        return None
    return TelegramDraftPresentationContext() if matches else None


async def lock_telegram_draft_presentation_message(
    session: AsyncSession,
    owner_id: UUID,
    expected: DraftRef,
    message_id: int,
    *,
    allow_suspended: bool = False,
) -> bool:
    """Lock and validate a message binding when the caller has no chat id.

    This compatibility boundary is intentionally database-adapter-local. New
    controllers should prefer the full presentation-context reader, which also
    validates the chat id and returns bounded navigation context.
    """

    owner_exists = await session.scalar(
        select(User.id)
        .where(User.id == owner_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if owner_exists is None:
        return False
    draft = await session.scalar(
        select(Draft)
        .where(Draft.user_id == owner_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        draft is None
        or draft.id != expected.draft_id
        or draft.revision != expected.revision
        or (draft.suspended and not allow_suspended)
    ):
        return False

    projected_message_id = await session.scalar(
        select(TelegramDraftPresentation.message_id).where(
            TelegramDraftPresentation.draft_id == draft.id,
            TelegramDraftPresentation.rendered_revision == draft.revision,
        )
    )
    if projected_message_id is not None:
        return projected_message_id == message_id

    legacy_message_id = draft.payload.get("ui_message_id")
    if legacy_message_id is None:
        legacy_message_id = draft.presentation_ref
    try:
        return legacy_message_id is not None and int(str(legacy_message_id)) == message_id
    except ValueError:
        return False


class SqlAlchemyDraftPresentationGuard:
    """Lock and validate the Telegram message presenting one exact draft revision."""

    __slots__ = ()

    async def __call__(
        self,
        session: AsyncSession,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
        message_id: int,
    ) -> bool:
        return (
            await lock_telegram_draft_presentation_context(
                session,
                owner_id,
                expected,
                chat_id,
                message_id,
                allow_suspended=False,
            )
            is not None
        )


class SqlAlchemyDraftConflictPresentationGuard:
    """Validate the exact conflict/resume message, including suspended drafts."""

    __slots__ = ()

    async def __call__(
        self,
        session: AsyncSession,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
        message_id: int,
    ) -> bool:
        return (
            await lock_telegram_draft_presentation_context(
                session,
                owner_id,
                expected,
                chat_id,
                message_id,
                allow_suspended=True,
            )
            is not None
        )
