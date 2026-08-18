from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.executor as executor_module
from finbot.adapters.telegram.controllers.transaction_edit_text_input import (
    TelegramTransactionEditTextInputContext,
    TransactionEditTextInputController,
    TransactionEditTextInputReceiptSnapshot,
    TransactionEditTextInputSessionUseCases,
    TransactionEditTextPresentationContext,
)
from finbot.adapters.telegram.executor import TelegramMutationExecutor, TelegramMutationRequest
from finbot.application.dto import DraftRef, DraftSnapshot, OwnerSnapshot, TransactionSnapshot
from finbot.application.errors import DraftRevisionConflictError, InvalidStateError
from finbot.application.transaction_edit_text_input import (
    TransactionEditTextInputCommand,
    TransactionEditTextInputError,
    TransactionEditTextInputField,
    TransactionEditTextInputResult,
    TransactionEditTextInputStatus,
)
from finbot.application.use_cases.transaction_edit_text_input import (
    TransactionEditTextInputUseCase,
)
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000401")
TRANSACTION_ID = UUID("00000000-0000-7000-8000-000000000501")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000601")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000701")
NOW = datetime(2026, 8, 13, 9, 30, tzinfo=UTC)


@dataclass(slots=True)
class _Owner:
    id: UUID


@dataclass(frozen=True, slots=True)
class _PresentationContext:
    history_page: int | None
    pending_history_page: int | None = None


_DEFAULT_PRESENTATION = _PresentationContext(3)


class _FakeSession:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __aenter__(self) -> _FakeSession:
        self.events.append("session.enter")
        return self

    async def __aexit__(self, *args: object) -> None:
        self.events.append("session.exit")

    async def commit(self) -> None:
        self.events.append("commit")

    async def rollback(self) -> None:
        self.events.append("rollback")


class _FakeSessionFactory:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    def __call__(self) -> _FakeSession:
        return self.session


class _Targets:
    def __init__(self, events: list[str], draft: DraftSnapshot) -> None:
        self.events = events
        self.draft = draft

    async def lock_active(self, owner_id: UUID) -> DraftSnapshot:
        assert owner_id == OWNER_ID
        self.events.append("target.lock_active")
        return self.draft

    async def presentation_message_id(
        self,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
    ) -> int | None:
        assert owner_id == OWNER_ID
        assert expected == self.draft.ref
        assert chat_id == 93_000_003
        self.events.append("target.presentation")
        return 95_000_005


class _TextInput:
    def __init__(
        self,
        events: list[str],
        result: TransactionEditTextInputResult,
    ) -> None:
        self.events = events
        self.result = result
        self.commands: list[TransactionEditTextInputCommand] = []

    async def execute(
        self,
        command: TransactionEditTextInputCommand,
    ) -> TransactionEditTextInputResult:
        self.events.append("text_input.execute")
        self.commands.append(command)
        return self.result


def _request(*, update_id: int | None = 91_000_001) -> TelegramMutationRequest:
    return TelegramMutationRequest(
        update_id=update_id,
        owner_telegram_user_id=92_000_002,
        chat_id=93_000_003,
        locale="ru",
        timezone="Europe/Moscow",
        currency="RUB",
    )


def _owner_snapshot() -> OwnerSnapshot:
    return OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", ACCOUNT_ID)


def _transaction(*, version: int = 5) -> TransactionSnapshot:
    return TransactionSnapshot(
        transaction_id=TRANSACTION_ID,
        kind=TransactionType.EXPENSE,
        amount_minor=22_222,
        currency="RUB",
        account_id=ACCOUNT_ID,
        account_name="Закрытый счёт",
        category_id=CATEGORY_ID,
        category_name="Закрытая категория",
        category_emoji="▫️",
        occurred_at=NOW,
        description="закрытое описание",
        version=version,
    )


def _active(*, revision: int = 8) -> DraftSnapshot:
    return DraftSnapshot(
        DRAFT_ID,
        "edit_amount",
        {"transaction_id": str(TRANSACTION_ID), "version": 4},
        revision=revision,
    )


