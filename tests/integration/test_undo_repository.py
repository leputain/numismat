import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, update
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
    ProcessedUpdate,
    TelegramResponseOutbox,
    Transaction,
    User,
)
from finbot.adapters.database.repositories.queries import SqlAlchemyQueryRepository
from finbot.adapters.database.repositories.undo import SqlAlchemyUndoRepository
from finbot.adapters.database.services.outbox import queue_send_message
from finbot.adapters.telegram.controllers.undo import (
    TelegramUndoContext,
    UndoController,
    UndoReceiptEnqueuer,
    UndoReceiptSnapshot,
    UndoSessionUseCases,
)
from finbot.adapters.telegram.executor import TelegramMutationExecutor, TelegramMutationRequest
from finbot.application.undo import UndoAction
from finbot.application.use_cases.queries import GetOwnerSettings
from finbot.application.use_cases.undo import UndoLastAction


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


def _synthetic_telegram_id() -> int:
    return 8_000_000_000 + uuid4().int % 900_000_000


def _synthetic_update_id() -> int:
    return 7_000_000_000 + uuid4().int % 900_000_000


async def _setup_owner(
    factory: async_sessionmaker[AsyncSession],
    *,
    action: str | None,
    deleted: bool = False,
    version: int = 1,
    audit_data: dict[str, object] | None = None,
) -> tuple[UUID, UUID, int]:
    telegram_id = _synthetic_telegram_id()
    async with factory() as session:
        owner = User(
            telegram_user_id=telegram_id,
            telegram_chat_id=telegram_id,
            locale="ru",
            timezone="UTC",
            base_currency="RUB",
            fast_mode=False,
        )
        session.add(owner)
        await session.flush()
        account = Account(
            user_id=owner.id,
            name="Synthetic account",
            slug=f"synthetic-account-{uuid4().hex}",
            currency="RUB",
        )
        category = Category(
            user_id=owner.id,
            kind="expense",
            name="Synthetic category",
            slug=f"synthetic-category-{uuid4().hex}",
            emoji="▫️",
        )
        session.add_all((account, category))
        await session.flush()
        owner.default_account_id = account.id
        transaction = Transaction(
            user_id=owner.id,
            type="expense",
            amount_minor=12_500,
            currency="RUB",
            account_id=account.id,
            category_id=category.id,
            occurred_at=datetime(2026, 8, 13, 12, tzinfo=UTC),
            description="synthetic transaction",
            source="manual",
            deleted_at=datetime(2026, 8, 13, 13, tzinfo=UTC) if deleted else None,
            version=version,
        )
        session.add(transaction)
        await session.flush()
        if action is not None:
            session.add(
                AuditEvent(
                    user_id=owner.id,
                    transaction_id=transaction.id,
                    action=action,
                    data=audit_data or {},
                    created_at=datetime(2026, 8, 13, 14, tzinfo=UTC),
                )
            )
        await session.commit()
        return owner.id, transaction.id, telegram_id


async def _cleanup(
    engine: AsyncEngine,
    owner_id: UUID,
    update_ids: tuple[int, ...] = (),
) -> None:
    async with engine.begin() as connection:
        if update_ids:
            await connection.execute(
                delete(ProcessedUpdate).where(ProcessedUpdate.update_id.in_(update_ids))
            )
        await connection.execute(delete(AuditEvent).where(AuditEvent.user_id == owner_id))
        await connection.execute(delete(Transaction).where(Transaction.user_id == owner_id))
        await connection.execute(
            update(User).where(User.id == owner_id).values(default_account_id=None)
        )
        await connection.execute(delete(Category).where(Category.user_id == owner_id))
        await connection.execute(delete(Account).where(Account.user_id == owner_id))
        await connection.execute(delete(User).where(User.id == owner_id))


def _request(telegram_id: int, update_id: int) -> TelegramMutationRequest:
    return TelegramMutationRequest(
        update_id=update_id,
        owner_telegram_user_id=telegram_id,
        chat_id=telegram_id,
        locale="ru",
        timezone="UTC",
        currency="RUB",
    )


def _controller(
    factory: async_sessionmaker[AsyncSession],
    enqueue: UndoReceiptEnqueuer,
) -> UndoController:
    def use_cases(session: AsyncSession) -> UndoSessionUseCases:
        reader = SqlAlchemyQueryRepository(session)
        return UndoSessionUseCases(
            undo_last=UndoLastAction(SqlAlchemyUndoRepository(session)),
            get_owner_settings=GetOwnerSettings(reader),
        )

    return UndoController(
        TelegramMutationExecutor(factory),
        use_cases,
        enqueue,
    )


async def _enqueue_receipt(
    session: AsyncSession,
    request: TelegramMutationRequest,
    receipt: UndoReceiptSnapshot,
) -> None:
    assert request.update_id is not None
    queue_send_message(
        session,
        update_id=request.update_id,
        owner_telegram_user_id=request.owner_telegram_user_id,
        chat_id=request.chat_id,
        text="undo receipt" if receipt.result is not None else "nothing to undo",
        parse_mode="HTML",
    )


