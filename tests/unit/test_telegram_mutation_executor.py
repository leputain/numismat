from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.executor as executor_module
from finbot.adapters.telegram.executor import (
    DuplicateTelegramMutation,
    SuccessfulTelegramMutation,
    TelegramMutationExecutor,
    TelegramMutationRequest,
)

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")


@dataclass(slots=True)
class _Owner:
    id: UUID


@dataclass(slots=True)
class _SensitiveResult:
    description: str
    amount_minor: int


class _FakeSession:
    def __init__(self, events: list[str], *, commit_error: Exception | None = None) -> None:
        self.events = events
        self.commit_error = commit_error

    async def __aenter__(self) -> _FakeSession:
        self.events.append("session.enter")
        return self

    async def __aexit__(self, *args: object) -> None:
        self.events.append("session.exit")

    async def commit(self) -> None:
        self.events.append("commit")
        if self.commit_error is not None:
            raise self.commit_error

    async def rollback(self) -> None:
        self.events.append("rollback")


class _FakeSessionFactory:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self.calls = 0

    def __call__(self) -> _FakeSession:
        self.calls += 1
        return self.session


def _request() -> TelegramMutationRequest:
    return TelegramMutationRequest(
        update_id=91_000_001,
        owner_telegram_user_id=92_000_002,
        chat_id=93_000_003,
        locale="ru",
        timezone="Europe/Moscow",
        currency="RUB",
    )


def _executor(factory: _FakeSessionFactory) -> TelegramMutationExecutor:
    return TelegramMutationExecutor(cast(async_sessionmaker[AsyncSession], factory))


def _patch_database_steps(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    claimed: bool = True,
    expected_update_id: int | None = 91_000_001,
) -> None:
    async def claim(session: AsyncSession, update_id: int | None) -> bool:
        assert session is cast(Any, fake_session)
        assert update_id == expected_update_id
        events.append("claim")
        return claimed

    async def ensure(
        session: AsyncSession,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        locale: str,
        timezone: str,
        currency: str,
    ) -> _Owner:
        assert session is cast(Any, fake_session)
        assert (telegram_user_id, telegram_chat_id) == (92_000_002, 93_000_003)
        assert (locale, timezone, currency) == ("ru", "Europe/Moscow", "RUB")
        events.append("ensure_owner")
        return _Owner(OWNER_ID)

    fake_session = cast(_FakeSession, _CURRENT_FAKE_SESSION)
    monkeypatch.setattr(executor_module, "claim_update", claim)
    monkeypatch.setattr(executor_module, "ensure_owner_user", ensure)


_CURRENT_FAKE_SESSION: _FakeSession


@pytest.mark.asyncio
async def test_executor_commits_claim_owner_mutation_and_receipt_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    global _CURRENT_FAKE_SESSION
    _CURRENT_FAKE_SESSION = _FakeSession(events)
    factory = _FakeSessionFactory(_CURRENT_FAKE_SESSION)
    _patch_database_steps(monkeypatch, events)
    expected = _SensitiveResult("секретное описание", 12_345)

    async def mutate(session: AsyncSession, owner_id: UUID) -> _SensitiveResult:
        assert session is cast(Any, _CURRENT_FAKE_SESSION)
        assert owner_id == OWNER_ID
        events.append("mutate")
        return expected

    async def receipt(
        session: AsyncSession,
        request: TelegramMutationRequest,
        receipt_value: str,
    ) -> None:
        assert session is cast(Any, _CURRENT_FAKE_SESSION)
        assert request is mutation_request
        assert receipt_value == "private-receipt"
        events.append("enqueue_receipt")

    def build_receipt(value: _SensitiveResult) -> str:
        assert value is expected
        events.append("build_receipt")
        return "private-receipt"

    mutation_request = _request()
    result = await _executor(factory).execute(
        mutation_request,
        mutate=mutate,
        build_receipt=build_receipt,
        enqueue_receipt=receipt,
    )

    assert isinstance(result, SuccessfulTelegramMutation)
    assert result.value is expected
    assert result.receipt == "private-receipt"
    assert factory.calls == 1
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "mutate",
        "build_receipt",
        "enqueue_receipt",
        "commit",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_duplicate_rolls_back_without_owner_mutation_or_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    global _CURRENT_FAKE_SESSION
    _CURRENT_FAKE_SESSION = _FakeSession(events)
    factory = _FakeSessionFactory(_CURRENT_FAKE_SESSION)
    _patch_database_steps(monkeypatch, events, claimed=False)

    async def forbidden_mutation(_session: AsyncSession, _owner_id: UUID) -> None:
        raise AssertionError("duplicate update reached mutation")

    async def forbidden_receipt(
        _session: AsyncSession,
        _request: TelegramMutationRequest,
        _value: None,
    ) -> None:
        raise AssertionError("duplicate update reached receipt")

    def forbidden_builder(_value: None) -> None:
        raise AssertionError("duplicate update reached receipt builder")

    result = await _executor(factory).execute(
        _request(),
        mutate=forbidden_mutation,
        build_receipt=forbidden_builder,
        enqueue_receipt=forbidden_receipt,
    )

    assert isinstance(result, DuplicateTelegramMutation)
    assert events == ["session.enter", "claim", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_untracked_library_invocation_commits_and_returns_post_commit_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    global _CURRENT_FAKE_SESSION
    _CURRENT_FAKE_SESSION = _FakeSession(events)
    factory = _FakeSessionFactory(_CURRENT_FAKE_SESSION)
    _patch_database_steps(monkeypatch, events, expected_update_id=None)
    request = TelegramMutationRequest(
        update_id=None,
        owner_telegram_user_id=92_000_002,
        chat_id=93_000_003,
        locale="ru",
        timezone="Europe/Moscow",
        currency="RUB",
    )

    async def mutate(_session: AsyncSession, owner_id: UUID) -> str:
        assert owner_id == OWNER_ID
        events.append("mutate")
        return "private-result"

    def build_receipt(value: str) -> str:
        assert value == "private-result"
        events.append("build_receipt")
        return "post-commit-receipt"

    async def forbidden_enqueue(
        _session: AsyncSession,
        _request: TelegramMutationRequest,
        _receipt: str,
    ) -> None:
        raise AssertionError("untracked invocation queued a NULL update outbox row")

    result = await _executor(factory).execute(
        request,
        mutate=mutate,
        build_receipt=build_receipt,
        enqueue_receipt=forbidden_enqueue,
    )

    assert isinstance(result, SuccessfulTelegramMutation)
    assert (result.value, result.receipt) == ("private-result", "post-commit-receipt")
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "mutate",
        "build_receipt",
        "commit",
        "session.exit",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["mutation", "build", "receipt", "commit"])
