from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid7

import pytest

import finbot.adapters.database.repositories.http_mutations as http_mutation_repository
from finbot.adapters.database.repositories.http_idempotency import (
    IdempotencyClaim,
    IdempotencyClaimStatus,
    IdempotencyResult,
    IdempotencyResultKind,
)
from finbot.adapters.database.repositories.http_mutations import (
    SqlAlchemyHttpMutationUnitOfWork,
)
from finbot.adapters.http.auth.crypto import HttpSecurityDigester
from finbot.adapters.http.auth.ports import (
    AuthenticatedSession,
    AuthOwner,
    SessionCheck,
    SessionCheckStatus,
)
from finbot.adapters.http.mutations.service import (
    HttpMutationExecutor,
    InvalidStoredMutationResultError,
    MutationCredentials,
    MutationOperation,
    MutationReceipt,
)

NOW = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
OWNER_ID = UUID("018f0000-0000-7000-8000-000000000001")
DRAFT_ID = UUID("018f0000-0000-7000-8000-000000000002")
SECURITY_KEY = base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode("ascii")
SESSION_TOKEN = base64.urlsafe_b64encode(b"s" * 32).rstrip(b"=").decode("ascii")
CSRF_TOKEN = base64.urlsafe_b64encode(b"c" * 32).rstrip(b"=").decode("ascii")
IDEMPOTENCY_KEY = base64.urlsafe_b64encode(b"i" * 32).rstrip(b"=").decode("ascii")
CREDENTIALS = MutationCredentials(
    session_token=SESSION_TOKEN,
    csrf_cookie=CSRF_TOKEN,
    csrf_header=CSRF_TOKEN,
    idempotency_key=IDEMPOTENCY_KEY,
)


class FakeAuthPersistence:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def lock_session_for_mutation(self, *_args: object, **_kwargs: object) -> SessionCheck:
        self.events.append("session")
        return SessionCheck(
            SessionCheckStatus.ACTIVE,
            AuthenticatedSession(
                AuthOwner(OWNER_ID, "ru_RU", "Europe/Moscow", "RUB"),
                NOW + timedelta(hours=1),
            ),
        )


class FakeIdempotency:
    def __init__(
        self,
        events: list[str],
        *,
        status: IdempotencyClaimStatus,
        result: IdempotencyResult | None = None,
    ) -> None:
        self.events = events
        self.status = status
        self.result = result
        self.completed_at: datetime | None = None

    async def claim(self, *_args: object, **_kwargs: object) -> IdempotencyClaim:
        self.events.append("idempotency_claim")
        return IdempotencyClaim(uuid7(), self.status, self.result)

    async def complete(
        self,
        _owner_id: UUID,
        _claim: IdempotencyClaim,
        _result: IdempotencyResult,
        *,
        completed_at: datetime,
    ) -> None:
        self.events.append("idempotency_complete")
        self.completed_at = completed_at


class NoDomainAccess:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"stored replay must not access {name}")


class FakeMutationUow:
    def __init__(self, idempotency: FakeIdempotency, events: list[str]) -> None:
        self.auth = FakeAuthPersistence(events)
        self.idempotency = idempotency
        self.commands = NoDomainAccess()
        self.drafts = NoDomainAccess()
        self.events = events

    async def lock_owner(self, _owner_id: UUID) -> None:
        self.events.append("owner")


class FakeMutationUowFactory:
    def __init__(self, uow: FakeMutationUow, events: list[str]) -> None:
        self.uow = uow
        self.events = events

    @asynccontextmanager
    async def __call__(self) -> Any:
        try:
            yield self.uow
        finally:
            self.events.append("exit")


def _executor(
    idempotency: FakeIdempotency,
    events: list[str],
    *,
    clock: Any = lambda: NOW,
) -> HttpMutationExecutor:
    uow = FakeMutationUow(idempotency, events)
    return HttpMutationExecutor(
        digester=HttpSecurityDigester(SECURITY_KEY),
        uow_factory=FakeMutationUowFactory(uow, events),
        clock=clock,
    )


