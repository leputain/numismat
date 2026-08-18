import asyncio
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from finbot.adapters.database.models import Account, Category, Draft, Transaction, User
from finbot.adapters.database.repositories.drafts import SqlAlchemyDraftRepository
from finbot.adapters.database.repositories.queries import SqlAlchemyQueryRepository
from finbot.adapters.database.repositories.transaction_edit_ingress import (
    SqlAlchemyTransactionEditTargetReader,
)
from finbot.application.draft_conflicts import PendingEditIntent, decode_pending_draft_intent
from finbot.application.errors import ActiveDraftConflictError, ObjectVersionConflictError
from finbot.application.transaction_edit_ingress import (
    BeginTransactionEditCommand,
    TransactionEditIngressStatus,
)
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.application.use_cases.queries import GetOwnerSettings
from finbot.application.use_cases.transaction_edit_ingress import BeginTransactionEdit
from finbot.domain.transactions import TransactionType

DATABASE_URL = os.environ["TEST_DATABASE_URL"]


@dataclass(frozen=True, slots=True)
class _Fixture:
    owner_id: UUID = field(repr=False)
    transaction_id: UUID = field(repr=False)


def _synthetic_telegram_user_id() -> int:
    return 7_900_000_000 + uuid4().int % 1_000_000_000


def _begin(session: AsyncSession) -> BeginTransactionEdit:
    queries = SqlAlchemyQueryRepository(session)
    return BeginTransactionEdit(
        DraftUseCases(SqlAlchemyDraftRepository(session)),
        GetOwnerSettings(queries),
        SqlAlchemyTransactionEditTargetReader(session),
    )


async def _setup(factory: async_sessionmaker[AsyncSession]) -> _Fixture:
    async with factory() as session:
        owner = User(telegram_user_id=_synthetic_telegram_user_id())
        session.add(owner)
        await session.flush()
        account = Account(
            user_id=owner.id,
            name="Основной",
            slug="основной",
            currency="RUB",
        )
        category = Category(
            user_id=owner.id,
            kind=TransactionType.EXPENSE.value,
            name="Базовая",
            slug="базовая",
            emoji="▫️",
        )
        session.add_all((account, category))
        await session.flush()
        owner.default_account_id = account.id
        transaction = Transaction(
            user_id=owner.id,
            type=TransactionType.EXPENSE.value,
            amount_minor=12_345,
            currency="RUB",
            account_id=account.id,
            category_id=category.id,
            occurred_at=datetime(2026, 8, 13, 12, tzinfo=UTC),
            description="",
        )
        session.add(transaction)
        await session.flush()
        fixture = _Fixture(owner.id, transaction.id)
        await session.commit()
        return fixture


async def _cleanup(engine: AsyncEngine, fixture: _Fixture) -> None:
    async with engine.begin() as connection:
        await connection.execute(delete(Draft).where(Draft.user_id == fixture.owner_id))
        await connection.execute(delete(Transaction).where(Transaction.user_id == fixture.owner_id))
        await connection.execute(delete(Category).where(Category.user_id == fixture.owner_id))
        await connection.execute(delete(Account).where(Account.user_id == fixture.owner_id))
        await connection.execute(delete(User).where(User.id == fixture.owner_id))


@pytest.mark.asyncio
async def test_other_draft_winner_is_preserved_and_receives_typed_pending_edit() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup(factory)
    other_created = asyncio.Event()
    edit_started = asyncio.Event()

    try:

        async def create_other_winner() -> None:
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                created = await SqlAlchemyDraftRepository(session).create_if_absent(
                    fixture.owner_id,
                    "wizard_type",
                    {"flow": "wizard", "history_page": 7},
                )
                assert created.revision == 1
                other_created.set()
                await asyncio.wait_for(edit_started.wait(), timeout=2)
                await session.commit()

        async def stage_edit_loser() -> None:
            await asyncio.wait_for(other_created.wait(), timeout=2)
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                edit_started.set()
                result = await _begin(session).execute(
                    BeginTransactionEditCommand(
                        fixture.owner_id,
                        fixture.transaction_id,
                        1,
                    )
                )
                assert result.status is TransactionEditIngressStatus.CONFLICT_STAGED
                assert result.draft.state == "wizard_type"
                assert result.draft.revision == 2
                await session.commit()

        await asyncio.wait_for(
            asyncio.gather(create_other_winner(), stage_edit_loser()),
            timeout=8,
        )

        async with factory() as verification:
            draft = await verification.scalar(
                select(Draft).where(Draft.user_id == fixture.owner_id)
            )
            assert draft is not None
            assert draft.state == "wizard_type"
            assert draft.revision == 2
            assert draft.suspended is False
            assert draft.payload["flow"] == "wizard"
            assert "history_page" not in draft.payload
            assert decode_pending_draft_intent(draft.payload["pending_intent"]) == (
                PendingEditIntent(fixture.transaction_id, 1)
            )
            assert "history_page" not in draft.payload["pending_intent"]
            assert "ui_message_id" not in draft.payload["pending_intent"]
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.asyncio
async def test_edit_winner_creates_one_canonical_draft_and_other_creator_gets_conflict() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup(factory)
    edit_created = asyncio.Event()
    contender_started = asyncio.Event()

    try:

        async def create_edit_winner() -> None:
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                result = await _begin(session).execute(
                    BeginTransactionEditCommand(
                        fixture.owner_id,
                        fixture.transaction_id,
                        1,
                    )
                )
                assert result.status is TransactionEditIngressStatus.DRAFT_CREATED
                edit_created.set()
                await asyncio.wait_for(contender_started.wait(), timeout=2)
                await session.commit()

        async def create_other_loser() -> None:
            await asyncio.wait_for(edit_created.wait(), timeout=2)
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                contender_started.set()
                with pytest.raises(ActiveDraftConflictError) as conflict:
                    await SqlAlchemyDraftRepository(session).create_if_absent(
                        fixture.owner_id,
                        "wizard_type",
                        {"flow": "wizard"},
                    )
                assert conflict.value.current_revision == 1
                await session.rollback()

        await asyncio.wait_for(
            asyncio.gather(create_edit_winner(), create_other_loser()),
            timeout=8,
        )

        async with factory() as verification:
            drafts = (
                await verification.scalars(select(Draft).where(Draft.user_id == fixture.owner_id))
            ).all()
            assert len(drafts) == 1
            assert drafts[0].state == "edit_menu"
            assert drafts[0].revision == 1
            assert drafts[0].payload == {
                "transaction_id": str(fixture.transaction_id),
                "version": 1,
            }
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.asyncio
async def test_target_reader_rejects_stale_version_without_creating_a_draft() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup(factory)

    try:
        async with factory() as session:
            with pytest.raises(ObjectVersionConflictError) as conflict:
                await _begin(session).execute(
                    BeginTransactionEditCommand(
                        fixture.owner_id,
                        fixture.transaction_id,
                        2,
                    )
                )
            assert conflict.value.current_version == 1
            await session.rollback()

        async with factory() as verification:
            draft_count = len(
                (
                    await verification.scalars(
                        select(Draft).where(Draft.user_id == fixture.owner_id)
                    )
                ).all()
            )
            assert draft_count == 0
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()
