import asyncio
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text, update
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
from finbot.adapters.database.repositories.catalogs import SqlAlchemyCatalogRepository
from finbot.adapters.database.repositories.drafts import SqlAlchemyDraftRepository
from finbot.adapters.database.repositories.queries import SqlAlchemyQueryRepository
from finbot.adapters.database.repositories.transaction_draft_selection import (
    SqlAlchemyTransactionDraftSelectionRepository,
)
from finbot.application.catalogs import (
    ArchiveAccountCommand,
    UpdateCategoryCommand,
)
from finbot.application.draft_navigation import DraftCatalogRef
from finbot.application.dto import DraftRef
from finbot.application.errors import (
    CatalogUnavailableError,
    DraftRevisionConflictError,
    ObjectVersionConflictError,
)
from finbot.application.transaction_draft_selection import (
    TransactionDraftSelectionAction,
    TransactionDraftSelectionCommand,
)
from finbot.application.use_cases.catalogs import CatalogUseCases
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.application.use_cases.queries import GetOwnerSettings
from finbot.application.use_cases.transaction_draft_selection import (
    TransactionDraftSelectionUseCases,
)
from finbot.domain.transactions import TransactionType

DATABASE_URL = os.environ["TEST_DATABASE_URL"]


@dataclass(frozen=True, slots=True)
class _Fixture:
    owner_id: UUID = field(repr=False)
    transaction_id: UUID = field(repr=False)
    primary_account_id: UUID = field(repr=False)
    target_account_id: UUID = field(repr=False)
    base_category_id: UUID = field(repr=False)
    target_category_id: UUID = field(repr=False)
    wrong_kind_category_id: UUID = field(repr=False)
    draft: DraftRef = field(repr=False)


def _synthetic_telegram_user_id() -> int:
    return 7_800_000_000 + uuid4().int % 1_000_000_000


def _selection(session: AsyncSession) -> TransactionDraftSelectionUseCases:
    reader = SqlAlchemyQueryRepository(session)
    return TransactionDraftSelectionUseCases(
        DraftUseCases(SqlAlchemyDraftRepository(session)),
        GetOwnerSettings(reader),
        SqlAlchemyTransactionDraftSelectionRepository(session),
    )


