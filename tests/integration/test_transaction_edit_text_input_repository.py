import asyncio
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from finbot.adapters.database.models import (
    Account,
    AuditEvent,
    Category,
    Draft,
    TelegramDraftPresentation,
    Transaction,
    User,
)
from finbot.adapters.database.repositories.draft_presentations import (
    lock_telegram_draft_presentation_context,
)
from finbot.adapters.database.repositories.drafts import SqlAlchemyDraftRepository
from finbot.adapters.database.repositories.transaction_edit_text_input import (
    SqlAlchemyTransactionEditTextTargetRepository,
)
from finbot.adapters.database.repositories.transactions import (
    SqlAlchemyTransactionCommandRepository,
)
from finbot.application.dto import DraftRef
from finbot.application.errors import DraftRevisionConflictError
from finbot.application.transaction_edit_text_input import (
    TransactionEditTextInputCommand,
    TransactionEditTextInputError,
    TransactionEditTextInputStatus,
    TransactionEditTextTargetRepository,
)
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.application.use_cases.transaction_edit_text_input import (
    TransactionEditTextInputUseCase,
)
from finbot.application.use_cases.transactions import TransactionUseCases
from finbot.domain.transactions import TransactionType

DATABASE_URL = os.environ["TEST_DATABASE_URL"]


@dataclass(frozen=True, slots=True)
class _Fixture:
    owner_id: UUID = field(repr=False)
    draft_ref: DraftRef = field(repr=False)
    transaction_id: UUID = field(repr=False)
    chat_id: int = field(repr=False)
    message_id: int = field(repr=False)


def _synthetic_telegram_user_id() -> int:
    return 8_100_000_000 + uuid4().int % 800_000_000


def _use_case(
    session: AsyncSession,
    *,
    targets: TransactionEditTextTargetRepository | None = None,
) -> TransactionEditTextInputUseCase:
    drafts = DraftUseCases(SqlAlchemyDraftRepository(session))
    return TransactionEditTextInputUseCase(
        targets or SqlAlchemyTransactionEditTextTargetRepository(session),
        TransactionUseCases(
            SqlAlchemyTransactionCommandRepository(session),
            SqlAlchemyDraftRepository(session),
        ),
        drafts,
    )


async def _setup(factory: async_sessionmaker[AsyncSession]) -> _Fixture:
    async with factory() as session:
        chat_id = _synthetic_telegram_user_id()
        owner = User(
            telegram_user_id=chat_id,
            telegram_chat_id=chat_id,
            timezone="Europe/Moscow",
        )
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
            occurred_at=datetime(2026, 8, 13, 9, 30, tzinfo=UTC),
            description="до изменения",
        )
        session.add(transaction)
        await session.flush()
        draft = Draft(
            user_id=owner.id,
            state="edit_amount",
            payload={"transaction_id": str(transaction.id), "version": 1},
        )
        session.add(draft)
        await session.flush()
        message_id = 8_900_000_000 + uuid4().int % 90_000_000
        session.add(
            TelegramDraftPresentation(
                draft_id=draft.id,
                chat_id=chat_id,
                message_id=message_id,
                rendered_revision=draft.revision,
                history_page=6,
            )
        )
        fixture = _Fixture(
            owner.id,
            DraftRef(draft.id, draft.revision),
            transaction.id,
            chat_id,
            message_id,
        )
        await session.commit()
        return fixture


async def _cleanup(engine: AsyncEngine, fixture: _Fixture) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(User).where(User.id == fixture.owner_id).values(default_account_id=None)
        )
        await connection.execute(delete(Draft).where(Draft.user_id == fixture.owner_id))
        await connection.execute(delete(AuditEvent).where(AuditEvent.user_id == fixture.owner_id))
        await connection.execute(delete(Transaction).where(Transaction.user_id == fixture.owner_id))
        await connection.execute(delete(Category).where(Category.user_id == fixture.owner_id))
        await connection.execute(delete(Account).where(Account.user_id == fixture.owner_id))
        await connection.execute(delete(User).where(User.id == fixture.owner_id))


