from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import Draft
from finbot.adapters.database.repositories.draft_presentations import (
    SqlAlchemyDraftConflictPresentationGuard,
    SqlAlchemyDraftPresentationGuard,
    TelegramDraftPresentationContext,
    lock_telegram_draft_presentation_context,
    lock_telegram_draft_presentation_context_by_revision,
)
from finbot.application.dto import DraftRef

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000401")


class _ProjectionResult:
    def __init__(self, projection: tuple[int, ...] | None) -> None:
        self._projection = projection

    def one_or_none(self) -> Any:
        if self._projection is None:
            return None
        projection = self._projection
        if len(projection) == 2:
            projection = (*projection, None, None)
        return type("ProjectionRow", (), {"_t": projection})()


class _Session:
    def __init__(
        self,
        scalar_results: Sequence[object | None],
        projection: tuple[int, ...] | None,
    ) -> None:
        self.scalar_results = list(scalar_results)
        self.projection = projection
        self.scalar_statements: list[object] = []
        self.execute_statements: list[object] = []

    async def scalar(self, statement: object) -> object | None:
        self.scalar_statements.append(statement)
        return self.scalar_results.pop(0)

    async def execute(self, statement: object) -> _ProjectionResult:
        self.execute_statements.append(statement)
        return _ProjectionResult(self.projection)


def _draft(
    *,
    revision: int = 7,
    suspended: bool = False,
    payload: dict[str, object] | None = None,
    presentation_ref: str | None = None,
) -> Draft:
    return Draft(
        id=DRAFT_ID,
        user_id=OWNER_ID,
        state="quick_confirm",
        payload=payload or {},
        revision=revision,
        suspended=suspended,
        presentation_ref=presentation_ref,
    )


@pytest.mark.asyncio
async def test_projection_is_authoritative_and_owner_and_draft_are_locked() -> None:
    session = _Session(
        [OWNER_ID, _draft(payload={"ui_message_id": 94_000_004})], (93_000_003, 94_000_004)
    )

    is_current = await SqlAlchemyDraftPresentationGuard()(
        cast(AsyncSession, session),
        OWNER_ID,
        DraftRef(DRAFT_ID, 7),
        93_000_003,
        94_000_004,
    )

    assert is_current is True
    assert len(session.scalar_statements) == 2
    assert all("FOR UPDATE" in str(statement) for statement in session.scalar_statements)
    assert len(session.execute_statements) == 1


@pytest.mark.asyncio
async def test_exact_projection_returns_both_bounded_context_pages() -> None:
    session = _Session(
        [OWNER_ID, _draft()],
        (93_000_003, 94_000_004, 12, 19),
    )

    context = await lock_telegram_draft_presentation_context(
        cast(AsyncSession, session),
        OWNER_ID,
        DraftRef(DRAFT_ID, 7),
        93_000_003,
        94_000_004,
    )

    assert context == TelegramDraftPresentationContext(12, 19)


@pytest.mark.asyncio
async def test_legacy_projection_returns_empty_context_instead_of_guessing_pages() -> None:
    session = _Session(
        [OWNER_ID, _draft(payload={"ui_message_id": 94_000_004})],
        None,
    )

    context = await lock_telegram_draft_presentation_context(
        cast(AsyncSession, session),
        OWNER_ID,
        DraftRef(DRAFT_ID, 7),
        93_000_003,
        94_000_004,
    )

    assert context == TelegramDraftPresentationContext()


@pytest.mark.asyncio
async def test_prior_revision_context_can_be_locked_for_conflict_receipt() -> None:
    class _PriorContextSession:
        def __init__(self) -> None:
            self.statements: list[object] = []

        async def execute(self, statement: object) -> Any:
            self.statements.append(statement)
            return type(
                "PriorProjectionResult",
                (),
                {"one_or_none": lambda self: type("PriorProjectionRow", (), {"_t": (12, 19)})()},
            )()

    session = _PriorContextSession()

    context = await lock_telegram_draft_presentation_context_by_revision(
        cast(AsyncSession, session),
        DRAFT_ID,
        6,
    )

    assert context == TelegramDraftPresentationContext(12, 19)
    assert len(session.statements) == 1
    assert "FOR UPDATE" in str(session.statements[0])


