from dataclasses import dataclass, replace
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.executor as executor_module
from finbot.adapters.telegram.controllers.settings_mutations import (
    InvalidSettingsMutationCallback,
    SettingsMutationController,
    SettingsMutationSessionUseCases,
    StaleSettingsTimezoneCallback,
    TelegramSettingsMutationContext,
    parse_settings_timezone_callback,
)
from finbot.adapters.telegram.controllers.settings_queries import SettingsCatalogTarget
from finbot.adapters.telegram.executor import TelegramMutationExecutor, TelegramMutationRequest
from finbot.application.dto import (
    AccountSnapshot,
    DraftRef,
    DraftSnapshot,
    OwnerSnapshot,
)
from finbot.application.settings_mutations import (
    BeginAccountCreateCommand,
    BeginAccountRenameCommand,
    ChangeTimezoneCommand,
    SettingsInputIngressOperation,
    SettingsInputIngressResult,
)
from finbot.application.use_cases.settings_mutations import (
    BeginSettingsInput,
    ChangeSettingsTimezone,
)

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000201")
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


class _Sessions:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    def __call__(self) -> _FakeSession:
        return self.session


def _owner_snapshot(
    timezone: str = "Europe/Moscow",
    *,
    settings_version: int = 1,
) -> OwnerSnapshot:
    return OwnerSnapshot(
        OWNER_ID,
        "ru",
        timezone,
        "RUB",
        ACCOUNT_ID,
        settings_version=settings_version,
    )


def _account() -> AccountSnapshot:
    return AccountSnapshot(ACCOUNT_ID, "Карта <основная>", "card", "RUB", None, 4)


def _result(*, rename: bool) -> SettingsInputIngressResult:
    if rename:
        return SettingsInputIngressResult(
            SettingsInputIngressOperation.ACCOUNT_RENAME,
            _owner_snapshot(),
            DraftSnapshot(
                DRAFT_ID,
                "settings_account_rename",
                {"account_id": str(ACCOUNT_ID), "object_version": 4},
            ),
            account=_account(),
        )
    return SettingsInputIngressResult(
        SettingsInputIngressOperation.ACCOUNT_CREATE,
        _owner_snapshot(),
        DraftSnapshot(DRAFT_ID, "settings_account_create", {}),
    )


class _Begin:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.result = _result(rename=True)
        self.commands: list[object] = []

    async def account_create(
        self,
        command: BeginAccountCreateCommand,
    ) -> SettingsInputIngressResult:
        self.events.append("begin.account_create")
        self.commands.append(command)
        return _result(rename=False)

    async def account_rename(
        self,
        command: BeginAccountRenameCommand,
    ) -> SettingsInputIngressResult:
        self.events.append("begin.account_rename")
        self.commands.append(command)
        return self.result


class _Timezone:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.commands: list[ChangeTimezoneCommand] = []

    async def execute(self, command: ChangeTimezoneCommand) -> OwnerSnapshot:
        self.events.append("timezone.execute")
        self.commands.append(command)
        return replace(
            _owner_snapshot(),
            timezone=command.timezone,
            settings_version=command.expected_version + 1,
        )


class _Reader:
    def __init__(self, events: list[str], timezone: str = "Europe/Moscow") -> None:
        self.events = events
        self.owner = _owner_snapshot(timezone)

    async def get_owner(self, owner_id: UUID) -> OwnerSnapshot | None:
        assert owner_id == OWNER_ID
        self.events.append("query.owner")
        return self.owner

    async def list_accounts(
        self,
        owner_id: UUID,
        *,
        archived: bool = False,
    ) -> tuple[AccountSnapshot, ...]:
        assert owner_id == OWNER_ID and not archived
        self.events.append("query.accounts")
        return (_account(),)

    async def list_categories(
        self,
        owner_id: UUID,
        *,
        kind: str | None = None,
        archived: bool = False,
    ) -> tuple[()]:
        del owner_id, kind, archived
        return ()


class _Drafts:
    def __init__(self, events: list[str], active: DraftSnapshot | None = None) -> None:
        self.events = events
        self.active = active

    async def get_active(self, owner_id: UUID) -> DraftSnapshot | None:
        assert owner_id == OWNER_ID
        self.events.append("draft.get")
        return self.active

    async def suspend(self, owner_id: UUID, expected: DraftRef) -> DraftSnapshot:
        del owner_id, expected
        raise AssertionError("suspend is not used by settings mutations")


def _request(update_id: int | None = 91_000_001) -> TelegramMutationRequest:
    return TelegramMutationRequest(
        update_id=update_id,
        owner_telegram_user_id=92_000_002,
        chat_id=93_000_003,
        locale="ru",
        timezone="Europe/Moscow",
        currency="RUB",
    )