@pytest.mark.asyncio
async def test_adapter_reads_only_exact_durable_presentation_context() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup(factory)
    try:
        async with factory() as session:
            repository = SqlAlchemyTransactionEditTextTargetRepository(session)
            active = await repository.lock_active(fixture.owner_id)
            message_id = await repository.presentation_message_id(
                fixture.owner_id,
                active.ref,
                fixture.chat_id,
            )
            assert message_id == fixture.message_id
            context = await lock_telegram_draft_presentation_context(
                session,
                fixture.owner_id,
                active.ref,
                fixture.chat_id,
                fixture.message_id,
            )
            assert context is not None
            assert context.history_page == 6
            assert context.pending_history_page is None
            assert (
                await repository.presentation_message_id(
                    fixture.owner_id,
                    active.ref,
                    fixture.chat_id + 1,
                )
                is None
            )
            await session.rollback()
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.asyncio
async def test_success_updates_audit_and_draft_atomically() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup(factory)
    try:
        async with factory() as session:
            result = await _use_case(session).execute(
                TransactionEditTextInputCommand(
                    fixture.owner_id,
                    fixture.draft_ref,
                    "222,22",
                )
            )
            assert result.status is TransactionEditTextInputStatus.UPDATED
            assert result.transaction.amount_minor == 22_222
            assert result.transaction.version == 2
            await session.commit()

        async with factory() as verification:
            transaction = await verification.get(Transaction, fixture.transaction_id)
            assert transaction is not None
            assert transaction.amount_minor == 22_222
            assert transaction.version == 2
            assert (
                await verification.scalar(select(Draft).where(Draft.user_id == fixture.owner_id))
                is None
            )
            audits = (
                await verification.scalars(
                    select(AuditEvent).where(AuditEvent.user_id == fixture.owner_id)
                )
            ).all()
            assert len(audits) == 1
            assert audits[0].action == "update"
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.asyncio
async def test_invalid_input_advances_retry_revision_without_transaction_or_audit() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup(factory)
    try:
        async with factory() as session:
            result = await _use_case(session).execute(
                TransactionEditTextInputCommand(
                    fixture.owner_id,
                    fixture.draft_ref,
                    "не сумма",
                )
            )
            assert result.status is TransactionEditTextInputStatus.RETRY
            assert result.retry_error is TransactionEditTextInputError.INVALID_AMOUNT
            assert result.draft is not None
            assert result.draft.ref == DraftRef(
                fixture.draft_ref.draft_id,
                fixture.draft_ref.revision + 1,
            )
            await session.commit()

        async with factory() as verification:
            transaction = await verification.get(Transaction, fixture.transaction_id)
            assert transaction is not None
            assert transaction.amount_minor == 12_345
            assert transaction.version == 1
            draft = await verification.scalar(
                select(Draft).where(Draft.user_id == fixture.owner_id)
            )
            assert draft is not None
            assert draft.revision == fixture.draft_ref.revision + 1
            assert draft.state == "edit_amount"
            assert draft.payload == {
                "transaction_id": str(fixture.transaction_id),
                "version": 1,
            }
            assert (
                await verification.scalar(
                    select(AuditEvent).where(AuditEvent.user_id == fixture.owner_id)
                )
                is None
            )
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.asyncio
async def test_outer_rollback_restores_transaction_audit_and_exact_draft() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup(factory)
    try:
        async with factory() as session:
            result = await _use_case(session).execute(
                TransactionEditTextInputCommand(
                    fixture.owner_id,
                    fixture.draft_ref,
                    "222,22",
                )
            )
            assert result.transaction.version == 2
            await session.rollback()

        async with factory() as verification:
            transaction = await verification.get(Transaction, fixture.transaction_id)
            assert transaction is not None
            assert transaction.amount_minor == 12_345
            assert transaction.version == 1
            draft = await verification.scalar(
                select(Draft).where(Draft.user_id == fixture.owner_id)
            )
            assert draft is not None
            assert DraftRef(draft.id, draft.revision) == fixture.draft_ref
            assert (
                await verification.scalar(
                    select(AuditEvent).where(AuditEvent.user_id == fixture.owner_id)
                )
                is None
            )
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_exact_inputs_allow_only_one_edit_winner() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup(factory)
    contender_started = asyncio.Event()
    try:
        async with factory() as winner_session:
            await winner_session.execute(text("SET LOCAL lock_timeout = '3s'"))
            winner_targets = SqlAlchemyTransactionEditTextTargetRepository(winner_session)
            await winner_targets.lock_target(fixture.owner_id, fixture.draft_ref)

            async def run_contender() -> DraftRevisionConflictError:
                async with factory() as contender_session:
                    await contender_session.execute(text("SET LOCAL lock_timeout = '3s'"))
                    contender_started.set()
                    try:
                        await _use_case(contender_session).execute(
                            TransactionEditTextInputCommand(
                                fixture.owner_id,
                                fixture.draft_ref,
                                "333,33",
                            )
                        )
                    except DraftRevisionConflictError as error:
                        await contender_session.rollback()
                        return error
                    raise AssertionError("Concurrent stale input unexpectedly succeeded")

            contender = asyncio.create_task(run_contender())
            await asyncio.wait_for(contender_started.wait(), timeout=2)
            await asyncio.sleep(0.05)
            winner = await _use_case(
                winner_session,
                targets=cast(TransactionEditTextTargetRepository, winner_targets),
            ).execute(
                TransactionEditTextInputCommand(
                    fixture.owner_id,
                    fixture.draft_ref,
                    "222,22",
                )
            )
            assert winner.transaction.amount_minor == 22_222
            await winner_session.commit()
            conflict = await asyncio.wait_for(contender, timeout=5)
            assert conflict.current_revision is None

        async with factory() as verification:
            transaction = await verification.get(Transaction, fixture.transaction_id)
            assert transaction is not None
            assert transaction.amount_minor == 22_222
            assert transaction.version == 2
            audits = (
                await verification.scalars(
                    select(AuditEvent).where(AuditEvent.user_id == fixture.owner_id)
                )
            ).all()
            assert len(audits) == 1
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()
