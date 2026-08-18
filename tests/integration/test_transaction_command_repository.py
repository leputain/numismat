import asyncio
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.engine import make_url
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
    CategoryRule,
    Draft,
    ProcessedUpdate,
    TelegramResponseOutbox,
    Transaction,
    User,
)
from finbot.adapters.database.repositories.draft_conflicts import (
    SqlAlchemyDraftConflictReplacementTargets,
)
from finbot.adapters.database.repositories.drafts import SqlAlchemyDraftRepository
from finbot.adapters.database.repositories.transactions import (
    SqlAlchemyTransactionCommandRepository,
)
from finbot.adapters.database.services.catalogs import archive_account, archive_category
from finbot.adapters.telegram.executor import TelegramMutationExecutor, TelegramMutationRequest
from finbot.application.dto import (
    ConfirmTransactionDraftCommand,
    DraftRef,
    EditTransactionCommand,
    PrepareRepeatDraftCommand,
    TransactionMutationResult,
    VersionedTransactionCommand,
)
from finbot.application.errors import (
    CatalogUnavailableError,
    InvalidStateError,
    ObjectVersionConflictError,
)
from finbot.application.use_cases.transactions import TransactionUseCases


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


async def _cleanup_owner(engine: AsyncEngine, owner_id: UUID) -> None:
    async with engine.begin() as connection:
        await connection.execute(delete(AuditEvent).where(AuditEvent.user_id == owner_id))
        await connection.execute(delete(Transaction).where(Transaction.user_id == owner_id))
        await connection.execute(delete(Draft).where(Draft.user_id == owner_id))
        await connection.execute(delete(CategoryRule).where(CategoryRule.user_id == owner_id))
        await connection.execute(
            update(User).where(User.id == owner_id).values(default_account_id=None)
        )
        await connection.execute(delete(Category).where(Category.user_id == owner_id))
        await connection.execute(delete(Account).where(Account.user_id == owner_id))
        await connection.execute(delete(User).where(User.id == owner_id))


async def _setup_owner(
    factory: async_sessionmaker[AsyncSession],
    *,
    with_transaction: bool = False,
) -> tuple[UUID, UUID, UUID, UUID, UUID | None, DraftRef | None]:
    async with factory() as session:
        owner = User(telegram_user_id=_synthetic_telegram_user_id())
        session.add(owner)
        await session.flush()
        primary = Account(user_id=owner.id, name="Основной", slug="основной")
        target = Account(user_id=owner.id, name="Резервный", slug="резервный")
        base_category = Category(
            user_id=owner.id,
            kind="expense",
            name="Базовая",
            slug="базовая",
            emoji="▫️",
        )
        target_category = Category(
            user_id=owner.id,
            kind="expense",
            name="Целевая",
            slug="целевая",
            emoji="▫️",
        )
        session.add_all((primary, target, base_category, target_category))
        await session.flush()
        owner.default_account_id = primary.id

        transaction_id: UUID | None = None
        if with_transaction:
            transaction = Transaction(
                user_id=owner.id,
                type="expense",
                amount_minor=1000,
                currency="RUB",
                account_id=primary.id,
                category_id=base_category.id,
                occurred_at=datetime(2026, 8, 13, 12, tzinfo=UTC),
                description="",
            )
            session.add(transaction)
            await session.flush()
            transaction_id = transaction.id

        draft_ref: DraftRef | None = None
        if not with_transaction:
            draft = Draft(
                user_id=owner.id,
                state="quick_confirm",
                payload={
                    "type": "expense",
                    "amount_minor": 1000,
                    "account_id": str(target.id),
                    "category_id": str(target_category.id),
                    "occurred_at": "2026-08-13T12:00:00+00:00",
                    "description": "",
                },
            )
            session.add(draft)
            await session.flush()
            draft_ref = DraftRef(draft.id, draft.revision)
        await session.commit()
        return (
            owner.id,
            primary.id,
            target.id,
            target_category.id,
            transaction_id,
            draft_ref,
        )


