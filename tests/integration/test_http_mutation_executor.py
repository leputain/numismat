from __future__ import annotations

import asyncio
import base64
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid7

import httpx2
import pytest
from fastapi import FastAPI
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
    CategoryRule,
    Draft,
    HttpIdempotencyRecord,
    Transaction,
    User,
    WebSession,
)
from finbot.adapters.database.repositories.http_auth import (
    SqlAlchemyAuthUnitOfWorkFactory,
)
from finbot.adapters.database.repositories.http_idempotency import (
    IdempotencyResultKind,
)
from finbot.adapters.database.repositories.http_mutations import (
    SqlAlchemyHttpMutationUnitOfWorkFactory,
)
from finbot.adapters.database.repositories.web_sessions import (
    SqlAlchemyWebSessionRepository,
)
from finbot.adapters.http.app import create_app
from finbot.adapters.http.auth.cookies import CSRF_COOKIE, SESSION_COOKIE
from finbot.adapters.http.auth.crypto import HttpSecurityDigester
from finbot.adapters.http.auth.service import SessionInvalidError, TelegramAuthService
from finbot.adapters.http.mutations.ports import MutationUnitOfWork
from finbot.adapters.http.mutations.service import (
    HttpMutationExecutor,
    HttpRevisionMutationService,
    IdempotencyKeyReuseError,
    MutationCredentials,
    MutationOperation,
    MutationReceipt,
)
from finbot.application.dto import DraftRef
from finbot.application.errors import (
    EntityNotFoundError,
    ObjectVersionConflictError,
)
from finbot.domain.transactions import TransactionType

