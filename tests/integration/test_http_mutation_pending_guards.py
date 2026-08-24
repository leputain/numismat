from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, update
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
    Transaction,
    User,
)
from finbot.adapters.database.repositories.ocr_queue import (
    SqlAlchemyOcrQueueCommandRepository,
)
from finbot.adapters.database.repositories.settings_text_input import (
    SqlAlchemySettingsTextInputRepository,
)
from finbot.adapters.database.repositories.transaction_draft_selection import (
    SqlAlchemyTransactionDraftSelectionRepository,
)
from finbot.adapters.database.repositories.transactions import (
    SqlAlchemyTransactionCommandRepository,
)
from finbot.application.draft_conflicts import (
    PendingWizardIntent,
    encode_pending_draft_intent,
)
from finbot.application.draft_navigation import DraftCatalogRef
from finbot.application.dto import (
    ConfirmTransactionDraftCommand,
    DraftRef,
    OcrQueueMutationCommand,
)
from finbot.application.errors import InvalidStateError
from finbot.application.ocr_queue import OcrQueueState, encode_ocr_queue
from finbot.application.transaction_draft_selection import (
    TransactionDraftSelectionAction,
    TransactionDraftSelectionCommand,
)
from finbot.domain.transactions import TransactionType

DATABASE_URL = os.environ["TEST_DATABASE_URL"]
PENDING_INTENT = encode_pending_draft_intent(PendingWizardIntent())


@dataclass(frozen=True, slots=True)
class _OwnerFixture:
    owner_id: UUID = field(repr=False)
    account_id: UUID = field(repr=False)
    target_account_id: UUID = field(repr=False)
    category_id: UUID = field(repr=False)
    target_category_id: UUID = field(repr=False)
    transaction_id: UUID = field(repr=False)


def _synthetic_telegram_user_id() -> int:
    return 9_100_000_000 + uuid4().int % 800_000_000


async def _setup_owner(
    factory: async_sessionmaker[AsyncSession],
) -> _OwnerFixture:
    async with factory.begin() as session:
        telegram_id = _synthetic_telegram_user_id()
        owner = User(
            telegram_user_id=telegram_id,
            telegram_chat_id=telegram_id,
            locale="ru_RU",
            timezone="Europe/Moscow",
            base_currency="RUB",
        )
        session.add(owner)
        await session.flush()
        account = Account(
            user_id=owner.id,
            name="Основной",
            slug="основной",
            currency="RUB",
        )
        target_account = Account(
            user_id=owner.id,
            name="Резервный",
            slug="резервный",
            currency="RUB",
        )
        category = Category(
            user_id=owner.id,
            kind=TransactionType.EXPENSE.value,
            name="Базовая",
            slug="базовая",
            emoji="▫️",
        )
        target_category = Category(
            user_id=owner.id,
            kind=TransactionType.EXPENSE.value,
            name="Целевая",
            slug="целевая",
            emoji="▫️",
        )
        session.add_all((account, target_account, category, target_category))
        await session.flush()
        owner.default_account_id = account.id
        transaction = Transaction(
            user_id=owner.id,
            type=TransactionType.EXPENSE.value,
            amount_minor=1_000,
            currency="RUB",
            account_id=account.id,
            category_id=category.id,
            occurred_at=datetime(2026, 8, 14, 12, tzinfo=UTC),
            description="",
        )
        session.add(transaction)
        await session.flush()
        return _OwnerFixture(
            owner_id=owner.id,
            account_id=account.id,
            target_account_id=target_account.id,
            category_id=category.id,
            target_category_id=target_category.id,
            transaction_id=transaction.id,
        )


async def _create_draft(
    factory: async_sessionmaker[AsyncSession],
    fixture: _OwnerFixture,
    *,
    state: str,
    payload: dict[str, object],
) -> DraftRef:
    async with factory.begin() as session:
        draft = Draft(
            user_id=fixture.owner_id,
            state=state,
            payload={**payload, "pending_intent": PENDING_INTENT},
        )
        session.add(draft)
        await session.flush()
        return DraftRef(draft.id, draft.revision)


async def _cleanup(engine: AsyncEngine, fixture: _OwnerFixture) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(User).where(User.id == fixture.owner_id).values(default_account_id=None)
        )
        await connection.execute(delete(AuditEvent).where(AuditEvent.user_id == fixture.owner_id))
        await connection.execute(delete(Draft).where(Draft.user_id == fixture.owner_id))
        await connection.execute(delete(Transaction).where(Transaction.user_id == fixture.owner_id))
        await connection.execute(delete(Category).where(Category.user_id == fixture.owner_id))
        await connection.execute(delete(Account).where(Account.user_id == fixture.owner_id))
        await connection.execute(delete(User).where(User.id == fixture.owner_id))