def _controller(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    begin: _Begin,
    timezone: _Timezone,
    reader: _Reader,
    drafts: _Drafts,
    receipts: list[object],
    *,
    claimed: bool = True,
    enqueue_error: Exception | None = None,
) -> SettingsMutationController:
    session = _FakeSession(events)

    async def claim(actual_session: AsyncSession, update_id: int | None) -> bool:
        assert actual_session is cast(Any, session)
        assert update_id in {None, 91_000_001}
        events.append("claim")
        return claimed

    async def ensure_owner(
        actual_session: AsyncSession,
        **kwargs: object,
    ) -> _Owner:
        assert actual_session is cast(Any, session)
        assert kwargs["telegram_user_id"] == 92_000_002
        events.append("ensure_owner")
        return _Owner(OWNER_ID)

    monkeypatch.setattr(executor_module, "claim_update", claim)
    monkeypatch.setattr(executor_module, "ensure_owner_user", ensure_owner)

    def use_cases(actual_session: AsyncSession) -> SettingsMutationSessionUseCases:
        assert actual_session is cast(Any, session)
        events.append("use_cases")
        return SettingsMutationSessionUseCases(
            cast(BeginSettingsInput, begin),
            cast(ChangeSettingsTimezone, timezone),
        )

    def readers(actual_session: AsyncSession) -> _Reader:
        assert actual_session is cast(Any, session)
        events.append("reader")
        return reader

    def draft_factory(actual_session: AsyncSession) -> _Drafts:
        assert actual_session is cast(Any, session)
        events.append("drafts")
        return drafts

    async def enqueue(
        actual_session: AsyncSession,
        request: TelegramMutationRequest,
        receipt: object,
    ) -> None:
        assert actual_session is cast(Any, session)
        assert request.update_id == 91_000_001
        events.append("outbox")
        if enqueue_error is not None:
            raise enqueue_error
        receipts.append(receipt)

    return SettingsMutationController(
        TelegramMutationExecutor(cast(async_sessionmaker[AsyncSession], _Sessions(session))),
        use_cases,
        readers,
        draft_factory,
        enqueue,
    )


@pytest.mark.asyncio
async def test_rename_claims_mutates_and_enqueues_exact_receipt_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[object] = []
    begin = _Begin(events)
    controller = _controller(
        monkeypatch,
        events,
        begin,
        _Timezone(events),
        _Reader(events),
        _Drafts(events),
        receipts,
    )

    receipt = await controller.account_rename(
        TelegramSettingsMutationContext(_request(), 94_000_004),
        SettingsCatalogTarget(ACCOUNT_ID, 4),
    )

    assert receipt is receipts[0]
    assert receipt is not None
    assert receipt.message_id == 94_000_004
    assert receipt.draft_ref == DraftRef(DRAFT_ID, 1)
    assert "Карта &lt;основная&gt;" in receipt.text
    assert "ui_message_id" not in begin.result.draft.payload
    assert begin.commands == [BeginAccountRenameCommand(OWNER_ID, ACCOUNT_ID, 4)]
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "use_cases",
        "begin.account_rename",
        "outbox",
        "commit",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_outbox_failure_rolls_back_ingress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    controller = _controller(
        monkeypatch,
        events,
        _Begin(events),
        _Timezone(events),
        _Reader(events),
        _Drafts(events),
        [],
        enqueue_error=RuntimeError("synthetic outbox failure"),
    )

    with pytest.raises(RuntimeError, match="synthetic outbox failure"):
        await controller.account_create(TelegramSettingsMutationContext(_request(), 94_000_004))

    assert "commit" not in events
    assert events[-2:] == ["rollback", "session.exit"]


@pytest.mark.asyncio
async def test_untracked_ingress_returns_only_after_commit_without_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    controller = _controller(
        monkeypatch,
        events,
        _Begin(events),
        _Timezone(events),
        _Reader(events),
        _Drafts(events),
        [],
    )

    receipt = await controller.account_create(
        TelegramSettingsMutationContext(_request(None), 94_000_004)
    )

    assert receipt is not None and receipt.draft_ref == DraftRef(DRAFT_ID, 1)
    assert "outbox" not in events
    assert events[-2:] == ["commit", "session.exit"]


@pytest.mark.asyncio
async def test_timezone_mutation_preserves_exact_active_draft_in_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[object] = []
    active = DraftSnapshot(
        DRAFT_ID,
        "wizard_type",
        {"flow": "wizard"},
        revision=8,
        suspended=True,
    )
    timezone = _Timezone(events)
    controller = _controller(
        monkeypatch,
        events,
        _Begin(events),
        timezone,
        _Reader(events),
        _Drafts(events, active),
        receipts,
    )

    receipt = await controller.timezone(
        TelegramSettingsMutationContext(_request(), 94_000_004),
        parse_settings_timezone_callback("s:timezone:3:1"),
    )

    assert receipt is receipts[0]
    assert receipt is not None
    assert receipt.draft_ref == DraftRef(DRAFT_ID, 8)
    assert "Asia/Yekaterinburg" in receipt.text
    assert timezone.commands == [ChangeTimezoneCommand(OWNER_ID, 1, "Asia/Yekaterinburg")]
    assert events.index("outbox") < events.index("commit")


@pytest.mark.asyncio
async def test_stale_timezone_version_rolls_back_without_mutation_or_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    timezone = _Timezone(events)
    controller = _controller(
        monkeypatch,
        events,
        _Begin(events),
        timezone,
        _Reader(events),
        _Drafts(events),
        [],
    )

    with pytest.raises(StaleSettingsTimezoneCallback):
        await controller.timezone(
            TelegramSettingsMutationContext(_request(), 94_000_004),
            parse_settings_timezone_callback("s:timezone:3:2"),
        )

    assert timezone.commands == []
    assert "outbox" not in events
    assert events[-2:] == ["rollback", "session.exit"]


@pytest.mark.parametrize(
    "data",
    [
        "",
        "s:timezone:",
        "s:timezone:-1:1",
        "s:timezone:99:1",
        "s:timezone:1:0",
        "s:timezone:1:01",
        "s:timezone:1:2147483648",
    ],
)
def test_timezone_parser_rejects_malformed_callbacks(data: str) -> None:
    with pytest.raises(InvalidSettingsMutationCallback):
        parse_settings_timezone_callback(data)