@pytest.mark.asyncio
async def test_confirm_is_review_only_atomic_and_uses_external_transaction_boundary() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    (
        owner_id,
        _primary_id,
        _target_id,
        _category_id,
        _transaction_id,
        draft_ref,
    ) = await _setup_owner(factory)
    assert draft_ref is not None

    try:
        async with factory() as session:
            result = await SqlAlchemyTransactionCommandRepository(session).confirm_reviewed_draft(
                ConfirmTransactionDraftCommand(owner_id=owner_id, expected=draft_ref)
            )
            assert result.resulting_state == "confirmed"
            assert result.version == 1
            assert (
                await session.scalar(
                    select(func.count(Transaction.id)).where(Transaction.user_id == owner_id)
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count(AuditEvent.id)).where(AuditEvent.user_id == owner_id)
                )
                == 1
            )
            assert await session.scalar(select(Draft).where(Draft.user_id == owner_id)) is None
            await session.rollback()

        async with factory() as verification:
            assert (
                await verification.scalar(
                    select(func.count(Transaction.id)).where(Transaction.user_id == owner_id)
                )
                == 0
            )
            assert (
                await verification.scalar(
                    select(func.count(AuditEvent.id)).where(AuditEvent.user_id == owner_id)
                )
                == 0
            )
            restored = await verification.scalar(select(Draft).where(Draft.user_id == owner_id))
            assert restored is not None
            assert DraftRef(restored.id, restored.revision) == draft_ref

        async with factory() as committed:
            await SqlAlchemyTransactionCommandRepository(committed).confirm_reviewed_draft(
                ConfirmTransactionDraftCommand(owner_id=owner_id, expected=draft_ref)
            )
            await committed.commit()

        async with factory() as verification:
            assert (
                await verification.scalar(
                    select(func.count(Transaction.id)).where(Transaction.user_id == owner_id)
                )
                == 1
            )
            assert (
                await verification.scalar(
                    select(func.count(AuditEvent.id)).where(AuditEvent.user_id == owner_id)
                )
                == 1
            )
            assert await verification.scalar(select(Draft).where(Draft.user_id == owner_id)) is None
    finally:
        await _cleanup_owner(engine, owner_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_confirm_atomically_persists_a_revalidated_pending_rule() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    (
        owner_id,
        _primary_id,
        target_id,
        target_category_id,
        _transaction_id,
        draft_ref,
    ) = await _setup_owner(factory)
    assert draft_ref is not None

    try:
        async with factory() as setup:
            draft = await setup.scalar(select(Draft).where(Draft.user_id == owner_id))
            assert draft is not None
            payload = dict(draft.payload)
            payload.update(
                {
                    "flow": "quick",
                    "description": "кофе на вокзале",
                    "category_explicit": False,
                    "rule_offer_pattern": "кофе",
                    "pending_rule": {
                        "pattern": "кофе",
                        "scope": "account",
                        "account_id": str(target_id),
                        "category_id": str(target_category_id),
                    },
                }
            )
            draft.payload = payload
            await setup.commit()

        async with factory() as session:
            result = await SqlAlchemyTransactionCommandRepository(session).confirm_reviewed_draft(
                ConfirmTransactionDraftCommand(owner_id=owner_id, expected=draft_ref)
            )
            assert result.resulting_state == "confirmed"
            rule = await session.scalar(
                select(CategoryRule).where(CategoryRule.user_id == owner_id)
            )
            assert rule is not None
            assert rule.normalized_pattern == "кофе"
            assert rule.account_id == target_id
            assert rule.category_id == target_category_id
            assert (
                await session.scalar(
                    select(func.count(Transaction.id)).where(Transaction.user_id == owner_id)
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count(AuditEvent.id)).where(AuditEvent.user_id == owner_id)
                )
                == 1
            )
            assert await session.scalar(select(Draft).where(Draft.user_id == owner_id)) is None
            await session.rollback()

        async with factory() as verification:
            assert (
                await verification.scalar(
                    select(func.count(CategoryRule.id)).where(CategoryRule.user_id == owner_id)
                )
                == 0
            )
            assert (
                await verification.scalar(
                    select(func.count(Transaction.id)).where(Transaction.user_id == owner_id)
                )
                == 0
            )
            assert (
                await verification.scalar(
                    select(func.count(AuditEvent.id)).where(AuditEvent.user_id == owner_id)
                )
                == 0
            )
            restored = await verification.scalar(select(Draft).where(Draft.user_id == owner_id))
            assert restored is not None
            assert DraftRef(restored.id, restored.revision) == draft_ref
    finally:
        await _cleanup_owner(engine, owner_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_confirm_ignores_a_pending_rule_invalidated_by_final_description() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    (
        owner_id,
        _primary_id,
        target_id,
        target_category_id,
        _transaction_id,
        draft_ref,
    ) = await _setup_owner(factory)
    assert draft_ref is not None

    try:
        async with factory() as setup:
            draft = await setup.scalar(select(Draft).where(Draft.user_id == owner_id))
            assert draft is not None
            payload = dict(draft.payload)
            payload.update(
                {
                    "flow": "quick",
                    "description": "чай на вокзале",
                    "category_explicit": False,
                    "rule_offer_pattern": "кофе",
                    "pending_rule": {
                        "pattern": "кофе",
                        "scope": "account",
                        "account_id": str(target_id),
                        "category_id": str(target_category_id),
                    },
                }
            )
            draft.payload = payload
            await setup.commit()

        async with factory() as session:
            result = await SqlAlchemyTransactionCommandRepository(session).confirm_reviewed_draft(
                ConfirmTransactionDraftCommand(owner_id=owner_id, expected=draft_ref)
            )
            await session.commit()
            assert result.resulting_state == "confirmed"

        async with factory() as verification:
            assert (
                await verification.scalar(
                    select(func.count(CategoryRule.id)).where(CategoryRule.user_id == owner_id)
                )
                == 0
            )
            assert (
                await verification.scalar(
                    select(func.count(Transaction.id)).where(Transaction.user_id == owner_id)
                )
                == 1
            )
            assert await verification.scalar(select(Draft).where(Draft.user_id == owner_id)) is None
    finally:
        await _cleanup_owner(engine, owner_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_confirm_refuses_an_ocr_queue_draft() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    (
        owner_id,
        _primary_id,
        _target_id,
        _target_category_id,
        _transaction_id,
        draft_ref,
    ) = await _setup_owner(factory)
    assert draft_ref is not None

    try:
        async with factory() as setup:
            draft = await setup.scalar(select(Draft).where(Draft.user_id == owner_id))
            assert draft is not None
            payload = dict(draft.payload)
            payload["ocr_batch"] = {"version": 1}
            draft.payload = payload
            await setup.commit()

        async with factory() as ocr_session:
            repository = SqlAlchemyTransactionCommandRepository(ocr_session)
            with pytest.raises(InvalidStateError):
                await repository.confirm_reviewed_draft(
                    ConfirmTransactionDraftCommand(owner_id=owner_id, expected=draft_ref)
                )
            await ocr_session.rollback()

        async with factory() as verification:
            assert await verification.scalar(select(Draft).where(Draft.user_id == owner_id))
            assert (
                await verification.scalar(
                    select(func.count(Transaction.id)).where(Transaction.user_id == owner_id)
                )
                == 0
            )
    finally:
        await _cleanup_owner(engine, owner_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_executor_rolls_back_delete_when_receipt_build_fails() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    (
        owner_id,
        _primary_id,
        _target_id,
        _category_id,
        transaction_id,
        _draft_ref,
    ) = await _setup_owner(factory, with_transaction=True)
    assert transaction_id is not None
    update_id = 6_000_000_000 + uuid4().int % 1_000_000_000

    try:
        async with factory() as lookup:
            telegram_user_id = await lookup.scalar(
                select(User.telegram_user_id).where(User.id == owner_id)
            )
        assert telegram_user_id is not None

        async def mutate(
            session: AsyncSession,
            locked_owner_id: UUID,
        ) -> TransactionMutationResult:
            return await TransactionUseCases(
                SqlAlchemyTransactionCommandRepository(session),
                SqlAlchemyDraftRepository(session),
            ).delete(
                VersionedTransactionCommand(
                    owner_id=locked_owner_id,
                    transaction_id=transaction_id,
                    expected_version=1,
                )
            )

        def fail_receipt_build(_value: TransactionMutationResult) -> str:
            raise RuntimeError("synthetic receipt failure")

        async def forbidden_enqueue(
            _session: AsyncSession,
            _request: TelegramMutationRequest,
            _receipt: str,
        ) -> None:
            raise AssertionError("failed receipt was enqueued")

        with pytest.raises(RuntimeError, match="synthetic receipt failure"):
            await TelegramMutationExecutor(factory).execute(
                TelegramMutationRequest(
                    update_id=update_id,
                    owner_telegram_user_id=telegram_user_id,
                    chat_id=telegram_user_id,
                    locale="ru_RU",
                    timezone="Europe/Moscow",
                    currency="RUB",
                ),
                mutate=mutate,
                build_receipt=fail_receipt_build,
                enqueue_receipt=forbidden_enqueue,
            )

        async with factory() as verification:
            transaction = await verification.scalar(
                select(Transaction).where(Transaction.id == transaction_id)
            )
            assert transaction is not None
            assert transaction.deleted_at is None
            assert transaction.version == 1
            assert (
                await verification.scalar(
                    select(func.count(AuditEvent.id)).where(AuditEvent.user_id == owner_id)
                )
                == 0
            )
            assert await verification.get(ProcessedUpdate, update_id) is None
            assert (
                await verification.scalar(
                    select(func.count(TelegramResponseOutbox.id)).where(
                        TelegramResponseOutbox.update_id == update_id
                    )
                )
                == 0
            )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(ProcessedUpdate).where(ProcessedUpdate.update_id == update_id)
            )
        await _cleanup_owner(engine, owner_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_edit_delete_restore_and_repeat_map_versions_and_states() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    (
        owner_id,
        _primary_id,
        target_id,
        target_category_id,
        transaction_id,
        _draft_ref,
    ) = await _setup_owner(factory, with_transaction=True)
    assert transaction_id is not None

    try:
        async with factory() as session:
            repository = SqlAlchemyTransactionCommandRepository(session)
            repeated = await repository.prepare_repeat(
                PrepareRepeatDraftCommand(
                    owner_id=owner_id,
                    transaction_id=transaction_id,
                    expected_version=1,
                    occurred_at=datetime(2026, 8, 14, 12, tzinfo=UTC),
                )
            )
            assert repeated.source_version == 1
            assert repeated.transaction.occurred_at == datetime(2026, 8, 14, 12, tzinfo=UTC)

            edited = await repository.edit(
                EditTransactionCommand(
                    owner_id=owner_id,
                    transaction_id=transaction_id,
                    expected_version=1,
                    amount_minor=2500,
                    account_id=target_id,
                    category_id=target_category_id,
                )
            )
            assert edited.resulting_state == "updated"
            assert edited.version == 2
            assert edited.transaction.amount_minor == 2500

            with pytest.raises(ObjectVersionConflictError) as stale:
                await repository.edit(
                    EditTransactionCommand(
                        owner_id=owner_id,
                        transaction_id=transaction_id,
                        expected_version=1,
                        amount_minor=3000,
                    )
                )
            assert stale.value.current_version == 2

            deleted = await repository.delete(
                VersionedTransactionCommand(
                    owner_id=owner_id,
                    transaction_id=transaction_id,
                    expected_version=2,
                )
            )
            assert deleted.resulting_state == "deleted"
            assert deleted.version == 3
            assert deleted.transaction.deleted_at is not None

            with pytest.raises(InvalidStateError):
                await repository.delete(
                    VersionedTransactionCommand(
                        owner_id=owner_id,
                        transaction_id=transaction_id,
                        expected_version=3,
                    )
                )

            restored = await repository.restore(
                VersionedTransactionCommand(
                    owner_id=owner_id,
                    transaction_id=transaction_id,
                    expected_version=3,
                )
            )
            assert restored.resulting_state == "active"
            assert restored.version == 4
            assert restored.transaction.deleted_at is None

            with pytest.raises(InvalidStateError):
                await repository.restore(
                    VersionedTransactionCommand(
                        owner_id=owner_id,
                        transaction_id=transaction_id,
                        expected_version=4,
                    )
                )
            await session.commit()

        async with factory() as verification:
            transaction = await verification.scalar(
                select(Transaction).where(Transaction.id == transaction_id)
            )
            audit_count = await verification.scalar(
                select(func.count(AuditEvent.id)).where(AuditEvent.user_id == owner_id)
            )
            assert transaction is not None
            assert transaction.version == 4
            assert transaction.deleted_at is None
            assert audit_count == 3
            assert (
                await verification.scalar(
                    select(func.count(Draft.id)).where(Draft.user_id == owner_id)
                )
                == 0
            )
    finally:
        await _cleanup_owner(engine, owner_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_conflict_edit_target_reloads_version_and_rejects_deleted_rows() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    (
        owner_id,
        _primary_id,
        _target_id,
        _target_category_id,
        transaction_id,
        _draft_ref,
    ) = await _setup_owner(factory, with_transaction=True)
    assert transaction_id is not None

    try:
        async with factory() as current:
            targets = SqlAlchemyDraftConflictReplacementTargets(current)
            await targets.validate_edit(owner_id, transaction_id, 1)
            transaction = await current.get(Transaction, transaction_id)
            assert transaction is not None
            transaction.version = 2
            await current.flush()

            with pytest.raises(ObjectVersionConflictError) as stale:
                await targets.validate_edit(owner_id, transaction_id, 1)
            assert stale.value.current_version == 2
            await current.rollback()

        async with factory() as deleted:
            transaction = await deleted.get(Transaction, transaction_id)
            assert transaction is not None
            transaction.deleted_at = datetime.now(UTC)
            transaction.version = 2
            await deleted.flush()

            with pytest.raises(InvalidStateError, match="восстановить"):
                await SqlAlchemyDraftConflictReplacementTargets(deleted).validate_edit(
                    owner_id,
                    transaction_id,
                    2,
                )
            await deleted.rollback()
    finally:
        await _cleanup_owner(engine, owner_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_confirm_loses_cleanly_when_account_archive_wins_owner_lock() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    owner_id, primary_id, target_id, _category_id, _transaction_id, draft_ref = await _setup_owner(
        factory
    )
    assert draft_ref is not None
    archived = asyncio.Event()
    confirm_started = asyncio.Event()

    try:

        async def archive_winner() -> None:
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                await archive_account(
                    session,
                    owner_id,
                    target_id,
                    primary_id,
                    expected_version=1,
                )
                archived.set()
                await asyncio.wait_for(confirm_started.wait(), timeout=2)
                await asyncio.sleep(0)
                await session.commit()

        async def confirm_contender() -> None:
            await asyncio.wait_for(archived.wait(), timeout=2)
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                confirm_started.set()
                with pytest.raises(CatalogUnavailableError):
                    await SqlAlchemyTransactionCommandRepository(session).confirm_reviewed_draft(
                        ConfirmTransactionDraftCommand(owner_id=owner_id, expected=draft_ref)
                    )
                await session.rollback()

        await asyncio.wait_for(
            asyncio.gather(archive_winner(), confirm_contender()),
            timeout=8,
        )

        async with factory() as verification:
            assert (
                await verification.scalar(
                    select(func.count(Transaction.id)).where(Transaction.user_id == owner_id)
                )
                == 0
            )
            assert await verification.scalar(select(Draft).where(Draft.user_id == owner_id))
            account = await verification.scalar(select(Account).where(Account.id == target_id))
            assert account is not None and account.archived_at is not None
    finally:
        await _cleanup_owner(engine, owner_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_edit_loses_cleanly_when_category_archive_wins_owner_lock() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    (
        owner_id,
        _primary_id,
        _target_id,
        target_category_id,
        transaction_id,
        _draft_ref,
    ) = await _setup_owner(factory, with_transaction=True)
    assert transaction_id is not None
    archived = asyncio.Event()
    edit_started = asyncio.Event()

    try:

        async def archive_winner() -> None:
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                await archive_category(
                    session,
                    owner_id,
                    target_category_id,
                    expected_version=1,
                )
                archived.set()
                await asyncio.wait_for(edit_started.wait(), timeout=2)
                await asyncio.sleep(0)
                await session.commit()

        async def edit_contender() -> None:
            await asyncio.wait_for(archived.wait(), timeout=2)
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                edit_started.set()
                with pytest.raises(CatalogUnavailableError):
                    await SqlAlchemyTransactionCommandRepository(session).edit(
                        EditTransactionCommand(
                            owner_id=owner_id,
                            transaction_id=transaction_id,
                            expected_version=1,
                            category_id=target_category_id,
                        )
                    )
                await session.rollback()

        await asyncio.wait_for(
            asyncio.gather(archive_winner(), edit_contender()),
            timeout=8,
        )

        async with factory() as verification:
            transaction = await verification.scalar(
                select(Transaction).where(Transaction.id == transaction_id)
            )
            category = await verification.scalar(
                select(Category).where(Category.id == target_category_id)
            )
            assert transaction is not None
            assert transaction.version == 1
            assert transaction.category_id != target_category_id
            assert category is not None and category.archived_at is not None
    finally:
        await _cleanup_owner(engine, owner_id)
        await engine.dispose()