@pytest.mark.asyncio
async def test_repository_reverses_each_audited_action_in_strict_reverse_order() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    owner_id, transaction_id, _telegram_id = await _setup_owner(
        factory,
        action=None,
        version=4,
    )
    original_occurred_at = datetime(2026, 8, 12, 10, tzinfo=UTC)
    try:
        async with factory() as session:
            transaction = await session.get(Transaction, transaction_id)
            assert transaction is not None
            account_id = transaction.account_id
            category_id = transaction.category_id
            transaction.amount_minor = 25_000
            base = datetime(2026, 8, 13, 8, tzinfo=UTC)
            session.add_all(
                (
                    AuditEvent(
                        user_id=owner_id,
                        transaction_id=transaction_id,
                        action="create",
                        created_at=base,
                    ),
                    AuditEvent(
                        user_id=owner_id,
                        transaction_id=transaction_id,
                        action="delete",
                        created_at=base + timedelta(minutes=1),
                    ),
                    AuditEvent(
                        user_id=owner_id,
                        transaction_id=transaction_id,
                        action="restore",
                        created_at=base + timedelta(minutes=2),
                    ),
                    AuditEvent(
                        user_id=owner_id,
                        transaction_id=transaction_id,
                        action="update",
                        data={
                            "old": {
                                "amount_minor": 12_500,
                                "category_id": str(category_id),
                                "account_id": str(account_id),
                                "currency": "RUB",
                                "occurred_at": original_occurred_at.isoformat(),
                                "description": "previous description",
                                "deleted_at": None,
                            }
                        },
                        created_at=base + timedelta(minutes=3),
                    ),
                )
            )
            await session.commit()

        async with factory() as session:
            repository = SqlAlchemyUndoRepository(session)
            results = [await repository.undo_last(owner_id) for _ in range(4)]
            await session.commit()

        assert [result.action for result in results if result is not None] == [
            UndoAction.UPDATE,
            UndoAction.RESTORE,
            UndoAction.DELETE,
            UndoAction.CREATE,
        ]
        assert results[0] is not None
        assert results[0].transaction.amount_minor == 12_500
        assert results[0].transaction.occurred_at == original_occurred_at
        assert results[0].transaction.version == 5
        assert results[1] is not None and results[1].transaction.deleted_at is not None
        assert results[2] is not None and results[2].transaction.deleted_at is None
        assert results[3] is not None and results[3].transaction.deleted_at is not None
        assert results[3].transaction.version == 8
    finally:
        await _cleanup(engine, owner_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_tracked_outbox_failure_rolls_back_then_same_update_replays_once() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    update_id = _synthetic_update_id()
    owner_id, transaction_id, telegram_id = await _setup_owner(
        factory,
        action="delete",
        deleted=True,
        version=2,
    )

    async def fail_enqueue(
        _session: AsyncSession,
        _request: TelegramMutationRequest,
        _receipt: UndoReceiptSnapshot,
    ) -> None:
        raise RuntimeError("synthetic outbox failure")

    context = TelegramUndoContext(_request(telegram_id, update_id))
    try:
        with pytest.raises(RuntimeError, match="outbox failure"):
            await _controller(factory, fail_enqueue).execute(context)

        async with factory() as verification:
            transaction = await verification.get(Transaction, transaction_id)
            event = await verification.scalar(
                select(AuditEvent).where(AuditEvent.user_id == owner_id)
            )
            assert transaction is not None and transaction.deleted_at is not None
            assert transaction.version == 2
            assert event is not None and event.undone_at is None
            assert await verification.get(ProcessedUpdate, update_id) is None

        controller = _controller(factory, _enqueue_receipt)
        receipt = await controller.execute(context)
        duplicate = await controller.execute(context)

        assert receipt is not None and receipt.result is not None
        assert receipt.result.action is UndoAction.DELETE
        assert duplicate is None
        async with factory() as verification:
            transaction = await verification.get(Transaction, transaction_id)
            event = await verification.scalar(
                select(AuditEvent).where(AuditEvent.user_id == owner_id)
            )
            outbox_count = await verification.scalar(
                select(func.count(TelegramResponseOutbox.id)).where(
                    TelegramResponseOutbox.update_id == update_id
                )
            )
            assert transaction is not None and transaction.deleted_at is None
            assert transaction.version == 3
            assert event is not None and event.undone_at is not None
            assert outbox_count == 1
    finally:
        await _cleanup(engine, owner_id, (update_id,))
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_undo_requests_reverse_one_audit_event_only_once() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    update_ids = (_synthetic_update_id(), _synthetic_update_id())
    owner_id, transaction_id, telegram_id = await _setup_owner(
        factory,
        action="create",
        version=1,
    )
    controller = _controller(factory, _enqueue_receipt)
    try:
        receipts = await asyncio.wait_for(
            asyncio.gather(
                *(
                    controller.execute(TelegramUndoContext(_request(telegram_id, update_id)))
                    for update_id in update_ids
                )
            ),
            timeout=8,
        )

        assert sum(receipt is not None and receipt.result is not None for receipt in receipts) == 1
        async with factory() as verification:
            transaction = await verification.get(Transaction, transaction_id)
            event = await verification.scalar(
                select(AuditEvent).where(AuditEvent.user_id == owner_id)
            )
            outbox_count = await verification.scalar(
                select(func.count(TelegramResponseOutbox.id)).where(
                    TelegramResponseOutbox.update_id.in_(update_ids)
                )
            )
            assert transaction is not None and transaction.deleted_at is not None
            assert transaction.version == 2
            assert event is not None and event.undone_at is not None
            assert outbox_count == 2
    finally:
        await _cleanup(engine, owner_id, update_ids)
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_transaction_without_audit_is_never_guessed_or_mutated() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    owner_id, transaction_id, _telegram_id = await _setup_owner(
        factory,
        action=None,
        version=6,
    )
    try:
        async with factory() as session:
            assert await SqlAlchemyUndoRepository(session).undo_last(owner_id) is None
            await session.commit()
        async with factory() as verification:
            transaction = await verification.get(Transaction, transaction_id)
            assert transaction is not None
            assert transaction.version == 6
            assert transaction.deleted_at is None
    finally:
        await _cleanup(engine, owner_id)
        await engine.dispose()