async def test_executor_rolls_back_every_failure_after_claim(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    events: list[str] = []
    failure = RuntimeError(f"{failure_stage} failed")
    global _CURRENT_FAKE_SESSION
    _CURRENT_FAKE_SESSION = _FakeSession(
        events,
        commit_error=failure if failure_stage == "commit" else None,
    )
    factory = _FakeSessionFactory(_CURRENT_FAKE_SESSION)
    _patch_database_steps(monkeypatch, events)

    async def mutate(_session: AsyncSession, _owner_id: UUID) -> str:
        events.append("mutate")
        if failure_stage == "mutation":
            raise failure
        return "private-result"

    async def receipt(
        _session: AsyncSession,
        _request: TelegramMutationRequest,
        _value: str,
    ) -> None:
        events.append("enqueue_receipt")
        if failure_stage == "receipt":
            raise failure

    def build_receipt(value: str) -> str:
        assert value == "private-result"
        events.append("build_receipt")
        if failure_stage == "build":
            raise failure
        return "private-receipt"

    with pytest.raises(RuntimeError, match=failure_stage):
        await _executor(factory).execute(
            _request(),
            mutate=mutate,
            build_receipt=build_receipt,
            enqueue_receipt=receipt,
        )

    assert events[-2:] == ["rollback", "session.exit"]
    assert events.count("commit") == (1 if failure_stage == "commit" else 0)
    if failure_stage == "mutation":
        assert "build_receipt" not in events
        assert "enqueue_receipt" not in events
    if failure_stage == "build":
        assert "enqueue_receipt" not in events


def test_executor_contract_hides_all_private_values_from_repr() -> None:
    request = _request()
    value = _SensitiveResult("тайное описание", 98_765)
    success = SuccessfulTelegramMutation(value, "секретная квитанция")

    assert repr(request) == "TelegramMutationRequest()"
    assert repr(success) == "SuccessfulTelegramMutation()"
    for rendered in (repr(request), repr(success)):
        assert "91_000_001" not in rendered
        assert "92000002" not in rendered
        assert "93000003" not in rendered
        assert "98_765" not in rendered
        assert "тайн" not in rendered
        assert "квитанц" not in rendered


@pytest.mark.parametrize(
    ("field_name", "value", "error"),
    [
        ("update_id", 0, ValueError),
        ("update_id", True, TypeError),
        ("owner_telegram_user_id", -1, ValueError),
        ("chat_id", False, TypeError),
    ],
)
def test_request_rejects_invalid_telegram_identifiers(
    field_name: str,
    value: object,
    error: type[Exception],
) -> None:
    values: dict[str, object] = {
        "update_id": 1,
        "owner_telegram_user_id": 2,
        "chat_id": 3,
        "locale": "ru",
        "timezone": "Europe/Moscow",
        "currency": "RUB",
    }
    values[field_name] = value
    with pytest.raises(error):
        TelegramMutationRequest(**values)  # type: ignore[arg-type]