DATABASE_URL = os.environ["TEST_DATABASE_URL"]
SECURITY_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
ORIGIN = "https://miniapp.mutations.integration.test"
NOW = datetime(2026, 8, 14, 10, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _Fixture:
    owner_id: UUID = field(repr=False)
    account_id: UUID = field(repr=False)
    category_id: UUID = field(repr=False)
    transaction_id: UUID | None = field(default=None, repr=False)
    draft_ref: DraftRef | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class _AuthTokens:
    session_token: str = field(repr=False)
    csrf_token: str = field(repr=False)

    def credentials(self, idempotency_key: str) -> MutationCredentials:
        return MutationCredentials(
            self.session_token,
            self.csrf_token,
            self.csrf_token,
            idempotency_key,
        )


class _ReadinessStub:
    async def check(self) -> None:
        return None


def _opaque_token() -> str:
    raw = uuid4().bytes + uuid4().bytes
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _synthetic_telegram_user_id() -> int:
    return 9_900_000_000 + uuid4().int % 90_000_000


async def _setup_owner(
    factory: async_sessionmaker[AsyncSession],
    *,
    transaction_state: str | None = None,
    review_draft: bool = False,
) -> _Fixture:
    async with factory.begin() as session:
        telegram_user_id = _synthetic_telegram_user_id()
        owner = User(
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_user_id,
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

        transaction_id: UUID | None = None
        if transaction_state is not None:
            transaction = Transaction(
                user_id=owner.id,
                type=TransactionType.EXPENSE.value,
                amount_minor=1_000,
                currency="RUB",
                account_id=account.id,
                category_id=category.id,
                occurred_at=NOW,
                description="",
                deleted_at=NOW if transaction_state == "deleted" else None,
                version=2 if transaction_state == "deleted" else 1,
            )
            session.add(transaction)
            await session.flush()
            transaction_id = transaction.id

        draft_ref: DraftRef | None = None
        if review_draft:
            draft = Draft(
                user_id=owner.id,
                state="quick_confirm",
                payload={
                    "flow": "quick",
                    "type": "expense",
                    "amount_minor": 500,
                    "account_id": str(account.id),
                    "category_id": str(category.id),
                    "occurred_at": NOW.isoformat(),
                    "description": "",
                },
            )
            session.add(draft)
            await session.flush()
            draft_ref = DraftRef(draft.id, draft.revision)

        return _Fixture(
            owner_id=owner.id,
            account_id=account.id,
            category_id=category.id,
            transaction_id=transaction_id,
            draft_ref=draft_ref,
        )


async def _create_auth_session(
    factory: async_sessionmaker[AsyncSession],
    owner_id: UUID,
    digester: HttpSecurityDigester,
) -> _AuthTokens:
    tokens = _AuthTokens(_opaque_token(), _opaque_token())
    async with factory.begin() as session:
        await SqlAlchemyWebSessionRepository(session).create(
            owner_id,
            digester.session(tokens.session_token),
            digester.csrf(tokens.csrf_token),
            created_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )
    return tokens


def _executor(
    factory: async_sessionmaker[AsyncSession],
    digester: HttpSecurityDigester,
) -> HttpMutationExecutor:
    return HttpMutationExecutor(
        digester=digester,
        uow_factory=SqlAlchemyHttpMutationUnitOfWorkFactory(factory),
        clock=lambda: NOW,
    )


def _app(
    factory: async_sessionmaker[AsyncSession],
    digester: HttpSecurityDigester,
) -> FastAPI:
    app: FastAPI = create_app(
        readiness_probe=_ReadinessStub(),
        mutation_service=HttpRevisionMutationService(_executor(factory, digester)),
        mutation_origin=ORIGIN,
    )
    return app


def _client(app: FastAPI) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
        base_url=ORIGIN,
    )


def _cookie_header(tokens: _AuthTokens) -> str:
    return f"{SESSION_COOKIE}={tokens.session_token}; {CSRF_COOKIE}={tokens.csrf_token}"


def _read_headers(tokens: _AuthTokens) -> dict[str, str]:
    return {"Cookie": _cookie_header(tokens)}


def _mutation_headers(
    tokens: _AuthTokens,
    idempotency_key: str,
) -> dict[str, str]:
    return {
        "Cookie": _cookie_header(tokens),
        "Idempotency-Key": idempotency_key,
        "Origin": ORIGIN,
        "X-CSRF-Token": tokens.csrf_token,
    }


async def _cleanup(engine: AsyncEngine, fixture: _Fixture) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            delete(HttpIdempotencyRecord).where(HttpIdempotencyRecord.user_id == fixture.owner_id)
        )
        await connection.execute(delete(WebSession).where(WebSession.user_id == fixture.owner_id))
        await connection.execute(
            delete(CategoryRule).where(CategoryRule.user_id == fixture.owner_id)
        )
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
async def test_same_key_concurrent_confirm_replays_one_committed_transaction() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup_owner(factory, review_draft=True)
    assert fixture.draft_ref is not None
    digester = HttpSecurityDigester(SECURITY_KEY)
    tokens = await _create_auth_session(factory, fixture.owner_id, digester)
    service = HttpRevisionMutationService(_executor(factory, digester))
    credentials = tokens.credentials(_opaque_token())

    try:
        first, second = await asyncio.wait_for(
            asyncio.gather(
                service.confirm_draft(credentials, fixture.draft_ref),
                service.confirm_draft(credentials, fixture.draft_ref),
            ),
            timeout=8,
        )

        assert (first.http_status, second.http_status) == (201, 201)
        assert first.kind is second.kind is IdempotencyResultKind.TRANSACTION
        assert (first.result_id, first.revision) == (second.result_id, second.revision)
        assert {first.replayed, second.replayed} == {False, True}
        async with factory() as verification:
            assert (
                await verification.scalar(
                    select(func.count(Transaction.id)).where(
                        Transaction.user_id == fixture.owner_id
                    )
                )
                == 1
            )
            assert await verification.get(Draft, fixture.draft_ref.draft_id) is None
            records = (
                await verification.scalars(
                    select(HttpIdempotencyRecord).where(
                        HttpIdempotencyRecord.user_id == fixture.owner_id,
                        HttpIdempotencyRecord.operation == "draft.confirm",
                    )
                )
            ).all()
            assert len(records) == 1 and records[0].status == "completed"
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.asyncio
async def test_different_keys_same_revision_have_one_confirm_and_one_not_found() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup_owner(factory, review_draft=True)
    assert fixture.draft_ref is not None
    digester = HttpSecurityDigester(SECURITY_KEY)
    tokens = await _create_auth_session(factory, fixture.owner_id, digester)
    service = HttpRevisionMutationService(_executor(factory, digester))

    async def contender(idempotency_key: str) -> int:
        try:
            receipt = await service.confirm_draft(
                tokens.credentials(idempotency_key),
                fixture.draft_ref,
            )
            return int(receipt.http_status)
        except EntityNotFoundError:
            return 404

    try:
        statuses = await asyncio.wait_for(
            asyncio.gather(contender(_opaque_token()), contender(_opaque_token())),
            timeout=8,
        )
        assert sorted(statuses) == [201, 404]

        async with factory() as verification:
            assert (
                await verification.scalar(
                    select(func.count(Transaction.id)).where(
                        Transaction.user_id == fixture.owner_id
                    )
                )
                == 1
            )
            assert (
                await verification.scalar(
                    select(func.count(HttpIdempotencyRecord.id)).where(
                        HttpIdempotencyRecord.user_id == fixture.owner_id,
                        HttpIdempotencyRecord.operation == "draft.confirm",
                    )
                )
                == 1
            )
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.asyncio
async def test_same_key_fingerprint_mismatch_never_runs_a_second_mutation() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup_owner(factory, transaction_state="active")
    assert fixture.transaction_id is not None
    digester = HttpSecurityDigester(SECURITY_KEY)
    tokens = await _create_auth_session(factory, fixture.owner_id, digester)
    service = HttpRevisionMutationService(_executor(factory, digester))
    credentials = tokens.credentials(_opaque_token())

    try:
        deleted = await service.delete_transaction(
            credentials,
            fixture.transaction_id,
            1,
        )
        with pytest.raises(IdempotencyKeyReuseError):
            await service.delete_transaction(
                credentials,
                fixture.transaction_id,
                2,
            )

        assert (deleted.http_status, deleted.revision) == (200, 2)
        async with factory() as verification:
            transaction = await verification.get(Transaction, fixture.transaction_id)
            assert transaction is not None
            assert transaction.deleted_at is not None and transaction.version == 2
            assert (
                await verification.scalar(
                    select(func.count(AuditEvent.id)).where(AuditEvent.user_id == fixture.owner_id)
                )
                == 1
            )
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.asyncio
async def test_post_mutation_failure_rolls_back_claim_and_retry_is_new() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup_owner(factory, review_draft=True)
    draft_ref = fixture.draft_ref
    assert draft_ref is not None
    digester = HttpSecurityDigester(SECURITY_KEY)
    tokens = await _create_auth_session(factory, fixture.owner_id, digester)
    executor = _executor(factory, digester)
    credentials = tokens.credentials(_opaque_token())
    semantics = {
        "draft_id": str(draft_ref.draft_id),
        "revision": draft_ref.revision,
    }
    allowed = frozenset({(201, IdempotencyResultKind.TRANSACTION)})

    async def fail_after_mutation(
        uow: MutationUnitOfWork,
        owner_id: UUID,
    ) -> MutationReceipt:
        await uow.commands.confirm_draft(
            owner_id,
            draft_ref.draft_id,
            draft_ref.revision,
        )
        raise RuntimeError("synthetic receipt failure")

    async def succeed(
        uow: MutationUnitOfWork,
        owner_id: UUID,
    ) -> MutationReceipt:
        result = await uow.commands.confirm_draft(
            owner_id,
            draft_ref.draft_id,
            draft_ref.revision,
        )
        return MutationReceipt(
            result.http_status,
            result.kind,
            result.result_id,
            result.revision,
        )

    try:
        with pytest.raises(RuntimeError, match="synthetic receipt failure"):
            await executor.execute(
                credentials,
                operation=MutationOperation.DRAFT_CONFIRM,
                semantic_request=semantics,
                allowed_results=allowed,
                mutate=fail_after_mutation,
            )

        async with factory() as rolled_back:
            assert await rolled_back.get(Draft, draft_ref.draft_id) is not None
            assert (
                await rolled_back.scalar(
                    select(func.count(Transaction.id)).where(
                        Transaction.user_id == fixture.owner_id
                    )
                )
                == 0
            )
            assert (
                await rolled_back.scalar(
                    select(func.count(HttpIdempotencyRecord.id)).where(
                        HttpIdempotencyRecord.user_id == fixture.owner_id,
                        HttpIdempotencyRecord.operation == "draft.confirm",
                    )
                )
                == 0
            )

        retried = await executor.execute(
            credentials,
            operation=MutationOperation.DRAFT_CONFIRM,
            semantic_request=semantics,
            allowed_results=allowed,
            mutate=succeed,
        )
        assert retried.http_status == 201 and not retried.replayed
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.asyncio
async def test_logout_waits_for_task14_mutation_transaction_then_revokes_session() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup_owner(factory)
    digester = HttpSecurityDigester(SECURITY_KEY)
    tokens = await _create_auth_session(factory, fixture.owner_id, digester)
    executor = _executor(factory, digester)
    credentials = tokens.credentials(_opaque_token())
    mutation_ready = asyncio.Event()
    release_mutation = asyncio.Event()
    logout_started = asyncio.Event()

    async def blocking_create(
        uow: MutationUnitOfWork,
        owner_id: UUID,
    ) -> MutationReceipt:
        status, draft = await uow.commands.create_draft(owner_id)
        mutation_ready.set()
        await release_mutation.wait()
        return MutationReceipt(
            status,
            IdempotencyResultKind.DRAFT,
            draft.draft_id,
            draft.revision,
        )

    async def logout() -> None:
        await mutation_ready.wait()
        logout_started.set()
        await TelegramAuthService(
            bot_token="123456:synthetic-token",
            owner_telegram_user_id=_synthetic_telegram_user_id(),
            digester=digester,
            uow_factory=SqlAlchemyAuthUnitOfWorkFactory(factory),
            clock=lambda: NOW,
        ).logout(tokens.session_token, tokens.csrf_token, tokens.csrf_token)

    mutation_task = asyncio.create_task(
        executor.execute(
            credentials,
            operation=MutationOperation.DRAFT_CREATE,
            semantic_request={},
            allowed_results=frozenset(
                {
                    (200, IdempotencyResultKind.DRAFT),
                    (201, IdempotencyResultKind.DRAFT),
                }
            ),
            mutate=blocking_create,
        )
    )
    logout_task = asyncio.create_task(logout())
    await logout_started.wait()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(logout_task), timeout=0.2)
    finally:
        release_mutation.set()

    try:
        receipt = await mutation_task
        await logout_task
        assert receipt.http_status == 201
        with pytest.raises(SessionInvalidError):
            await HttpRevisionMutationService(executor).create_draft(
                tokens.credentials(_opaque_token())
            )
        async with factory() as verification:
            session = await verification.scalar(
                select(WebSession).where(WebSession.user_id == fixture.owner_id)
            )
            assert session is not None and session.revoked_at == NOW
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.parametrize("operation", ["repeat", "edit"])
@pytest.mark.asyncio
async def test_repeat_and_edit_absent_slot_races_create_one_draft(
    operation: str,
) -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup_owner(factory, transaction_state="active")
    assert fixture.transaction_id is not None
    digester = HttpSecurityDigester(SECURITY_KEY)
    tokens = await _create_auth_session(factory, fixture.owner_id, digester)
    service = HttpRevisionMutationService(_executor(factory, digester))

    async def contender(idempotency_key: str) -> MutationReceipt:
        credentials = tokens.credentials(idempotency_key)
        if operation == "repeat":
            return await service.repeat_transaction(
                credentials,
                fixture.transaction_id,
                1,
            )
        return await service.begin_transaction_edit(
            credentials,
            fixture.transaction_id,
            1,
        )

    try:
        receipts = await asyncio.wait_for(
            asyncio.gather(contender(_opaque_token()), contender(_opaque_token())),
            timeout=8,
        )
        assert sorted(receipt.http_status for receipt in receipts) == [200, 201]
        assert len({receipt.result_id for receipt in receipts}) == 1

        async with factory() as verification:
            drafts = (
                await verification.scalars(select(Draft).where(Draft.user_id == fixture.owner_id))
            ).all()
            assert len(drafts) == 1
            assert "pending_intent" in drafts[0].payload
            assert (
                await verification.scalar(
                    select(func.count(HttpIdempotencyRecord.id)).where(
                        HttpIdempotencyRecord.user_id == fixture.owner_id
                    )
                )
                == 2
            )
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.parametrize(
    ("operation", "initial_state", "expected_version", "final_deleted", "final_version"),
    [
        ("delete", "active", 1, True, 2),
        ("restore", "deleted", 2, False, 3),
    ],
)
@pytest.mark.asyncio
async def test_delete_and_restore_version_races_mutate_once(
    operation: str,
    initial_state: str,
    expected_version: int,
    final_deleted: bool,
    final_version: int,
) -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup_owner(factory, transaction_state=initial_state)
    assert fixture.transaction_id is not None
    digester = HttpSecurityDigester(SECURITY_KEY)
    tokens = await _create_auth_session(factory, fixture.owner_id, digester)
    service = HttpRevisionMutationService(_executor(factory, digester))

    async def contender(idempotency_key: str) -> int:
        credentials = tokens.credentials(idempotency_key)
        try:
            if operation == "delete":
                receipt = await service.delete_transaction(
                    credentials,
                    fixture.transaction_id,
                    expected_version,
                )
            else:
                receipt = await service.restore_transaction(
                    credentials,
                    fixture.transaction_id,
                    expected_version,
                )
            return int(receipt.http_status)
        except ObjectVersionConflictError:
            return 409

    try:
        statuses = await asyncio.wait_for(
            asyncio.gather(contender(_opaque_token()), contender(_opaque_token())),
            timeout=8,
        )
        assert sorted(statuses) == [200, 409]

        async with factory() as verification:
            transaction = await verification.get(Transaction, fixture.transaction_id)
            assert transaction is not None
            assert (transaction.deleted_at is not None) is final_deleted
            assert transaction.version == final_version
            assert (
                await verification.scalar(
                    select(func.count(AuditEvent.id)).where(AuditEvent.user_id == fixture.owner_id)
                )
                == 1
            )
            assert (
                await verification.scalar(
                    select(func.count(HttpIdempotencyRecord.id)).where(
                        HttpIdempotencyRecord.user_id == fixture.owner_id
                    )
                )
                == 1
            )
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.asyncio
async def test_foreign_and_missing_entities_are_indistinguishable_and_unmodified() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    owner = await _setup_owner(factory)
    foreign = await _setup_owner(
        factory,
        transaction_state="active",
        review_draft=True,
    )
    assert foreign.transaction_id is not None and foreign.draft_ref is not None
    digester = HttpSecurityDigester(SECURITY_KEY)
    tokens = await _create_auth_session(factory, owner.owner_id, digester)
    service = HttpRevisionMutationService(_executor(factory, digester))

    try:
        read_errors: list[tuple[object, ...]] = []
        for draft_id in (foreign.draft_ref.draft_id, uuid7()):
            with pytest.raises(EntityNotFoundError) as caught:
                await service.draft(tokens.session_token, draft_id)
            read_errors.append(caught.value.args)
        assert read_errors[0] == read_errors[1]

        mutation_errors: list[tuple[object, ...]] = []
        for transaction_id in (foreign.transaction_id, uuid7()):
            with pytest.raises(EntityNotFoundError) as caught:
                await service.delete_transaction(
                    tokens.credentials(_opaque_token()),
                    transaction_id,
                    1,
                )
            mutation_errors.append(caught.value.args)
        assert mutation_errors[0] == mutation_errors[1]

        async with factory() as verification:
            transaction = await verification.get(Transaction, foreign.transaction_id)
            assert transaction is not None
            assert transaction.deleted_at is None and transaction.version == 1
            assert await verification.get(Draft, foreign.draft_ref.draft_id) is not None
            assert (
                await verification.scalar(
                    select(func.count(HttpIdempotencyRecord.id)).where(
                        HttpIdempotencyRecord.user_id == owner.owner_id
                    )
                )
                == 0
            )
    finally:
        await _cleanup(engine, owner)
        await _cleanup(engine, foreign)
        await engine.dispose()


