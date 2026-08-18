import asyncio
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from finbot.adapters.database.models import Account, Category, CategoryRule, Draft, User
from finbot.adapters.database.repositories.draft_preparation import (
    SqlAlchemyDraftPreparationRepository,
)
from finbot.adapters.database.repositories.drafts import SqlAlchemyDraftRepository
from finbot.adapters.database.repositories.ocr_queue import (
    SqlAlchemyOcrQueueCommandRepository,
)
from finbot.adapters.database.repositories.queries import SqlAlchemyQueryRepository
from finbot.adapters.database.services.onboarding import ensure_owner_user
from finbot.application.dto import (
    DraftSnapshot,
    OcrImageIngressStatus,
    ProcessOcrImageCommand,
    ProcessOcrImageResult,
)
from finbot.application.use_cases.draft_preparation import (
    PrepareParsedDraft,
    SystemDraftPreparationClock,
)
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.application.use_cases.ocr_queue import ProcessOcrImage, SharedOcrDraftPreparer


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


class _Extractor:
    def __init__(self) -> None:
        self.calls = 0

    async def extract_text(self, content: bytes, mime_type: str) -> str:
        assert content == b"synthetic-bounded-image"
        assert mime_type == "image/png"
        self.calls += 1
        return "ООО Ромашка\nИТОГО 1450,00 ₽\n12.08.2026 10:30"


class _BlockingExtractor(_Extractor):
    def __init__(self, started: asyncio.Event, release: asyncio.Event) -> None:
        super().__init__()
        self._started = started
        self._release = release

    async def extract_text(self, content: bytes, mime_type: str) -> str:
        self._started.set()
        await asyncio.wait_for(self._release.wait(), timeout=3)
        return await super().extract_text(content, mime_type)


def _synthetic_telegram_user_id() -> int:
    return 7_800_000_000 + uuid4().int % 1_000_000_000


async def _create_owner(factory: async_sessionmaker[AsyncSession]) -> UUID:
    async with factory() as session:
        owner = await ensure_owner_user(
            session,
            telegram_user_id=_synthetic_telegram_user_id(),
            telegram_chat_id=_synthetic_telegram_user_id(),
            locale="ru",
            timezone="Europe/Moscow",
            currency="RUB",
        )
        owner_id = owner.id
        await session.commit()
        return owner_id


async def _cleanup_owner(engine: AsyncEngine, owner_id: UUID) -> None:
    async with engine.begin() as connection:
        await connection.execute(delete(CategoryRule).where(CategoryRule.user_id == owner_id))
        await connection.execute(delete(Draft).where(Draft.user_id == owner_id))
        await connection.execute(
            update(User).where(User.id == owner_id).values(default_account_id=None)
        )
        await connection.execute(delete(Category).where(Category.user_id == owner_id))
        await connection.execute(delete(Account).where(Account.user_id == owner_id))
        await connection.execute(delete(User).where(User.id == owner_id))


def _process_image(session: AsyncSession, extractor: _Extractor) -> ProcessOcrImage:
    reader = SqlAlchemyQueryRepository(session)
    preparation = SqlAlchemyDraftPreparationRepository(session)
    return ProcessOcrImage(
        extractor,
        SqlAlchemyOcrQueueCommandRepository(session),
        SharedOcrDraftPreparer(
            PrepareParsedDraft(
                reader,
                preparation,
                preparation,
                SystemDraftPreparationClock(),
            )
        ),
        DraftUseCases(SqlAlchemyDraftRepository(session)),
    )