@pytest.mark.asyncio
async def test_replay_uses_only_stored_receipt_and_never_rereads_domain_state() -> None:
    events: list[str] = []
    stored = IdempotencyResult(201, IdempotencyResultKind.DRAFT, DRAFT_ID, 7)
    idempotency = FakeIdempotency(
        events,
        status=IdempotencyClaimStatus.REPLAY,
        result=stored,
    )

    async def must_not_mutate(*_args: object) -> MutationReceipt:
        raise AssertionError("stored replay must not execute the mutation")

    receipt = await _executor(idempotency, events).execute(
        CREDENTIALS,
        operation=MutationOperation.DRAFT_CREATE,
        semantic_request={},
        allowed_results=frozenset({(201, IdempotencyResultKind.DRAFT)}),
        mutate=must_not_mutate,
    )

    assert receipt == MutationReceipt(
        201,
        IdempotencyResultKind.DRAFT,
        DRAFT_ID,
        7,
        replayed=True,
    )
    assert events == ["session", "owner", "idempotency_claim", "exit"]
    assert idempotency.completed_at is None


@pytest.mark.asyncio
async def test_executor_rejects_replay_outside_the_operation_status_kind_matrix() -> None:
    events: list[str] = []
    idempotency = FakeIdempotency(
        events,
        status=IdempotencyClaimStatus.REPLAY,
        result=IdempotencyResult(200, IdempotencyResultKind.TRANSACTION, DRAFT_ID, 1),
    )

    async def must_not_mutate(*_args: object) -> MutationReceipt:
        raise AssertionError

    with pytest.raises(InvalidStoredMutationResultError):
        await _executor(idempotency, events).execute(
            CREDENTIALS,
            operation=MutationOperation.DRAFT_CREATE,
            semantic_request={},
            allowed_results=frozenset({(200, IdempotencyResultKind.DRAFT)}),
            mutate=must_not_mutate,
        )

    assert events == ["session", "owner", "idempotency_claim", "exit"]


@pytest.mark.asyncio
async def test_executor_lock_order_and_completion_use_a_fresh_clock_value() -> None:
    events: list[str] = []
    idempotency = FakeIdempotency(events, status=IdempotencyClaimStatus.NEW)
    completed = NOW + timedelta(seconds=2)
    values = iter((NOW, completed))

    async def mutate(_uow: object, _owner_id: UUID) -> MutationReceipt:
        events.append("domain")
        return MutationReceipt(200, IdempotencyResultKind.DRAFT, DRAFT_ID, 8)

    receipt = await _executor(idempotency, events, clock=lambda: next(values)).execute(
        CREDENTIALS,
        operation=MutationOperation.DRAFT_UPDATE,
        semantic_request={"action": "back", "draft_id": str(DRAFT_ID), "revision": 7},
        allowed_results=frozenset({(200, IdempotencyResultKind.DRAFT)}),
        mutate=mutate,
    )

    assert receipt.revision == 8
    assert idempotency.completed_at == completed
    assert events == [
        "session",
        "owner",
        "idempotency_claim",
        "domain",
        "idempotency_complete",
        "exit",
    ]


class FailingContext:
    def __init__(self) -> None:
        self.session = object()
        self.exit_args: tuple[object, object, object] | None = None

    async def __aenter__(self) -> object:
        return self.session

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        self.exit_args = (exc_type, exc, traceback)


class FakeSessions:
    def __init__(self, context: FailingContext) -> None:
        self.context = context

    def begin(self) -> FailingContext:
        return self.context


@pytest.mark.asyncio
async def test_mutation_uow_closes_entered_transaction_if_command_wiring_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = RuntimeError("synthetic wiring failure")
    context = FailingContext()

    def fail_wiring(_session: object) -> None:
        raise marker

    monkeypatch.setattr(
        http_mutation_repository,
        "SqlAlchemyRevisionMutationCommands",
        fail_wiring,
    )
    uow = SqlAlchemyHttpMutationUnitOfWork(cast(Any, FakeSessions(context)))

    with pytest.raises(RuntimeError) as caught:
        await uow.__aenter__()

    assert caught.value is marker
    assert context.exit_args is not None
    assert context.exit_args[0] is RuntimeError
    assert context.exit_args[1] is marker