def _result(*, retry: bool) -> TransactionEditTextInputResult:
    if retry:
        return TransactionEditTextInputResult(
            TransactionEditTextInputStatus.RETRY,
            TransactionEditTextInputField.AMOUNT,
            _owner_snapshot(),
            _transaction(version=4),
            _active(revision=9),
            TransactionEditTextInputError.INVALID_AMOUNT,
        )
    return TransactionEditTextInputResult(
        TransactionEditTextInputStatus.UPDATED,
        TransactionEditTextInputField.AMOUNT,
        _owner_snapshot(),
        _transaction(),
    )


def _controller(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    text_input: _TextInput,
    receipts: list[TransactionEditTextInputReceiptSnapshot],
    *,
    claimed: bool = True,
    presentation: _PresentationContext | None = _DEFAULT_PRESENTATION,
    enqueue_error: Exception | None = None,
) -> TransactionEditTextInputController:
    session = _FakeSession(events)
    targets = _Targets(events, _active())

    async def claim(actual_session: AsyncSession, update_id: int | None) -> bool:
        assert actual_session is cast(Any, session)
        assert update_id in {None, 91_000_001}
        events.append("claim")
        return claimed

    async def ensure_owner(
        actual_session: AsyncSession,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        locale: str,
        timezone: str,
        currency: str,
    ) -> _Owner:
        assert actual_session is cast(Any, session)
        assert (telegram_user_id, telegram_chat_id) == (92_000_002, 93_000_003)
        assert (locale, timezone, currency) == ("ru", "Europe/Moscow", "RUB")
        events.append("ensure_owner")
        return _Owner(OWNER_ID)

    monkeypatch.setattr(executor_module, "claim_update", claim)
    monkeypatch.setattr(executor_module, "ensure_owner_user", ensure_owner)
    sessions = cast(async_sessionmaker[AsyncSession], _FakeSessionFactory(session))
    expected_session = session

    def target_factory(actual_session: AsyncSession) -> _Targets:
        assert actual_session is cast(Any, session)
        events.append("target_factory")
        return targets

    async def read_presentation(
        session: AsyncSession,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
        message_id: int,
        *,
        allow_suspended: bool = False,
    ) -> TransactionEditTextPresentationContext | None:
        assert session is cast(Any, expected_session)
        assert owner_id == OWNER_ID
        assert expected == _active().ref
        assert (chat_id, message_id) == (93_000_003, 95_000_005)
        assert allow_suspended is False
        events.append("presentation.guard")
        return presentation

    def use_case_factory(
        actual_session: AsyncSession,
    ) -> TransactionEditTextInputSessionUseCases:
        assert actual_session is cast(Any, session)
        events.append("use_case_factory")
        return TransactionEditTextInputSessionUseCases(
            cast(TransactionEditTextInputUseCase, text_input)
        )

    async def enqueue(
        actual_session: AsyncSession,
        request: TelegramMutationRequest,
        receipt: TransactionEditTextInputReceiptSnapshot,
    ) -> None:
        assert actual_session is cast(Any, session)
        assert request.update_id == 91_000_001
        events.append("outbox")
        if enqueue_error is not None:
            raise enqueue_error
        receipts.append(receipt)

    return TransactionEditTextInputController(
        TelegramMutationExecutor(sessions),
        use_case_factory,
        target_factory,
        read_presentation,
        enqueue,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", [False, True])
async def test_controller_guards_exact_projection_and_enqueues_receipt_before_commit(
    monkeypatch: pytest.MonkeyPatch,
    retry: bool,
) -> None:
    events: list[str] = []
    receipts: list[TransactionEditTextInputReceiptSnapshot] = []
    text_input = _TextInput(events, _result(retry=retry))
    controller = _controller(monkeypatch, events, text_input, receipts)

    receipt = await controller.submit(
        TelegramTransactionEditTextInputContext(_request(), 94_000_004),
        "222,22",
    )

    assert receipt is receipts[0]
    assert receipt.input_message_id == 94_000_004
    assert receipt.target_message_id == 95_000_005
    assert receipt.history_page == 3
    assert receipt.draft_ref == (_active(revision=9).ref if retry else None)
    assert text_input.commands == [
        TransactionEditTextInputCommand(OWNER_ID, _active().ref, "222,22")
    ]
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "target_factory",
        "target.lock_active",
        "target.presentation",
        "presentation.guard",
        "use_case_factory",
        "text_input.execute",
        "outbox",
        "commit",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_legacy_projection_context_uses_bounded_page_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TransactionEditTextInputReceiptSnapshot] = []
    controller = _controller(
        monkeypatch,
        events,
        _TextInput(events, _result(retry=False)),
        receipts,
        presentation=_PresentationContext(None),
    )

    receipt = await controller.submit(
        TelegramTransactionEditTextInputContext(_request(), 94_000_004),
        "222,22",
    )

    assert receipt is not None
    assert receipt.history_page == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("presentation", "error_type"),
    [
        (None, DraftRevisionConflictError),
        (_PresentationContext(3, 7), InvalidStateError),
    ],
)
async def test_missing_or_conflicted_projection_rolls_back_before_use_case(
    monkeypatch: pytest.MonkeyPatch,
    presentation: _PresentationContext | None,
    error_type: type[Exception],
) -> None:
    events: list[str] = []
    receipts: list[TransactionEditTextInputReceiptSnapshot] = []
    text_input = _TextInput(events, _result(retry=False))
    controller = _controller(
        monkeypatch,
        events,
        text_input,
        receipts,
        presentation=presentation,
    )

    with pytest.raises(error_type):
        await controller.submit(
            TelegramTransactionEditTextInputContext(_request(), 94_000_004),
            "222,22",
        )

    assert text_input.commands == []
    assert receipts == []
    assert "outbox" not in events
    assert "commit" not in events
    assert events[-2:] == ["rollback", "session.exit"]