@pytest.mark.asyncio
async def test_mismatched_projection_rejects_even_when_legacy_payload_matches() -> None:
    session = _Session(
        [OWNER_ID, _draft(payload={"ui_message_id": 94_000_004})],
        (93_000_003, 94_000_099),
    )

    is_current = await SqlAlchemyDraftPresentationGuard()(
        cast(AsyncSession, session),
        OWNER_ID,
        DraftRef(DRAFT_ID, 7),
        93_000_003,
        94_000_004,
    )

    assert is_current is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "presentation_ref"),
    [
        ({"ui_message_id": 94_000_004}, None),
        ({}, "94000004"),
    ],
)
async def test_legacy_message_binding_is_used_only_without_a_projection(
    payload: dict[str, object],
    presentation_ref: str | None,
) -> None:
    session = _Session(
        [OWNER_ID, _draft(payload=payload, presentation_ref=presentation_ref)],
        None,
    )

    is_current = await SqlAlchemyDraftPresentationGuard()(
        cast(AsyncSession, session),
        OWNER_ID,
        DraftRef(DRAFT_ID, 7),
        93_000_003,
        94_000_004,
    )

    assert is_current is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "draft",
    [
        None,
        _draft(revision=8),
        _draft(suspended=True),
        Draft(
            id=UUID("00000000-0000-7000-8000-000000000499"),
            user_id=OWNER_ID,
            state="quick_confirm",
            payload={},
            revision=7,
            suspended=False,
        ),
    ],
)
async def test_missing_stale_or_suspended_draft_rejects_before_projection_query(
    draft: Draft | None,
) -> None:
    session = _Session([OWNER_ID, draft], (93_000_003, 94_000_004))

    is_current = await SqlAlchemyDraftPresentationGuard()(
        cast(AsyncSession, session),
        OWNER_ID,
        DraftRef(DRAFT_ID, 7),
        93_000_003,
        94_000_004,
    )

    assert is_current is False
    assert session.execute_statements == []


@pytest.mark.asyncio
async def test_invalid_legacy_message_reference_fails_closed() -> None:
    session = _Session([OWNER_ID, _draft(presentation_ref="not-an-integer")], None)

    is_current = await SqlAlchemyDraftPresentationGuard()(
        cast(AsyncSession, session),
        OWNER_ID,
        DraftRef(DRAFT_ID, 7),
        93_000_003,
        94_000_004,
    )

    assert is_current is False


@pytest.mark.asyncio
async def test_conflict_guard_accepts_exact_projection_for_a_suspended_draft() -> None:
    session = _Session(
        [OWNER_ID, _draft(suspended=True)],
        (93_000_003, 94_000_004),
    )

    is_current = await SqlAlchemyDraftConflictPresentationGuard()(
        cast(AsyncSession, session),
        OWNER_ID,
        DraftRef(DRAFT_ID, 7),
        93_000_003,
        94_000_004,
    )

    assert is_current is True
    assert len(session.scalar_statements) == 2
    assert all("FOR UPDATE" in str(statement) for statement in session.scalar_statements)


@pytest.mark.asyncio
async def test_conflict_guard_rejects_a_stale_suspended_draft_before_projection_lookup() -> None:
    session = _Session(
        [OWNER_ID, _draft(revision=8, suspended=True)],
        (93_000_003, 94_000_004),
    )

    is_current = await SqlAlchemyDraftConflictPresentationGuard()(
        cast(AsyncSession, session),
        OWNER_ID,
        DraftRef(DRAFT_ID, 7),
        93_000_003,
        94_000_004,
    )

    assert is_current is False
    assert session.execute_statements == []
