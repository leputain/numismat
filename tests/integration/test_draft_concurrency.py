import asyncio
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from finbot.adapters.database.models import Draft, User
from finbot.adapters.database.repositories.drafts import SqlAlchemyDraftRepository
from finbot.adapters.database.services.transactions import put_draft, start_draft
from finbot.application.draft_conflicts import (
    ResolveDraftConflictCommand,
)
from finbot.application.draft_preparation import PreparedDraftResult, PrepareQuickDraftCommand
from finbot.application.dto import (
    DraftConflictResolution,
    PreparedTransactionDraft,
    PrepareRepeatDraftCommand,
)
from finbot.application.errors import ActiveDraftConflictError, DraftRevisionConflictError
from finbot.application.use_cases.draft_conflicts import (
    PrepareDraftConflictReplacement,
    ResolveDraftConflict,
)
from finbot.domain.errors import StaleObjectError


def _safe_database_url() -> str:
    raw = os.getenv("TEST_DATABASE_URL", "")
    if not raw:
        raise pytest.UsageError("TEST_DATABASE_URL is required for integration tests")
    try:
        database = make_url(raw).database or ""
    except Exception as exc:
        raise pytest.UsageError("TEST_DATABASE_URL is invalid") from exc
    if not database.endswith("_test"):
        raise pytest.UsageError("TEST_DATABASE_URL database name must end with _test")
    return raw


DATABASE_URL = _safe_database_url()


def _synthetic_telegram_user_id() -> int:
    return 8_000_000_000 + uuid4().int % 1_000_000_000


class _UnusedQuickDrafts:
    async def execute(self, command: PrepareQuickDraftCommand) -> PreparedDraftResult:
        raise AssertionError("Quick preparation was not expected")


class _UnusedReplacementTargets:
    async def prepare_repeat(
        self,
        command: PrepareRepeatDraftCommand,
    ) -> PreparedTransactionDraft:
        raise AssertionError("Repeat preparation was not expected")

    async def validate_edit(
        self,
        owner_id: UUID,
        transaction_id: UUID,
        expected_version: int,
    ) -> None:
        raise AssertionError("Edit validation was not expected")


def _wizard_replacements() -> PrepareDraftConflictReplacement:
    return PrepareDraftConflictReplacement(
        _UnusedQuickDrafts(),
        _UnusedReplacementTargets(),
    )


async def _create_owner(factory: async_sessionmaker[AsyncSession]) -> UUID:
    async with factory() as session:
        owner = User(telegram_user_id=_synthetic_telegram_user_id())
        session.add(owner)
        await session.flush()
        owner_id = owner.id
        await session.commit()
        return owner_id


async def _remove_owner(engine: AsyncEngine, owner_id: UUID) -> None:
    async with engine.begin() as connection:
        await connection.execute(delete(Draft).where(Draft.user_id == owner_id))
        await connection.execute(delete(User).where(User.id == owner_id))


