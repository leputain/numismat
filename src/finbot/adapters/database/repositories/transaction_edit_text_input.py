from collections.abc import Mapping
from copy import deepcopy
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import Draft, TelegramDraftPresentation, User
from finbot.adapters.database.repositories.transaction_edit_ingress import (
    SqlAlchemyTransactionEditTargetReader,
)
from finbot.application.draft_conflicts import PENDING_DRAFT_INTENT_KEY
from finbot.application.dto import DraftRef, DraftSnapshot, OwnerSnapshot
from finbot.application.errors import (
    DraftRevisionConflictError,
    EntityNotFoundError,
    InvalidStateError,
)
from finbot.application.transaction_edit_text_input import (
    TRANSACTION_EDIT_TEXT_STATES,
    TransactionEditTextInputNotApplicableError,
    TransactionEditTextTarget,
)

_PRESENTATION_KEYS = frozenset(
    {
        "presentation_ref",
        "telegram_chat_id",
        "telegram_message_id",
        "ui_message_id",
        "history_page",
        "pending_history_page",
    }
)


def _application_payload(payload: Mapping[str, object]) -> dict[str, object]:
    return deepcopy({key: value for key, value in payload.items() if key not in _PRESENTATION_KEYS})


def _snapshot(draft: Draft) -> DraftSnapshot:
    return DraftSnapshot(
        draft_id=draft.id,
        state=draft.state,
        payload=_application_payload(draft.payload),
        schema_version=draft.schema_version,
        revision=draft.revision,
        suspended=draft.suspended,
        updated_at=draft.updated_at,
    )


class SqlAlchemyTransactionEditTextTargetRepository:
    """Lock exact edit input state without owning commit or rollback."""

    __slots__ = ("_session", "_transactions")

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._transactions = SqlAlchemyTransactionEditTargetReader(session)

    async def _lock_owner(self, owner_id: UUID) -> User:
        owner = await self._session.scalar(
            select(User)
            .where(User.id == owner_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if owner is None:
            raise EntityNotFoundError("Владелец не найден")
        return owner

    async def _locked_draft(self, owner_id: UUID) -> Draft | None:
        return cast(
            Draft | None,
            await self._session.scalar(
                select(Draft)
                .where(Draft.user_id == owner_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ),
        )

    @staticmethod
    def _require_exact(draft: Draft | None, expected: DraftRef) -> Draft:
        if draft is None or draft.id != expected.draft_id or draft.revision != expected.revision:
            raise DraftRevisionConflictError(
                current_revision=draft.revision if draft is not None else None
            )
        return draft

    @staticmethod
    def _require_applicable(draft: Draft) -> None:
        if draft.suspended or draft.state not in TRANSACTION_EDIT_TEXT_STATES:
            raise TransactionEditTextInputNotApplicableError(
                "Активный черновик не принимает редактирование операции"
            )
        if PENDING_DRAFT_INTENT_KEY in draft.payload:
            raise InvalidStateError("Сначала выберите действие для незавершённого ввода")

    async def lock_active(self, owner_id: UUID) -> DraftSnapshot:
        """Determine and lock the active text target for adapter routing."""

        await self._lock_owner(owner_id)
        draft = await self._locked_draft(owner_id)
        if draft is None:
            raise TransactionEditTextInputNotApplicableError(
                "Нет активного черновика редактирования операции"
            )
        self._require_applicable(draft)
        return _snapshot(draft)

    async def presentation_message_id(
        self,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
    ) -> int | None:
        """Read the target id; the controller separately performs the exact guard."""

        owner = await self._lock_owner(owner_id)
        draft = self._require_exact(await self._locked_draft(owner_id), expected)
        self._require_applicable(draft)
        projection = (
            await self._session.execute(
                select(
                    TelegramDraftPresentation.chat_id,
                    TelegramDraftPresentation.message_id,
                )
                .where(
                    TelegramDraftPresentation.draft_id == draft.id,
                    TelegramDraftPresentation.rendered_revision == draft.revision,
                )
                .with_for_update()
            )
        ).one_or_none()
        if projection is not None:
            projected_chat_id, message_id = projection._t
            return int(message_id) if projected_chat_id == chat_id else None

        if owner.telegram_chat_id != chat_id:
            return None
        legacy = draft.payload.get("ui_message_id", draft.presentation_ref)
        try:
            legacy_message_id = int(str(legacy)) if legacy is not None else None
        except ValueError:
            return None
        return (
            legacy_message_id if legacy_message_id is not None and legacy_message_id > 0 else None
        )

    async def lock_target(
        self,
        owner_id: UUID,
        expected: DraftRef,
    ) -> TransactionEditTextTarget:
        """Lock owner, exact draft, then its versioned active transaction."""

        owner = await self._lock_owner(owner_id)
        draft = self._require_exact(await self._locked_draft(owner_id), expected)
        self._require_applicable(draft)
        payload = _application_payload(draft.payload)
        transaction_id_raw = payload.get("transaction_id")
        version_raw = payload.get("version")
        if isinstance(version_raw, bool):
            raise InvalidStateError("Черновик редактирования операции повреждён")
        try:
            transaction_id = UUID(str(transaction_id_raw))
            expected_version = int(str(version_raw))
        except TypeError, ValueError:
            raise InvalidStateError("Черновик редактирования операции повреждён") from None
        if expected_version < 1:
            raise InvalidStateError("Черновик редактирования операции повреждён")

        transaction = await self._transactions(
            owner_id,
            transaction_id,
            expected_version,
        )
        return TransactionEditTextTarget(
            owner=OwnerSnapshot(
                owner_id=owner.id,
                locale=owner.locale,
                timezone=owner.timezone,
                base_currency=owner.base_currency,
                default_account_id=owner.default_account_id,
                fast_mode=owner.fast_mode,
                settings_version=owner.settings_version,
            ),
            draft=_snapshot(draft),
            transaction=transaction,
        )
