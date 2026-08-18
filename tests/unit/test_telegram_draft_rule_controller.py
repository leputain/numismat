from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.executor as executor_module
from finbot.adapters.telegram.controllers.draft_rules import (
    DraftRuleController,
    DraftRuleReceiptSnapshot,
    DraftRuleSessionUseCases,
    TelegramDraftRuleContext,
)
from finbot.adapters.telegram.executor import TelegramMutationExecutor, TelegramMutationRequest
from finbot.application.draft_rules import (
    DraftRuleAction,
    DraftRuleStagingResult,
    StageDraftRuleCommand,
)
from finbot.application.dto import DraftRef, DraftSnapshot, OwnerSnapshot
from finbot.application.errors import DraftRevisionConflictError
from finbot.application.use_cases.draft_rules import DraftRuleUseCases
from finbot.application.use_cases.queries import GetOwnerSettings

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000401")


@dataclass(slots=True)
class _Owner:
    id: UUID


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


class _Rules:
    def __init__(self, events: list[str], result: DraftRuleStagingResult) -> None:
        self.events = events
        self.result = result
        self.commands: list[StageDraftRuleCommand] = []

    async def execute(self, command: StageDraftRuleCommand) -> DraftRuleStagingResult:
        self.events.append("rules.execute")
        self.commands.append(command)
        return self.result


class _OwnerSettings:
    def __init__(self, events: list[str], owner: OwnerSnapshot) -> None:
        self.events = events
        self.owner = owner

    async def __call__(self, owner_id: UUID) -> OwnerSnapshot:
        assert owner_id == OWNER_ID
        self.events.append("owner.settings")
        return self.owner


def _request(*, update_id: int | None = 91_000_001) -> TelegramMutationRequest:
    return TelegramMutationRequest(
        update_id=update_id,
        owner_telegram_user_id=92_000_002,
        chat_id=93_000_003,
        locale="ru",
        timezone="Europe/Moscow",
        currency="RUB",
    )


def _owner() -> OwnerSnapshot:
    return OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", None)


def _result(action: DraftRuleAction = DraftRuleAction.ACCOUNT) -> DraftRuleStagingResult:
    return DraftRuleStagingResult(
        action,
        DraftSnapshot(
            DRAFT_ID,
            "quick_confirm",
            {
                "amount_minor": 12_345,
                "description": "закрытое описание",
                "pending_rule": {"scope": action.value},
            },
            revision=8,
        ),
    )


def _controller(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    rules: _Rules,
    receipts: list[DraftRuleReceiptSnapshot],
    *,
    claimed: bool = True,
    presentation_is_current: bool = True,
) -> DraftRuleController:
    session = _FakeSession(events)

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

    async def guard(
        actual_session: AsyncSession,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
        message_id: int,
    ) -> bool:
        assert actual_session is cast(Any, session)
        assert owner_id == OWNER_ID
        assert expected == DraftRef(DRAFT_ID, 7)
        assert (chat_id, message_id) == (93_000_003, 94_000_004)
        events.append("presentation.guard")
        return presentation_is_current

    owner_settings = _OwnerSettings(events, _owner())

    def use_case_factory(actual_session: AsyncSession) -> DraftRuleSessionUseCases:
        assert actual_session is cast(Any, session)
        events.append("use_case_factory")
        return DraftRuleSessionUseCases(
            cast(DraftRuleUseCases, rules),
            cast(GetOwnerSettings, owner_settings),
        )

    async def enqueue(
        actual_session: AsyncSession,
        request: TelegramMutationRequest,
        receipt: DraftRuleReceiptSnapshot,
    ) -> None:
        assert actual_session is cast(Any, session)
        assert request.update_id == 91_000_001
        events.append("outbox")
        receipts.append(receipt)

    return DraftRuleController(
        TelegramMutationExecutor(sessions),
        use_case_factory,
        guard,
        enqueue,
    )