@pytest.mark.asyncio
async def test_settings_text_target_rejects_pending_intent_before_catalog_access() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup_owner(factory)
    draft_ref = await _create_draft(
        factory,
        fixture,
        state="settings_account_rename",
        payload={"account_id": str(fixture.account_id), "object_version": 1},
    )

    try:
        async with factory() as session:
            with pytest.raises(InvalidStateError):
                await SqlAlchemySettingsTextInputRepository(session).lock_target(
                    fixture.owner_id,
                    draft_ref,
                )
            await session.rollback()

        async with factory() as verification:
            account = await verification.get(Account, fixture.account_id)
            draft = await verification.get(Draft, draft_ref.draft_id)
            assert account is not None
            assert (account.name, account.version) == ("Основной", 1)
            assert draft is not None and draft.revision == draft_ref.revision
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.asyncio
async def test_transaction_selection_rejects_pending_intent_before_update() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup_owner(factory)
    draft_ref = await _create_draft(
        factory,
        fixture,
        state="edit_category",
        payload={"transaction_id": str(fixture.transaction_id), "version": 1},
    )
    command = TransactionDraftSelectionCommand(
        fixture.owner_id,
        draft_ref,
        TransactionDraftSelectionAction.CATEGORY,
        DraftCatalogRef(fixture.target_category_id, 1),
    )

    try:
        async with factory() as session:
            with pytest.raises(InvalidStateError):
                await SqlAlchemyTransactionDraftSelectionRepository(session).apply(command)
            await session.rollback()

        async with factory() as verification:
            transaction = await verification.get(Transaction, fixture.transaction_id)
            draft = await verification.get(Draft, draft_ref.draft_id)
            assert transaction is not None
            assert (transaction.category_id, transaction.version) == (fixture.category_id, 1)
            assert draft is not None and draft.revision == draft_ref.revision
            assert (
                await verification.scalar(
                    select(func.count(AuditEvent.id)).where(AuditEvent.user_id == fixture.owner_id)
                )
                == 0
            )
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.asyncio
async def test_plain_confirm_rejects_pending_intent_before_transaction_creation() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup_owner(factory)
    draft_ref = await _create_draft(
        factory,
        fixture,
        state="quick_confirm",
        payload={
            "type": "expense",
            "amount_minor": 500,
            "account_id": str(fixture.account_id),
            "category_id": str(fixture.category_id),
            "occurred_at": "2026-08-14T13:00:00+00:00",
            "description": "",
        },
    )

    try:
        async with factory() as session:
            with pytest.raises(InvalidStateError):
                await SqlAlchemyTransactionCommandRepository(session).confirm_reviewed_draft(
                    ConfirmTransactionDraftCommand(fixture.owner_id, draft_ref)
                )
            await session.rollback()

        async with factory() as verification:
            assert (
                await verification.scalar(
                    select(func.count(Transaction.id)).where(
                        Transaction.user_id == fixture.owner_id
                    )
                )
                == 1
            )
            assert await verification.get(Draft, draft_ref.draft_id) is not None
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.parametrize("operation", ["confirm", "skip", "cancel"])
@pytest.mark.asyncio
async def test_ocr_queue_mutations_reject_pending_intent_authoritatively(
    operation: str,
) -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup_owner(factory)
    draft_ref = await _create_draft(
        factory,
        fixture,
        state="quick_confirm",
        payload={
            "flow": "ocr",
            "type": "expense",
            "amount_minor": 500,
            "account_id": str(fixture.account_id),
            "category_id": str(fixture.category_id),
            "occurred_at": "2026-08-14T13:00:00+00:00",
            "description": "",
            "ocr_batch": encode_ocr_queue(OcrQueueState.initial(())),
        },
    )
    command = OcrQueueMutationCommand(fixture.owner_id, draft_ref)

    try:
        async with factory() as session:
            repository = SqlAlchemyOcrQueueCommandRepository(session)
            with pytest.raises(InvalidStateError):
                if operation == "confirm":
                    await repository.confirm_and_continue(command)
                elif operation == "skip":
                    await repository.skip_and_continue(command)
                else:
                    await repository.cancel(command)
            await session.rollback()

        async with factory() as verification:
            assert (
                await verification.scalar(
                    select(func.count(Transaction.id)).where(
                        Transaction.user_id == fixture.owner_id
                    )
                )
                == 1
            )
            draft = await verification.get(Draft, draft_ref.draft_id)
            assert draft is not None and draft.revision == draft_ref.revision
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()
