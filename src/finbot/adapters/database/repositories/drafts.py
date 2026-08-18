from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid7

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import (
    Draft,
    ImportBatch,
    ImportRow,
    RecurringInstance,
    User,
)
from finbot.application.draft_conflicts import PENDING_DRAFT_INTENT_KEY
from finbot.application.dto import DraftRef, DraftSnapshot
from finbot.application.errors import (
    ActiveDraftConflictError,
    DraftRevisionConflictError,
    EntityNotFoundError,
    InvalidStateError,
)

_PRESENTATION_KEYS = frozenset(
    {"presentation_ref", "telegram_chat_id", "telegram_message_id", "ui_message_id"}
)
_IMPORT_DRAFT_STATES = frozenset({"review", "review_category", "wizard_description"})
_IMPORT_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {PENDING_DRAFT_INTENT_KEY, "ocr_batch", "pending_rule", "rule_offer_pattern"}
)
_MAX_VERSION = 2**31 - 1


def _next_provenance_version(current: int) -> int:
    if current >= _MAX_VERSION:
        raise InvalidStateError("Достигнут предел версий источника черновика")
    return current + 1


def _require_import_payload(
    row: ImportRow,
    account_id: UUID,
    payload: Mapping[str, Any],
) -> None:
    try:
        occurred_at = datetime.fromisoformat(str(payload.get("occurred_at", "")))
        amount_minor = int(payload.get("amount_minor", 0))
        payload_account_id = UUID(str(payload.get("account_id", "")))
    except TypeError, ValueError:
        raise InvalidStateError("Черновик импорта повреждён") from None
    if occurred_at.utcoffset() is None:
        raise InvalidStateError("Черновик импорта повреждён")
    if (
        payload.get("flow") != "bank_import"
        or _IMPORT_FORBIDDEN_PAYLOAD_KEYS.intersection(payload)
        or payload.get("type") != row.type
        or amount_minor != row.amount_minor
        or payload_account_id != account_id
        or payload.get("currency") != row.currency
        or occurred_at.astimezone(UTC) != row.occurred_at.astimezone(UTC)
    ):
        raise InvalidStateError("Поля банковской операции нельзя изменить до сверки")


def _application_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """Hide legacy adapter presentation fields from the application layer."""

    return deepcopy({key: value for key, value in payload.items() if key not in _PRESENTATION_KEYS})


def _stored_payload(
    payload: Mapping[str, Any],
    *,
    existing: Mapping[str, object] | None = None,
) -> dict[str, object]:
    leaked = _PRESENTATION_KEYS.intersection(payload)
    if leaked:
        raise ValueError("Draft payload must not contain channel presentation state")
    preserved = (
        {key: deepcopy(value) for key, value in existing.items() if key in _PRESENTATION_KEYS}
        if existing is not None
        else {}
    )
    preserved.update(deepcopy(dict(payload)))
    return preserved


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