@pytest.mark.asyncio
async def test_process_image_persists_only_canonical_queue_draft() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    owner_id = await _create_owner(factory)
    extractor = _Extractor()

    try:
        async with factory() as session:
            result = await _process_image(session, extractor)(
                ProcessOcrImageCommand(
                    owner_id,
                    b"synthetic-bounded-image",
                    "image/png",
                )
            )
            assert result.status is OcrImageIngressStatus.STARTED
            assert result.queue_item is not None
            assert (result.queue_item.position, result.queue_item.total) == (1, 1)
            await session.commit()

        async with factory() as verification:
            stored = await verification.scalar(select(Draft).where(Draft.user_id == owner_id))
            assert stored is not None
            assert stored.state == "review"
            assert stored.payload["flow"] == "ocr"
            assert stored.payload["amount_minor"] == 145_000
            assert "amount" not in stored.payload
            assert stored.payload["ocr_batch"] == {
                "version": 1,
                "index": 1,
                "total": 1,
                "saved": 0,
                "skipped": 0,
                "remaining": [],
            }
            rendered = repr(stored.payload)
            assert "synthetic-bounded-image" not in rendered
            assert "ИТОГО" not in rendered
            assert "ui_message_id" not in stored.payload
        assert extractor.calls == 1
    finally:
        await _cleanup_owner(engine, owner_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_active_draft_and_pending_intent_are_preserved_without_running_ocr() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    owner_id = await _create_owner(factory)
    extractor = _Extractor()

    try:
        async with factory() as session:
            existing = await SqlAlchemyDraftRepository(session).create_if_absent(
                owner_id,
                "wizard_amount",
                {
                    "flow": "wizard",
                    "pending_intent": {"kind": "wizard"},
                },
            )
            await session.commit()

        async with factory() as session:
            result = await _process_image(session, extractor)(
                ProcessOcrImageCommand(
                    owner_id,
                    b"synthetic-bounded-image",
                    "image/png",
                )
            )
            assert result.status is OcrImageIngressStatus.ACTIVE_DRAFT
            assert result.active_draft == existing
            await session.commit()

        async with factory() as verification:
            stored = await verification.scalar(select(Draft).where(Draft.user_id == owner_id))
            assert stored is not None
            assert stored.id == existing.draft_id
            assert stored.revision == existing.revision
            assert stored.state == "wizard_amount"
            assert stored.payload == {
                "flow": "wizard",
                "pending_intent": {"kind": "wizard"},
            }
        assert extractor.calls == 0
    finally:
        await _cleanup_owner(engine, owner_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_draft_winner_is_returned_without_overwrite() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    owner_id = await _create_owner(factory)
    extraction_started = asyncio.Event()
    winner_committed = asyncio.Event()
    extractor = _BlockingExtractor(extraction_started, winner_committed)

    try:

        async def process_image_loser() -> ProcessOcrImageResult:
            async with factory() as session:
                result = await _process_image(session, extractor)(
                    ProcessOcrImageCommand(
                        owner_id,
                        b"synthetic-bounded-image",
                        "image/png",
                    )
                )
                await session.commit()
                return result

        async def create_draft_winner() -> DraftSnapshot:
            await asyncio.wait_for(extraction_started.wait(), timeout=3)
            async with factory() as session:
                winner = await SqlAlchemyDraftRepository(session).create_if_absent(
                    owner_id,
                    "wizard_amount",
                    {
                        "flow": "wizard",
                        "pending_intent": {"kind": "wizard"},
                    },
                )
                await session.commit()
            winner_committed.set()
            return winner

        result, winner = await asyncio.wait_for(
            asyncio.gather(process_image_loser(), create_draft_winner()),
            timeout=8,
        )

        assert result.status is OcrImageIngressStatus.ACTIVE_DRAFT
        assert result.active_draft == winner
        assert extractor.calls == 1
        async with factory() as verification:
            stored = await verification.scalar(select(Draft).where(Draft.user_id == owner_id))
            assert stored is not None
            assert stored.id == winner.draft_id
            assert stored.revision == winner.revision
            assert stored.payload == {
                "flow": "wizard",
                "pending_intent": {"kind": "wizard"},
            }
    finally:
        winner_committed.set()
        await _cleanup_owner(engine, owner_id)
        await engine.dispose()