@pytest.mark.asyncio
async def test_http_same_key_concurrent_confirm_returns_exact_stored_receipt() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup_owner(factory, review_draft=True)
    draft_ref = fixture.draft_ref
    assert draft_ref is not None
    digester = HttpSecurityDigester(SECURITY_KEY)
    tokens = await _create_auth_session(factory, fixture.owner_id, digester)
    app = _app(factory, digester)
    idempotency_key = _opaque_token()
    path = f"/api/v1/drafts/{draft_ref.draft_id}/confirm"

    async def confirm() -> httpx2.Response:
        async with _client(app) as client:
            return await client.post(
                path,
                headers=_mutation_headers(tokens, idempotency_key),
                json={"revision": draft_ref.revision},
            )

    try:
        first, second = await asyncio.wait_for(
            asyncio.gather(confirm(), confirm()),
            timeout=8,
        )
        assert (first.status_code, second.status_code) == (201, 201)
        assert first.json() == second.json()
        assert first.json()["result"]["kind"] == "transaction"

        async with factory() as verification:
            assert (
                await verification.scalar(
                    select(func.count(Transaction.id)).where(
                        Transaction.user_id == fixture.owner_id
                    )
                )
                == 1
            )
            assert (
                await verification.scalar(
                    select(func.count(HttpIdempotencyRecord.id)).where(
                        HttpIdempotencyRecord.user_id == fixture.owner_id,
                        HttpIdempotencyRecord.operation == "draft.confirm",
                    )
                )
                == 1
            )
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.asyncio
async def test_http_same_key_replays_semantically_identical_patch_json() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup_owner(factory, review_draft=True)
    draft_ref = fixture.draft_ref
    assert draft_ref is not None
    digester = HttpSecurityDigester(SECURITY_KEY)
    tokens = await _create_auth_session(factory, fixture.owner_id, digester)
    app = _app(factory, digester)
    headers = {
        **_mutation_headers(tokens, _opaque_token()),
        "Content-Type": "application/json",
    }
    path = f"/api/v1/drafts/{draft_ref.draft_id}"
    compact = f'{{"revision":{draft_ref.revision},"action":"edit_description"}}'
    reordered = f'{{\n  "action" : "edit_description",\n  "revision" : {draft_ref.revision}\n}}'

    try:
        async with _client(app) as client:
            first = await client.patch(path, headers=headers, content=compact)
            replay = await client.patch(path, headers=headers, content=reordered)

        assert first.status_code == replay.status_code == 200
        assert first.json() == replay.json()
        assert first.json()["result"] == {
            "draft_id": str(draft_ref.draft_id),
            "kind": "draft",
            "revision": draft_ref.revision + 1,
        }

        async with factory() as verification:
            draft = await verification.get(Draft, draft_ref.draft_id)
            assert draft is not None
            assert (draft.state, draft.revision) == (
                "wizard_description",
                draft_ref.revision + 1,
            )
            assert (
                await verification.scalar(
                    select(func.count(HttpIdempotencyRecord.id)).where(
                        HttpIdempotencyRecord.user_id == fixture.owner_id,
                        HttpIdempotencyRecord.operation == "draft.update",
                    )
                )
                == 1
            )
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.asyncio
async def test_http_different_keys_concurrent_confirm_returns_201_and_404() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup_owner(factory, review_draft=True)
    draft_ref = fixture.draft_ref
    assert draft_ref is not None
    digester = HttpSecurityDigester(SECURITY_KEY)
    tokens = await _create_auth_session(factory, fixture.owner_id, digester)
    app = _app(factory, digester)
    path = f"/api/v1/drafts/{draft_ref.draft_id}/confirm"

    async def confirm(idempotency_key: str) -> httpx2.Response:
        async with _client(app) as client:
            return await client.post(
                path,
                headers=_mutation_headers(tokens, idempotency_key),
                json={"revision": draft_ref.revision},
            )

    try:
        responses = await asyncio.wait_for(
            asyncio.gather(confirm(_opaque_token()), confirm(_opaque_token())),
            timeout=8,
        )
        assert sorted(response.status_code for response in responses) == [201, 404]
        rejected = next(response for response in responses if response.status_code == 404)
        assert rejected.json()["error"]["code"] == "not_found"

        async with factory() as verification:
            assert (
                await verification.scalar(
                    select(func.count(Transaction.id)).where(
                        Transaction.user_id == fixture.owner_id
                    )
                )
                == 1
            )
            assert (
                await verification.scalar(
                    select(func.count(HttpIdempotencyRecord.id)).where(
                        HttpIdempotencyRecord.user_id == fixture.owner_id,
                        HttpIdempotencyRecord.operation == "draft.confirm",
                    )
                )
                == 1
            )
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.asyncio
async def test_http_fingerprint_mismatch_is_typed_409_without_second_mutation() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    fixture = await _setup_owner(factory, transaction_state="active")
    transaction_id = fixture.transaction_id
    assert transaction_id is not None
    digester = HttpSecurityDigester(SECURITY_KEY)
    tokens = await _create_auth_session(factory, fixture.owner_id, digester)
    app = _app(factory, digester)
    idempotency_key = _opaque_token()
    path = f"/api/v1/transactions/{transaction_id}/delete"
    headers = _mutation_headers(tokens, idempotency_key)

    try:
        async with _client(app) as client:
            deleted = await client.post(path, headers=headers, json={"version": 1})
            mismatch = await client.post(path, headers=headers, json={"version": 2})

        assert deleted.status_code == 200
        assert mismatch.status_code == 409
        assert mismatch.json()["error"]["code"] == "idempotency_key_conflict"
        async with factory() as verification:
            transaction = await verification.get(Transaction, transaction_id)
            assert transaction is not None
            assert transaction.deleted_at is not None and transaction.version == 2
            assert (
                await verification.scalar(
                    select(func.count(AuditEvent.id)).where(AuditEvent.user_id == fixture.owner_id)
                )
                == 1
            )
    finally:
        await _cleanup(engine, fixture)
        await engine.dispose()


