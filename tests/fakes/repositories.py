from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid7

from finbot.application.dto import DraftRef, DraftSnapshot
from finbot.application.errors import ActiveDraftConflictError, DraftRevisionConflictError


class InMemoryDraftRepository:
    """A strict single-active-draft fake with the same CAS contract as PostgreSQL."""

    def __init__(self) -> None:
        self._drafts: dict[UUID, DraftSnapshot] = {}

    @staticmethod
    def _copy(draft: DraftSnapshot) -> DraftSnapshot:
        return DraftSnapshot(
            draft_id=draft.draft_id,
            state=draft.state,
            payload=deepcopy(dict(draft.payload)),
            schema_version=draft.schema_version,
            revision=draft.revision,
            suspended=draft.suspended,
            updated_at=draft.updated_at,
        )

    def _require(self, owner_id: UUID, expected: DraftRef) -> DraftSnapshot:
        current = self._drafts.get(owner_id)
        if current is None or current.ref != expected:
            raise DraftRevisionConflictError(
                current_revision=current.revision if current is not None else None
            )
        return current

    async def get_active(self, owner_id: UUID) -> DraftSnapshot | None:
        draft = self._drafts.get(owner_id)
        return self._copy(draft) if draft is not None else None

    async def lock_active(
        self,
        owner_id: UUID,
        expected: DraftRef,
    ) -> DraftSnapshot:
        return self._copy(self._require(owner_id, expected))

    async def create_if_absent(
        self, owner_id: UUID, state: str, payload: Mapping[str, Any]
    ) -> DraftSnapshot:
        current = self._drafts.get(owner_id)
        if current is not None:
            raise ActiveDraftConflictError(current_revision=current.revision)
        draft = DraftSnapshot(
            draft_id=uuid7(),
            state=state,
            payload=deepcopy(dict(payload)),
            updated_at=datetime.now(UTC),
        )
        self._drafts[owner_id] = draft
        return self._copy(draft)

    async def update(
        self,
        owner_id: UUID,
        expected: DraftRef,
        state: str,
        payload: Mapping[str, Any],
    ) -> DraftSnapshot:
        current = self._require(owner_id, expected)
        draft = DraftSnapshot(
            draft_id=current.draft_id,
            state=state,
            payload=deepcopy(dict(payload)),
            schema_version=current.schema_version,
            revision=current.revision + 1,
            suspended=current.suspended,
            updated_at=datetime.now(UTC),
        )
        self._drafts[owner_id] = draft
        return self._copy(draft)

    async def continue_existing(
        self,
        owner_id: UUID,
        expected: DraftRef,
        state: str,
        payload: Mapping[str, Any],
    ) -> DraftSnapshot:
        current = self._require(owner_id, expected)
        draft = DraftSnapshot(
            draft_id=current.draft_id,
            state=state,
            payload=deepcopy(dict(payload)),
            schema_version=current.schema_version,
            revision=current.revision + 1,
            suspended=False,
            updated_at=datetime.now(UTC),
        )
        self._drafts[owner_id] = draft
        return self._copy(draft)

    async def replace(
        self,
        owner_id: UUID,
        expected: DraftRef,
        state: str,
        payload: Mapping[str, Any],
    ) -> DraftSnapshot:
        self._require(owner_id, expected)
        del self._drafts[owner_id]
        return await self.create_if_absent(owner_id, state, payload)

    async def set_suspended(
        self, owner_id: UUID, expected: DraftRef, suspended: bool
    ) -> DraftSnapshot:
        current = self._require(owner_id, expected)
        if current.suspended == suspended:
            return self._copy(current)
        draft = DraftSnapshot(
            draft_id=current.draft_id,
            state=current.state,
            payload=current.payload,
            schema_version=current.schema_version,
            revision=current.revision + 1,
            suspended=suspended,
            updated_at=datetime.now(UTC),
        )
        self._drafts[owner_id] = draft
        return self._copy(draft)

    async def delete(self, owner_id: UUID, expected: DraftRef) -> None:
        self._require(owner_id, expected)
        del self._drafts[owner_id]