@pytest.mark.asyncio
async def test_outbox_failure_rolls_back_and_input_is_not_deleted_by_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    controller = _controller(
        monkeypatch,
        events,
        _TextInput(events, _result(retry=False)),
        [],
        enqueue_error=RuntimeError("synthetic outbox failure"),
    )

    with pytest.raises(RuntimeError, match="synthetic outbox failure"):
        await controller.submit(
            TelegramTransactionEditTextInputContext(_request(), 94_000_004),
            "222,22",
        )

    assert "commit" not in events
    assert events[-2:] == ["rollback", "session.exit"]


@pytest.mark.asyncio
async def test_duplicate_returns_none_before_target_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    controller = _controller(
        monkeypatch,
        events,
        _TextInput(events, _result(retry=False)),
        [],
        claimed=False,
    )

    receipt = await controller.submit(
        TelegramTransactionEditTextInputContext(_request(), 94_000_004),
        "222,22",
    )

    assert receipt is None
    assert events == ["session.enter", "claim", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_untracked_update_returns_receipt_after_commit_without_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[TransactionEditTextInputReceiptSnapshot] = []
    controller = _controller(
        monkeypatch,
        events,
        _TextInput(events, _result(retry=True)),
        receipts,
    )

    receipt = await controller.submit(
        TelegramTransactionEditTextInputContext(_request(update_id=None), 94_000_004),
        "не сумма",
    )

    assert receipt is not None
    assert receipt.draft_ref == _active(revision=9).ref
    assert receipt.history_page == 3
    assert receipts == []
    assert "outbox" not in events
    assert events[-2:] == ["commit", "session.exit"]


def test_controller_contracts_hide_ids_and_financial_values_from_repr() -> None:
    context = TelegramTransactionEditTextInputContext(_request(), 94_000_004)
    receipt = TransactionEditTextInputReceiptSnapshot(
        _result(retry=False),
        94_000_004,
        95_000_005,
        3,
    )

    with pytest.raises(TypeError):
        TelegramTransactionEditTextInputContext(_request(), True)
    with pytest.raises(ValueError):
        TransactionEditTextInputReceiptSnapshot(_result(retry=False), 1, 2, -1)

    rendered = repr((context, receipt))
    for private in (
        str(OWNER_ID),
        str(DRAFT_ID),
        str(TRANSACTION_ID),
        "22222",
        "RUB",
        "Закрытый",
        "91000001",
        "92000002",
        "93000003",
        "94000004",
        "95000005",
    ):
        assert private not in rendered