@pytest.mark.asyncio
async def test_concurrent_create_returns_typed_active_draft_conflict() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    owner_id = await _create_owner(factory)

    try:
        winner_created = asyncio.Event()
        contender_started = asyncio.Event()

        async def create_winner() -> None:
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                await SqlAlchemyDraftRepository(session).create_if_absent(
                    owner_id, "amount", {"kind": "expense"}
                )
                winner_created.set()
                await asyncio.wait_for(contender_started.wait(), timeout=2)
                await asyncio.sleep(0)
                await session.commit()

        async def create_contender() -> None:
            await asyncio.wait_for(winner_created.wait(), timeout=2)
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                contender_started.set()
                with pytest.raises(ActiveDraftConflictError) as conflict:
                    await SqlAlchemyDraftRepository(session).create_if_absent(
                        owner_id, "review", {"kind": "income"}
                    )
                assert conflict.value.current_revision == 1
                await session.rollback()

        await asyncio.wait_for(
            asyncio.gather(create_winner(), create_contender()),
            timeout=8,
        )

        async with factory() as verification:
            count = await verification.scalar(
                select(func.count(Draft.id)).where(Draft.user_id == owner_id)
            )
            assert count == 1
    finally:
        await _remove_owner(engine, owner_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_create_serializes_repository_create_as_typed_conflict() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    owner_id = await _create_owner(factory)

    try:
        legacy_created = asyncio.Event()
        repository_started = asyncio.Event()

        async def create_with_legacy_service() -> None:
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                created = await put_draft(session, owner_id, "amount", {"step": 1})
                assert created.revision == 1
                legacy_created.set()
                await asyncio.wait_for(repository_started.wait(), timeout=2)
                await asyncio.sleep(0)
                await session.commit()

        async def create_with_repository() -> None:
            await asyncio.wait_for(legacy_created.wait(), timeout=2)
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                repository_started.set()
                with pytest.raises(ActiveDraftConflictError) as conflict:
                    await SqlAlchemyDraftRepository(session).create_if_absent(
                        owner_id, "review", {"step": 2}
                    )
                assert conflict.value.current_revision == 1
                await session.rollback()

        await asyncio.wait_for(
            asyncio.gather(create_with_legacy_service(), create_with_repository()),
            timeout=8,
        )

        async with factory() as verification:
            rows = (
                await verification.scalars(select(Draft).where(Draft.user_id == owner_id))
            ).all()
            assert len(rows) == 1
            assert rows[0].state == "amount"
            assert rows[0].payload == {"step": 1}
            assert rows[0].revision == 1
    finally:
        await _remove_owner(engine, owner_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_locked_mutation_refreshes_a_preloaded_draft_before_cas() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    owner_id = await _create_owner(factory)

    try:
        async with factory() as setup:
            initial = await SqlAlchemyDraftRepository(setup).create_if_absent(
                owner_id, "amount", {"step": 1}
            )
            await setup.commit()

        async with factory() as session_a, factory() as session_b:
            cached = await session_a.scalar(select(Draft).where(Draft.user_id == owner_id))
            assert cached is not None
            assert cached.revision == initial.revision

            changed = await SqlAlchemyDraftRepository(session_b).update(
                owner_id,
                initial.ref,
                "review",
                {"step": 2},
            )
            await session_b.commit()
            assert changed.revision == 2
            assert cached.revision == 1

            with pytest.raises(DraftRevisionConflictError) as conflict:
                await SqlAlchemyDraftRepository(session_a).update(
                    owner_id,
                    initial.ref,
                    "review",
                    {"step": 3},
                )
            assert conflict.value.current_revision == 2
            assert cached.revision == 2
            await session_a.rollback()
    finally:
        await _remove_owner(engine, owner_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_update_refreshes_a_preloaded_draft_before_cas() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    owner_id = await _create_owner(factory)

    try:
        async with factory() as setup:
            initial = await SqlAlchemyDraftRepository(setup).create_if_absent(
                owner_id, "amount", {"step": 1}
            )
            await setup.commit()

        async with factory() as legacy_session, factory() as repository_session:
            cached = await legacy_session.scalar(select(Draft).where(Draft.user_id == owner_id))
            assert cached is not None
            assert cached.revision == initial.revision

            changed = await SqlAlchemyDraftRepository(repository_session).update(
                owner_id,
                initial.ref,
                "review",
                {"step": 2},
            )
            await repository_session.commit()
            assert changed.revision == 2
            assert cached.revision == 1

            with pytest.raises(StaleObjectError, match="Черновик"):
                await put_draft(
                    legacy_session,
                    owner_id,
                    "review",
                    {"step": 3},
                    expected_revision=initial.revision,
                    expected_draft_id=initial.draft_id,
                )
            assert cached.revision == 2
            assert cached.payload == {"step": 2}
            await legacy_session.rollback()
    finally:
        await _remove_owner(engine, owner_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_repository_hides_and_preserves_legacy_presentation_payload() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    owner_id = await _create_owner(factory)

    try:
        async with factory() as setup:
            row = Draft(
                user_id=owner_id,
                state="amount",
                payload={"step": 1, "ui_message_id": 777},
                presentation_ref="synthetic-ref",
            )
            setup.add(row)
            await setup.commit()

        async with factory() as session:
            repository = SqlAlchemyDraftRepository(session)
            active = await repository.get_active(owner_id)
            assert active is not None
            assert active.payload == {"step": 1}
            assert "ui_message_id" not in active.payload

            updated = await repository.update(owner_id, active.ref, "review", {"step": 2})
            assert updated.payload == {"step": 2}
            stored = await session.scalar(select(Draft).where(Draft.user_id == owner_id))
            assert stored is not None
            assert stored.payload == {"step": 2, "ui_message_id": 777}
            assert stored.presentation_ref == "synthetic-ref"
            await session.rollback()
    finally:
        await _remove_owner(engine, owner_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_replace_invalidates_the_losing_reference() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    owner_id = await _create_owner(factory)

    try:
        async with factory() as setup:
            initial = await SqlAlchemyDraftRepository(setup).create_if_absent(
                owner_id, "amount", {"step": 1}
            )
            await setup.commit()

        winner_replaced = asyncio.Event()
        contender_started = asyncio.Event()

        async def replace_winner() -> None:
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                replacement = await SqlAlchemyDraftRepository(session).replace(
                    owner_id,
                    initial.ref,
                    "review",
                    {"step": 2},
                )
                assert replacement.draft_id != initial.draft_id
                winner_replaced.set()
                await asyncio.wait_for(contender_started.wait(), timeout=2)
                await asyncio.sleep(0)
                await session.commit()

        async def replace_contender() -> None:
            await asyncio.wait_for(winner_replaced.wait(), timeout=2)
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                contender_started.set()
                with pytest.raises(DraftRevisionConflictError):
                    await SqlAlchemyDraftRepository(session).replace(
                        owner_id,
                        initial.ref,
                        "review",
                        {"step": 3},
                    )
                await session.rollback()

        await asyncio.wait_for(
            asyncio.gather(replace_winner(), replace_contender()),
            timeout=8,
        )

        async with factory() as verification:
            rows = (
                await verification.scalars(select(Draft).where(Draft.user_id == owner_id))
            ).all()
            assert len(rows) == 1
            assert rows[0].id != initial.draft_id
            assert rows[0].revision == 1
    finally:
        await _remove_owner(engine, owner_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_conflict_replace_locks_exact_draft_and_creates_one_new_revision_one() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    owner_id = await _create_owner(factory)

    try:
        async with factory() as setup:
            initial = await SqlAlchemyDraftRepository(setup).create_if_absent(
                owner_id,
                "review",
                {
                    "flow": "quick",
                    "pending_intent": {"kind": "wizard"},
                },
            )
            await setup.commit()

        winner_prepared = asyncio.Event()
        contender_preloaded = asyncio.Event()

        async def resolve_winner() -> None:
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                result = await ResolveDraftConflict(
                    SqlAlchemyDraftRepository(session),
                    _wizard_replacements(),
                ).execute(
                    ResolveDraftConflictCommand(
                        owner_id,
                        initial.ref,
                        DraftConflictResolution.REPLACE,
                    )
                )
                assert result.draft.draft_id != initial.draft_id
                assert result.draft.revision == 1
                winner_prepared.set()
                await asyncio.wait_for(contender_preloaded.wait(), timeout=2)
                await session.commit()

        async def resolve_contender() -> None:
            await asyncio.wait_for(winner_prepared.wait(), timeout=2)
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                cached = await session.scalar(select(Draft).where(Draft.user_id == owner_id))
                assert cached is not None and cached.id == initial.draft_id
                contender_preloaded.set()
                with pytest.raises(DraftRevisionConflictError) as conflict:
                    await ResolveDraftConflict(
                        SqlAlchemyDraftRepository(session),
                        _wizard_replacements(),
                    ).execute(
                        ResolveDraftConflictCommand(
                            owner_id,
                            initial.ref,
                            DraftConflictResolution.REPLACE,
                        )
                    )
                assert conflict.value.current_revision == 1
                await session.rollback()

        await asyncio.wait_for(
            asyncio.gather(resolve_winner(), resolve_contender()),
            timeout=8,
        )

        async with factory() as verification:
            replacement = await verification.scalar(select(Draft).where(Draft.user_id == owner_id))
            assert replacement is not None
            assert replacement.id != initial.draft_id
            assert replacement.revision == 1
            assert replacement.state == "wizard_type"
            assert replacement.payload == {"flow": "wizard"}
            assert replacement.suspended is False
    finally:
        await _remove_owner(engine, owner_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_start_serializes_repository_replace_as_typed_conflict() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    owner_id = await _create_owner(factory)

    try:
        async with factory() as setup:
            initial = await SqlAlchemyDraftRepository(setup).create_if_absent(
                owner_id, "amount", {"step": 1}
            )
            await setup.commit()

        legacy_started = asyncio.Event()
        repository_started = asyncio.Event()
        replacement_id: UUID | None = None

        async def replace_with_legacy_start() -> None:
            nonlocal replacement_id
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                replacement = await start_draft(
                    session,
                    owner_id,
                    "review",
                    {"step": 2},
                )
                replacement_id = replacement.id
                assert replacement.id != initial.draft_id
                assert replacement.revision == 1
                legacy_started.set()
                await asyncio.wait_for(repository_started.wait(), timeout=2)
                await asyncio.sleep(0)
                await session.commit()

        async def replace_with_repository() -> None:
            await asyncio.wait_for(legacy_started.wait(), timeout=2)
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                repository_started.set()
                with pytest.raises(DraftRevisionConflictError) as conflict:
                    await SqlAlchemyDraftRepository(session).replace(
                        owner_id,
                        initial.ref,
                        "review",
                        {"step": 3},
                    )
                assert conflict.value.current_revision == 1
                await session.rollback()

        await asyncio.wait_for(
            asyncio.gather(replace_with_legacy_start(), replace_with_repository()),
            timeout=8,
        )

        async with factory() as verification:
            rows = (
                await verification.scalars(select(Draft).where(Draft.user_id == owner_id))
            ).all()
            assert len(rows) == 1
            assert rows[0].id == replacement_id
            assert rows[0].id != initial.draft_id
            assert rows[0].payload == {"step": 2}
            assert rows[0].revision == 1
    finally:
        await _remove_owner(engine, owner_id)
        await engine.dispose()