@pytest.mark.asyncio
async def test_http_foreign_and_missing_entities_return_identical_404_payloads() -> None:
    engine = create_async_engine(DATABASE_URL)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    owner = await _setup_owner(factory)
    foreign = await _setup_owner(
        factory,
        transaction_state="active",
        review_draft=True,
    )
    transaction_id = foreign.transaction_id
    draft_ref = foreign.draft_ref
    assert transaction_id is not None and draft_ref is not None
    digester = HttpSecurityDigester(SECURITY_KEY)
    tokens = await _create_auth_session(factory, owner.owner_id, digester)
    app = _app(factory, digester)

    try:
        async with _client(app) as client:
            foreign_draft = await client.get(
                f"/api/v1/drafts/{draft_ref.draft_id}",
                headers=_read_headers(tokens),
            )
            missing_draft = await client.get(
                f"/api/v1/drafts/{uuid7()}",
                headers=_read_headers(tokens),
            )
            foreign_transaction = await client.post(
                f"/api/v1/transactions/{transaction_id}/delete",
                headers=_mutation_headers(tokens, _opaque_token()),
                json={"version": 1},
            )
            missing_transaction = await client.post(
                f"/api/v1/transactions/{uuid7()}/delete",
                headers=_mutation_headers(tokens, _opaque_token()),
                json={"version": 1},
            )

        assert foreign_draft.status_code == missing_draft.status_code == 404
        assert foreign_draft.json() == missing_draft.json()
        assert foreign_transaction.status_code == missing_transaction.status_code == 404
        assert foreign_transaction.json() == missing_transaction.json()

        async with factory() as verification:
            transaction = await verification.get(Transaction, transaction_id)
            assert transaction is not None
            assert transaction.deleted_at is None and transaction.version == 1
            assert await verification.get(Draft, draft_ref.draft_id) is not None
            assert (
                await verification.scalar(
                    select(func.count(HttpIdempotencyRecord.id)).where(
                        HttpIdempotencyRecord.user_id == owner.owner_id
                    )
                )
                == 0
            )
    finally:
        await _cleanup(engine, owner)
        await _cleanup(engine, foreign)
        await engine.dispose()