@pytest.mark.asyncio
async def test_controller_guards_mutates_builds_receipt_and_enqueues_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftRuleReceiptSnapshot] = []
    rules = _Rules(events, _result())
    controller = _controller(monkeypatch, events, rules, receipts)
    expected = DraftRef(DRAFT_ID, 7)

    receipt = await controller.execute(
        TelegramDraftRuleContext(_request(), 94_000_004),
        expected,
        DraftRuleAction.ACCOUNT,
    )

    assert receipt is receipts[0]
    assert receipt.action is DraftRuleAction.ACCOUNT
    assert receipt.expected is expected
    assert receipt.owner == _owner()
    assert receipt.draft is rules.result.draft
    assert receipt.message_id == 94_000_004
    assert rules.commands == [StageDraftRuleCommand(OWNER_ID, expected, DraftRuleAction.ACCOUNT)]
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "presentation.guard",
        "use_case_factory",
        "rules.execute",
        "owner.settings",
        "outbox",
        "commit",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_stale_message_binding_rolls_back_without_mutation_or_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftRuleReceiptSnapshot] = []
    rules = _Rules(events, _result())
    controller = _controller(
        monkeypatch,
        events,
        rules,
        receipts,
        presentation_is_current=False,
    )

    with pytest.raises(DraftRevisionConflictError):
        await controller.execute(
            TelegramDraftRuleContext(_request(), 94_000_004),
            DraftRef(DRAFT_ID, 7),
            DraftRuleAction.GLOBAL,
        )

    assert rules.commands == []
    assert receipts == []
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "presentation.guard",
        "rollback",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_duplicate_returns_none_without_guard_or_use_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftRuleReceiptSnapshot] = []
    rules = _Rules(events, _result())
    controller = _controller(monkeypatch, events, rules, receipts, claimed=False)

    receipt = await controller.execute(
        TelegramDraftRuleContext(_request(), 94_000_004),
        DraftRef(DRAFT_ID, 7),
        DraftRuleAction.ACCOUNT,
    )

    assert receipt is None
    assert rules.commands == []
    assert receipts == []
    assert events == ["session.enter", "claim", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_untracked_update_returns_receipt_only_after_commit_without_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[DraftRuleReceiptSnapshot] = []
    rules = _Rules(events, _result(DraftRuleAction.REMOVE))
    controller = _controller(monkeypatch, events, rules, receipts)

    receipt = await controller.execute(
        TelegramDraftRuleContext(_request(update_id=None), 94_000_004),
        DraftRef(DRAFT_ID, 7),
        DraftRuleAction.REMOVE,
    )

    assert isinstance(receipt, DraftRuleReceiptSnapshot)
    assert receipt.action is DraftRuleAction.REMOVE
    assert receipts == []
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "presentation.guard",
        "use_case_factory",
        "rules.execute",
        "owner.settings",
        "commit",
        "session.exit",
    ]


def test_controller_contracts_hide_all_private_values_from_repr() -> None:
    result = _result()
    receipt = DraftRuleReceiptSnapshot(
        DraftRuleAction.ACCOUNT,
        DraftRef(DRAFT_ID, 7),
        _owner(),
        result.draft,
        94_000_004,
    )
    values = (
        TelegramDraftRuleContext(_request(), 94_000_004),
        receipt,
        DraftRuleSessionUseCases(
            cast(DraftRuleUseCases, object()),
            cast(GetOwnerSettings, object()),
        ),
    )

    rendered = repr(values)
    for private in (
        str(OWNER_ID),
        str(DRAFT_ID),
        "RUB",
        "12345",
        "закрытое",
        "91000001",
        "92000002",
        "93000003",
        "94000004",
    ):
        assert private not in rendered


@pytest.mark.parametrize("message_id", [True, False, 0, -1])
def test_context_rejects_invalid_message_id(message_id: int) -> None:
    with pytest.raises(TypeError if isinstance(message_id, bool) else ValueError):
        TelegramDraftRuleContext(_request(), message_id)