class SqlAlchemyDraftRepository:
    """PostgreSQL-backed single-active-draft repository with CAS mutations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _lock_owner(self, owner_id: UUID) -> None:
        owner = await self._session.scalar(
            select(User)
            .where(User.id == owner_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if owner is None:
            raise EntityNotFoundError("Владелец не найден")

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

    async def _insert(
        self,
        owner_id: UUID,
        state: str,
        payload: Mapping[str, Any],
    ) -> Draft | None:
        draft_id = uuid7()
        statement = (
            insert(Draft)
            .values(
                id=draft_id,
                user_id=owner_id,
                state=state,
                payload=_stored_payload(payload),
            )
            .on_conflict_do_nothing(index_elements=(Draft.user_id,))
            .returning(Draft.id)
        )
        inserted_id = (await self._session.execute(statement)).scalar_one_or_none()
        if inserted_id is None:
            return None
        return cast(
            Draft | None,
            await self._session.scalar(
                select(Draft)
                .where(Draft.id == inserted_id)
                .execution_options(populate_existing=True)
            ),
        )

    @staticmethod
    def _require(draft: Draft | None, expected: DraftRef) -> Draft:
        if draft is None or draft.id != expected.draft_id or draft.revision != expected.revision:
            raise DraftRevisionConflictError(
                current_revision=draft.revision if draft is not None else None
            )
        return draft

    async def _refresh_after_flush(self, draft: Draft) -> Draft:
        """Reload server-managed timestamps before building an async DTO."""

        refreshed = await self._session.scalar(
            select(Draft).where(Draft.id == draft.id).execution_options(populate_existing=True)
        )
        if refreshed is None:  # pragma: no cover - the row was just flushed
            raise RuntimeError("Сохранённый черновик не найден")
        return refreshed

    async def _require_import_update(
        self,
        owner_id: UUID,
        draft_id: UUID,
        state: str,
        payload: Mapping[str, Any],
    ) -> None:
        linked = (
            await self._session.execute(
                select(ImportRow, ImportBatch.account_id)
                .join(ImportBatch, ImportBatch.id == ImportRow.batch_id)
                .where(
                    ImportRow.user_id == owner_id,
                    ImportRow.draft_id == draft_id,
                )
                .with_for_update(of=ImportRow)
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if linked is not None:
            row, account_id = linked._t
            if state not in _IMPORT_DRAFT_STATES:
                raise InvalidStateError("Этот переход недоступен для банковского импорта")
            _require_import_payload(row, account_id, payload)

    async def _detach_draft_provenance(self, draft: Draft) -> None:
        recurring = await self._session.scalar(
            select(RecurringInstance)
            .where(
                RecurringInstance.user_id == draft.user_id,
                RecurringInstance.draft_id == draft.id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        import_row = await self._session.scalar(
            select(ImportRow)
            .where(ImportRow.user_id == draft.user_id, ImportRow.draft_id == draft.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if recurring is not None and import_row is not None:
            raise InvalidStateError("Черновик содержит конфликтующие источники")
        now = datetime.now(UTC)
        if recurring is not None:
            recurring.draft_id = None
            recurring.version = _next_provenance_version(recurring.version)
            recurring.updated_at = now
        if import_row is not None:
            batch = await self._session.scalar(
                select(ImportBatch)
                .where(
                    ImportBatch.id == import_row.batch_id,
                    ImportBatch.user_id == draft.user_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if batch is None or batch.status != "open" or import_row.status != "staged":
                raise InvalidStateError("Источник черновика импорта повреждён")
            import_row.draft_id = None
            import_row.version = _next_provenance_version(import_row.version)
            import_row.updated_at = now
            batch.version = _next_provenance_version(batch.version)
            batch.updated_at = now

    async def get_active(self, owner_id: UUID) -> DraftSnapshot | None:
        draft = cast(
            Draft | None,
            await self._session.scalar(
                select(Draft)
                .where(Draft.user_id == owner_id)
                .execution_options(populate_existing=True)
            ),
        )
        return _snapshot(draft) if draft is not None else None

    async def lock_active(
        self,
        owner_id: UUID,
        expected: DraftRef,
    ) -> DraftSnapshot:
        """Serialize conflict resolution on the owner slot and exact draft row."""

        await self._lock_owner(owner_id)
        draft = self._require(await self._locked_draft(owner_id), expected)
        return _snapshot(draft)

    async def create_if_absent(
        self,
        owner_id: UUID,
        state: str,
        payload: Mapping[str, Any],
    ) -> DraftSnapshot:
        # The durable owner row is the mutex for the owner's one-draft slot.
        # ON CONFLICT is a final guard for writers outside this repository.
        await self._lock_owner(owner_id)
        current = await self._locked_draft(owner_id)
        if current is not None:
            raise ActiveDraftConflictError(current_revision=current.revision)
        draft = await self._insert(owner_id, state, payload)
        if draft is None:
            current = await self._locked_draft(owner_id)
            raise ActiveDraftConflictError(
                current_revision=current.revision if current is not None else 1
            )
        return _snapshot(draft)

    async def update(
        self,
        owner_id: UUID,
        expected: DraftRef,
        state: str,
        payload: Mapping[str, Any],
    ) -> DraftSnapshot:
        draft = self._require(await self._locked_draft(owner_id), expected)
        await self._require_import_update(owner_id, draft.id, state, payload)
        draft.state = state
        draft.payload = _stored_payload(payload, existing=draft.payload)
        draft.revision += 1
        await self._session.flush()
        return _snapshot(await self._refresh_after_flush(draft))

    async def continue_existing(
        self,
        owner_id: UUID,
        expected: DraftRef,
        state: str,
        payload: Mapping[str, Any],
    ) -> DraftSnapshot:
        """Discard conflict metadata and resume an exact draft in one revision."""

        draft = self._require(await self._locked_draft(owner_id), expected)
        await self._require_import_update(owner_id, draft.id, state, payload)
        draft.state = state
        draft.payload = _stored_payload(payload, existing=draft.payload)
        draft.suspended = False
        draft.revision += 1
        await self._session.flush()
        return _snapshot(await self._refresh_after_flush(draft))

    async def replace(
        self,
        owner_id: UUID,
        expected: DraftRef,
        state: str,
        payload: Mapping[str, Any],
    ) -> DraftSnapshot:
        await self._lock_owner(owner_id)
        current = self._require(await self._locked_draft(owner_id), expected)
        await self._detach_draft_provenance(current)
        await self._session.delete(current)
        await self._session.flush()
        replacement = await self._insert(owner_id, state, payload)
        if replacement is None:  # pragma: no cover - owner lock makes this defensive only
            conflict = await self._locked_draft(owner_id)
            raise ActiveDraftConflictError(
                current_revision=conflict.revision if conflict is not None else 1
            )
        return _snapshot(replacement)

    async def set_suspended(
        self,
        owner_id: UUID,
        expected: DraftRef,
        suspended: bool,
    ) -> DraftSnapshot:
        draft = self._require(await self._locked_draft(owner_id), expected)
        if draft.suspended != suspended:
            draft.suspended = suspended
            draft.revision += 1
            await self._session.flush()
            draft = await self._refresh_after_flush(draft)
        return _snapshot(draft)

    async def delete(self, owner_id: UUID, expected: DraftRef) -> None:
        await self._lock_owner(owner_id)
        draft = self._require(await self._locked_draft(owner_id), expected)
        await self._detach_draft_provenance(draft)
        await self._session.delete(draft)
        await self._session.flush()
