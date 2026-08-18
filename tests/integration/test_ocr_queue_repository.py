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
    Transaction,
    User,
)
from finbot.adapters.database.repositories.ocr_queue import (
    SqlAlchemyOcrQueueCommandRepository,
)
from finbot.application.dto import (
    DraftRef,
    OcrQueueCandidate,
    OcrQueueContinuation,
    OcrQueueMutationCommand,
    OcrQueueStatus,
    PreparedOcrDraft,
)
from finbot.application.errors import DraftRevisionConflictError
from finbot.application.ocr_queue import OcrQueueState, decode_ocr_queue, encode_ocr_queue
from finbot.domain.transactions import TransactionType


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
    return 7_000_000_000 + uuid4().int % 1_000_000_000


async def _cleanup_owner(engine: AsyncEngine, owner_id: UUID) -> None:
    async with engine.begin() as connection:
        await connection.execute(delete(CategoryRule).where(CategoryRule.user_id == owner_id))
        await connection.execute(delete(AuditEvent).where(AuditEvent.user_id == owner_id))
        await connection.execute(delete(Transaction).where(Transaction.user_id == owner_id))
        await connection.execute(delete(Draft).where(Draft.user_id == owner_id))
        await connection.execute(
            update(User).where(User.id == owner_id).values(default_account_id=None)
        )
        await connection.execute(delete(Category).where(Category.user_id == owner_id))
        await connection.execute(delete(Account).where(Account.user_id == owner_id))
        await connection.execute(delete(User).where(User.id == owner_id))