async def _setup(
    factory: async_sessionmaker[AsyncSession],
    state: str,
) -> _Fixture:
    async with factory() as session:
        owner = User(telegram_user_id=_synthetic_telegram_user_id())
        session.add(owner)
        await session.flush()
        primary = Account(
            user_id=owner.id,
            name="Основной",
            slug="основной",
            currency="RUB",
        )
        target = Account(
            user_id=owner.id,
            name="Резервный",
            slug="резервный",
            currency="RUB",
        )
        base_category = Category(
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
        wrong_kind = Category(
            user_id=owner.id,
            kind=TransactionType.INCOME.value,
            name="Доходная",
            slug="доходная",
            emoji="▫️",
        )
        session.add_all((primary, target, base_category, target_category, wrong_kind))
        await session.flush()
        owner.default_account_id = primary.id
        transaction = Transaction(
            user_id=owner.id,
            type=TransactionType.EXPENSE.value,
            amount_minor=12_345,
            currency="RUB",
            account_id=primary.id,
            category_id=base_category.id,
            occurred_at=datetime(2026, 8, 13, 12, tzinfo=UTC),
            description="",
        )
        session.add(transaction)
        await session.flush()
        draft = await SqlAlchemyDraftRepository(session).create_if_absent(
            owner.id,
            state,
            {
                "transaction_id": str(transaction.id),
                "version": transaction.version,
                "history_page": 2,
            },
        )
        await session.commit()
        return _Fixture(
            owner_id=owner.id,
            transaction_id=transaction.id,
            primary_account_id=primary.id,
            target_account_id=target.id,
            base_category_id=base_category.id,
            target_category_id=target_category.id,
            wrong_kind_category_id=wrong_kind.id,
            draft=draft.ref,
        )


async def _cleanup(engine: AsyncEngine, fixture: _Fixture) -> None:
    async with engine.begin() as connection:
        await connection.execute(delete(AuditEvent).where(AuditEvent.user_id == fixture.owner_id))
        await connection.execute(delete(Draft).where(Draft.user_id == fixture.owner_id))
        await connection.execute(delete(Transaction).where(Transaction.user_id == fixture.owner_id))
        await connection.execute(
            update(User).where(User.id == fixture.owner_id).values(default_account_id=None)
        )
        await connection.execute(delete(Category).where(Category.user_id == fixture.owner_id))
        await connection.execute(delete(Account).where(Account.user_id == fixture.owner_id))
        await connection.execute(delete(User).where(User.id == fixture.owner_id))


@pytest.mark.asyncio
async def test_category_selection_rejects_a_rename_that_won_owner_serialization() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup(factory, "edit_category")
    renamed = asyncio.Event()
    selector_started = asyncio.Event()

    try:

        async def rename_winner() -> None:
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                result = await CatalogUseCases(
                    SqlAlchemyCatalogRepository(session)
                ).update_category(
                    UpdateCategoryCommand(
                        fixture.owner_id,
                        fixture.target_category_id,
                        "Переименованная",
                        1,
                    )
                )
                assert result.version == 2
                renamed.set()
                await asyncio.wait_for(selector_started.wait(), timeout=2)
                await session.commit()

        async def select_loser() -> None:
            await asyncio.wait_for(renamed.wait(), timeout=2)
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                selector_started.set()
                with pytest.raises(ObjectVersionConflictError) as conflict:
                    await _selection(session).execute(
                        TransactionDraftSelectionCommand(
                            fixture.owner_id,
                            fixture.draft,
                            TransactionDraftSelectionAction.CATEGORY,
                            DraftCatalogRef(fixture.target_category_id, 1),
                        )
                    )
                assert conflict.value.current_version == 2
                await session.rollback()

        await asyncio.wait_for(
            asyncio.gather(rename_winner(), select_loser()),
            timeout=8,
        )

        async with factory() as verification:
            transaction = await verification.get(Transaction, fixture.transaction_id)
            draft = await verification.scalar(
                select(Draft).where(Draft.user_id == fixture.owner_id)
            )
            category = await verification.get(Category, fixture.target_category_id)
            audit_count = await verification.scalar(
                select(func.count(AuditEvent.id)).where(AuditEvent.user_id == fixture.owner_id)
            )
            assert transaction is not None and transaction.version == 1
            assert transaction.category_id == fixture.base_category_id
            assert draft is not None and draft.revision == fixture.draft.revision
            assert category is not None and category.version == 2
            assert audit_count == 0
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.asyncio
async def test_account_selection_rejects_an_archive_that_won_owner_serialization() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup(factory, "edit_account")
    archived = asyncio.Event()
    selector_started = asyncio.Event()

    try:

        async def archive_winner() -> None:
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                result = await CatalogUseCases(
                    SqlAlchemyCatalogRepository(session)
                ).archive_account(
                    ArchiveAccountCommand(
                        fixture.owner_id,
                        fixture.target_account_id,
                        1,
                    )
                )
                assert result.version == 2
                archived.set()
                await asyncio.wait_for(selector_started.wait(), timeout=2)
                await session.commit()

        async def select_loser() -> None:
            await asyncio.wait_for(archived.wait(), timeout=2)
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                selector_started.set()
                with pytest.raises(ObjectVersionConflictError) as conflict:
                    await _selection(session).execute(
                        TransactionDraftSelectionCommand(
                            fixture.owner_id,
                            fixture.draft,
                            TransactionDraftSelectionAction.ACCOUNT,
                            DraftCatalogRef(fixture.target_account_id, 1),
                        )
                    )
                assert conflict.value.current_version == 2
                await session.rollback()

        await asyncio.wait_for(
            asyncio.gather(archive_winner(), select_loser()),
            timeout=8,
        )

        async with factory() as verification:
            transaction = await verification.get(Transaction, fixture.transaction_id)
            draft = await verification.scalar(
                select(Draft).where(Draft.user_id == fixture.owner_id)
            )
            account = await verification.get(Account, fixture.target_account_id)
            audit_count = await verification.scalar(
                select(func.count(AuditEvent.id)).where(AuditEvent.user_id == fixture.owner_id)
            )
            assert transaction is not None and transaction.version == 1
            assert transaction.account_id == fixture.primary_account_id
            assert draft is not None and draft.revision == fixture.draft.revision
            assert account is not None and account.archived_at is not None
            assert audit_count == 0
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_double_click_updates_once_and_deletes_the_exact_draft() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup(factory, "edit_category")
    winner_updated = asyncio.Event()
    contender_preloaded = asyncio.Event()

    try:

        async def select_winner() -> None:
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                result = await _selection(session).execute(
                    TransactionDraftSelectionCommand(
                        fixture.owner_id,
                        fixture.draft,
                        TransactionDraftSelectionAction.CATEGORY,
                        DraftCatalogRef(fixture.target_category_id, 1),
                    )
                )
                assert result.transaction is not None and result.transaction.version == 2
                winner_updated.set()
                await asyncio.wait_for(contender_preloaded.wait(), timeout=2)
                await session.commit()

        async def select_contender() -> None:
            await asyncio.wait_for(winner_updated.wait(), timeout=2)
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                cached = await session.scalar(
                    select(Draft).where(Draft.user_id == fixture.owner_id)
                )
                assert cached is not None and cached.revision == fixture.draft.revision
                contender_preloaded.set()
                with pytest.raises(DraftRevisionConflictError) as conflict:
                    await _selection(session).execute(
                        TransactionDraftSelectionCommand(
                            fixture.owner_id,
                            fixture.draft,
                            TransactionDraftSelectionAction.CATEGORY,
                            DraftCatalogRef(fixture.target_category_id, 1),
                        )
                    )
                assert conflict.value.current_revision is None
                await session.rollback()

        await asyncio.wait_for(
            asyncio.gather(select_winner(), select_contender()),
            timeout=8,
        )

        async with factory() as verification:
            transaction = await verification.get(Transaction, fixture.transaction_id)
            draft = await verification.scalar(
                select(Draft).where(Draft.user_id == fixture.owner_id)
            )
            audit_count = await verification.scalar(
                select(func.count(AuditEvent.id)).where(AuditEvent.user_id == fixture.owner_id)
            )
            assert transaction is not None and transaction.version == 2
            assert transaction.category_id == fixture.target_category_id
            assert draft is None
            assert audit_count == 1
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.asyncio
async def test_category_selection_rejects_a_same_owner_category_of_another_kind() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup(factory, "edit_category")

    try:
        async with factory() as session:
            with pytest.raises(CatalogUnavailableError):
                await _selection(session).execute(
                    TransactionDraftSelectionCommand(
                        fixture.owner_id,
                        fixture.draft,
                        TransactionDraftSelectionAction.CATEGORY,
                        DraftCatalogRef(fixture.wrong_kind_category_id, 1),
                    )
                )
            await session.rollback()

        async with factory() as verification:
            transaction = await verification.get(Transaction, fixture.transaction_id)
            draft = await verification.scalar(
                select(Draft).where(Draft.user_id == fixture.owner_id)
            )
            assert transaction is not None and transaction.version == 1
            assert draft is not None
            assert draft.id == fixture.draft.draft_id
            assert draft.revision == fixture.draft.revision
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()