async def _setup_queue(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[UUID, UUID, UUID, DraftRef, OcrQueueCandidate]:
    async with factory() as session:
        owner = User(telegram_user_id=_synthetic_telegram_user_id())
        session.add(owner)
        await session.flush()
        account = Account(
            user_id=owner.id,
            name="Тестовый счёт",
            slug="тестовый-счет",
            currency="RUB",
        )
        category = Category(
            user_id=owner.id,
            kind="expense",
            name="Тестовая категория",
            slug="тестовая-категория",
            emoji="▫️",
        )
        session.add_all((account, category))
        await session.flush()
        owner.default_account_id = account.id

        next_candidate = OcrQueueCandidate(
            kind=TransactionType.EXPENSE,
            amount_minor=2500,
            occurred_at=datetime(2026, 8, 14, 13, tzinfo=UTC),
            description="следующая операция",
        )
        queue = OcrQueueState.initial((next_candidate,))
        draft = Draft(
            user_id=owner.id,
            state="quick_confirm",
            payload={
                "flow": "quick",
                "type": "expense",
                "amount_minor": 1000,
                "account_id": str(account.id),
                "category_id": str(category.id),
                "occurred_at": "2026-08-13T12:00:00+00:00",
                "description": "магазин рядом",
                "category_explicit": False,
                "rule_offer_pattern": "магазин",
                "pending_rule": {
                    "pattern": "магазин",
                    "scope": "account",
                    "account_id": str(account.id),
                    "category_id": str(category.id),
                },
                "ocr_batch": encode_ocr_queue(queue),
            },
        )
        session.add(draft)
        await session.flush()
        draft_ref = DraftRef(draft.id, draft.revision)
        await session.commit()
        return owner.id, account.id, category.id, draft_ref, next_candidate


def _continuation(
    candidate: OcrQueueCandidate,
    account_id: UUID,
    category_id: UUID,
) -> OcrQueueContinuation:
    return OcrQueueContinuation(
        expected_candidate=candidate,
        prepared=PreparedOcrDraft(
            "quick_confirm",
            {
                "type": candidate.kind.value,
                "amount_minor": candidate.amount_minor,
                "account_id": str(account_id),
                "category_id": str(category_id),
                "occurred_at": candidate.occurred_at.isoformat()
                if candidate.occurred_at is not None
                else "2026-08-14T13:00:00+00:00",
                "description": candidate.description,
                "category_explicit": False,
            },
        ),
    )


@pytest.mark.asyncio
async def test_confirm_advances_atomically_applies_rule_and_rolls_back_cleanly() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    owner_id, account_id, category_id, draft_ref, candidate = await _setup_queue(factory)
    command = OcrQueueMutationCommand(
        owner_id,
        draft_ref,
        _continuation(candidate, account_id, category_id),
    )

    try:
        async with factory() as session:
            result = await SqlAlchemyOcrQueueCommandRepository(session).confirm_and_continue(
                command
            )
            assert result.status is OcrQueueStatus.ADVANCED
            assert result.saved == 1 and result.skipped == 0
            assert result.transaction is not None
            assert result.draft is not None
            assert result.draft.draft_id == draft_ref.draft_id
            assert result.draft.revision == draft_ref.revision + 1
            assert result.draft.payload["flow"] == "ocr"
            queue = decode_ocr_queue(result.draft.payload)
            assert queue.position == 2 and queue.remaining == ()
            assert (
                await session.scalar(
                    select(func.count(CategoryRule.id)).where(CategoryRule.user_id == owner_id)
                )
                == 1
            )
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
            assert (
                await verification.scalar(
                    select(func.count(CategoryRule.id)).where(CategoryRule.user_id == owner_id)
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
async def test_concurrent_confirm_has_one_winner_and_final_skip_creates_no_transaction() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    owner_id, account_id, category_id, draft_ref, candidate = await _setup_queue(factory)
    command = OcrQueueMutationCommand(
        owner_id,
        draft_ref,
        _continuation(candidate, account_id, category_id),
    )

    try:

        async def contender() -> str:
            async with factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '3s'"))
                try:
                    await SqlAlchemyOcrQueueCommandRepository(session).confirm_and_continue(command)
                    await session.commit()
                    return "confirmed"
                except DraftRevisionConflictError:
                    await session.rollback()
                    return "stale"

        outcomes = await asyncio.wait_for(
            asyncio.gather(contender(), contender()),
            timeout=8,
        )
        assert sorted(outcomes) == ["confirmed", "stale"]

        async with factory() as session:
            draft = await session.scalar(select(Draft).where(Draft.user_id == owner_id))
            assert draft is not None
            assert draft.revision == draft_ref.revision + 1
            final = await SqlAlchemyOcrQueueCommandRepository(session).skip_and_continue(
                OcrQueueMutationCommand(owner_id, DraftRef(draft.id, draft.revision))
            )
            assert final.status is OcrQueueStatus.COMPLETED
            assert final.saved == 1 and final.skipped == 1
            await session.commit()

        async with factory() as verification:
            transaction_count = await verification.scalar(
                select(func.count(Transaction.id)).where(Transaction.user_id == owner_id)
            )
            audit_count = await verification.scalar(
                select(func.count(AuditEvent.id)).where(AuditEvent.user_id == owner_id)
            )
            assert transaction_count == 1
            assert audit_count == 1
            assert await verification.scalar(select(Draft).where(Draft.user_id == owner_id)) is None
    finally:
        await _cleanup_owner(engine, owner_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_final_confirm_consumes_queue_and_cancel_never_creates_transaction() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    owner_id, account_id, category_id, draft_ref, candidate = await _setup_queue(factory)

    try:
        async with factory() as session:
            repository = SqlAlchemyOcrQueueCommandRepository(session)
            advanced = await repository.confirm_and_continue(
                OcrQueueMutationCommand(
                    owner_id,
                    draft_ref,
                    _continuation(candidate, account_id, category_id),
                )
            )
            assert advanced.draft is not None
            completed = await repository.confirm_and_continue(
                OcrQueueMutationCommand(owner_id, advanced.draft.ref)
            )
            assert completed.status is OcrQueueStatus.COMPLETED
            assert completed.saved == 2 and completed.skipped == 0
            assert completed.transaction is not None
            await session.commit()

        async with factory() as verification:
            assert (
                await verification.scalar(
                    select(func.count(Transaction.id)).where(Transaction.user_id == owner_id)
                )
                == 2
            )
            assert (
                await verification.scalar(
                    select(func.count(AuditEvent.id)).where(AuditEvent.user_id == owner_id)
                )
                == 2
            )
            assert await verification.scalar(select(Draft).where(Draft.user_id == owner_id)) is None
    finally:
        await _cleanup_owner(engine, owner_id)

    cancelled_owner, _account, _category, cancel_ref, _candidate_value = await _setup_queue(factory)
    try:
        async with factory() as session:
            cancelled = await SqlAlchemyOcrQueueCommandRepository(session).cancel(
                OcrQueueMutationCommand(cancelled_owner, cancel_ref)
            )
            assert cancelled.status is OcrQueueStatus.CANCELLED
            assert cancelled.saved == 0 and cancelled.skipped == 0
            await session.commit()

        async with factory() as verification:
            assert (
                await verification.scalar(
                    select(func.count(Transaction.id)).where(Transaction.user_id == cancelled_owner)
                )
                == 0
            )
            assert (
                await verification.scalar(select(Draft).where(Draft.user_id == cancelled_owner))
                is None
            )
    finally:
        await _cleanup_owner(engine, cancelled_owner)
        await engine.dispose()
