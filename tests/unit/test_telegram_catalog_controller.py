import ast
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import finbot.adapters.telegram.executor as executor_module
from finbot.adapters.telegram.controllers.catalogs import (
    AccountCatalogReceiptSnapshot,
    CatalogController,
    CatalogOperation,
    CatalogReceiptSnapshot,
    CatalogSessionUseCases,
    CategoryCatalogReceiptSnapshot,
    CreateAccountInput,
    CreateCategoryInput,
    TelegramCatalogContext,
    UpdateAccountInput,
    UpdateCategoryInput,
    VersionedAccountInput,
    VersionedCategoryInput,
)
from finbot.adapters.telegram.executor import TelegramMutationExecutor, TelegramMutationRequest
from finbot.application.catalogs import (
    ArchiveAccountCommand,
    ArchiveCategoryCommand,
    CreateAccountCommand,
    CreateCategoryCommand,
    RestoreAccountCommand,
    RestoreCategoryCommand,
    SetDefaultAccountCommand,
    UpdateAccountCommand,
    UpdateCategoryCommand,
)
from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    MutationResult,
    OwnerSnapshot,
)
from finbot.application.errors import ApplicationValidationError
from finbot.application.use_cases.catalogs import CatalogUseCases
from finbot.application.use_cases.queries import GetOwnerSettings, ListAccounts, ListCategories
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000201")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000301")
ARCHIVED_AT = datetime(2026, 8, 13, 10, tzinfo=UTC)


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


class _RecordingCatalogRepository:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[tuple[str, object]] = []

    def _record(self, operation: str, command: object) -> None:
        self.events.append(f"command.{operation}")
        self.calls.append((operation, command))

    async def create_account(self, command: CreateAccountCommand) -> AccountSnapshot:
        self._record("create_account", command)
        return AccountSnapshot(ACCOUNT_ID, command.name, "card", command.currency, None, 1)

    async def update_account(self, command: UpdateAccountCommand) -> AccountSnapshot:
        self._record("update_account", command)
        return AccountSnapshot(
            command.account_id,
            command.name,
            "card",
            "RUB",
            None,
            command.expected_version + 1,
        )

    async def archive_account(self, command: ArchiveAccountCommand) -> AccountSnapshot:
        self._record("archive_account", command)
        return AccountSnapshot(
            command.account_id,
            "Архивный счёт",
            "card",
            "RUB",
            ARCHIVED_AT,
            command.expected_version + 1,
        )

    async def restore_account(self, command: RestoreAccountCommand) -> AccountSnapshot:
        self._record("restore_account", command)
        return AccountSnapshot(
            command.account_id,
            "Восстановленный счёт",
            "card",
            "RUB",
            None,
            command.expected_version + 1,
        )

    async def set_default_account(self, command: SetDefaultAccountCommand) -> AccountSnapshot:
        self._record("set_default_account", command)
        return AccountSnapshot(
            command.account_id,
            "Основной счёт",
            "card",
            "RUB",
            None,
            command.expected_version + 1,
        )

    async def create_category(self, command: CreateCategoryCommand) -> CategorySnapshot:
        self._record("create_category", command)
        return CategorySnapshot(CATEGORY_ID, command.kind, command.name, "▫️", None, 1)

    async def update_category(self, command: UpdateCategoryCommand) -> CategorySnapshot:
        self._record("update_category", command)
        return CategorySnapshot(
            command.category_id,
            TransactionType.EXPENSE,
            command.name,
            "▫️",
            None,
            command.expected_version + 1,
        )

    async def archive_category(self, command: ArchiveCategoryCommand) -> CategorySnapshot:
        self._record("archive_category", command)
        return CategorySnapshot(
            command.category_id,
            TransactionType.EXPENSE,
            "Архивная категория",
            "▫️",
            ARCHIVED_AT,
            command.expected_version + 1,
        )

    async def restore_category(self, command: RestoreCategoryCommand) -> CategorySnapshot:
        self._record("restore_category", command)
        return CategorySnapshot(
            command.category_id,
            TransactionType.EXPENSE,
            "Восстановленная категория",
            "▫️",
            None,
            command.expected_version + 1,
        )


class _RecordingCatalogReader:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.active_accounts = (AccountSnapshot(ACCOUNT_ID, "Тайный счёт", "card", "RUB", None, 8),)
        self.archived_accounts = (
            AccountSnapshot(
                UUID("00000000-0000-7000-8000-000000000202"),
                "Скрытый архив",
                "cash",
                "USD",
                ARCHIVED_AT,
                3,
            ),
        )
        self.active_categories = (
            CategorySnapshot(
                CATEGORY_ID,
                TransactionType.EXPENSE,
                "Тайная категория",
                "▫️",
                None,
                5,
            ),
        )
        self.archived_categories = (
            CategorySnapshot(
                UUID("00000000-0000-7000-8000-000000000302"),
                TransactionType.INCOME,
                "Скрытая категория",
                "▫️",
                ARCHIVED_AT,
                2,
            ),
        )

    async def get_owner(self, owner_id: UUID) -> OwnerSnapshot | None:
        self.events.append("query.owner")
        if owner_id != OWNER_ID:
            return None
        return OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", ACCOUNT_ID)

    async def list_accounts(
        self,
        owner_id: UUID,
        *,
        archived: bool = False,
    ) -> tuple[AccountSnapshot, ...]:
        assert owner_id == OWNER_ID
        self.events.append(f"query.accounts:{archived}")
        return self.archived_accounts if archived else self.active_accounts

    async def list_categories(
        self,
        owner_id: UUID,
        *,
        kind: str | None = None,
        archived: bool = False,
    ) -> tuple[CategorySnapshot, ...]:
        assert owner_id == OWNER_ID
        assert kind is None
        self.events.append(f"query.categories:{archived}")
        return self.archived_categories if archived else self.active_categories


@dataclass(frozen=True, slots=True)
class _OperationCase:
    method: str
    values: object
    command_type: type[object]
    operation: CatalogOperation
    command_values: dict[str, object]
    account_operation: bool


OPERATION_CASES = (
    _OperationCase(
        "create_account",
        CreateAccountInput("Секретный счёт", " usd "),
        CreateAccountCommand,
        CatalogOperation.ACCOUNT_CREATED,
        {"owner_id": OWNER_ID, "name": "Секретный счёт", "currency": "USD"},
        True,
    ),
    _OperationCase(
        "update_account",
        UpdateAccountInput(ACCOUNT_ID, "Новое имя", 7),
        UpdateAccountCommand,
        CatalogOperation.ACCOUNT_UPDATED,
        {
            "owner_id": OWNER_ID,
            "account_id": ACCOUNT_ID,
            "name": "Новое имя",
            "expected_version": 7,
        },
        True,
    ),
    _OperationCase(
        "archive_account",
        VersionedAccountInput(ACCOUNT_ID, 7),
        ArchiveAccountCommand,
        CatalogOperation.ACCOUNT_ARCHIVED,
        {"owner_id": OWNER_ID, "account_id": ACCOUNT_ID, "expected_version": 7},
        True,
    ),
    _OperationCase(
        "restore_account",
        VersionedAccountInput(ACCOUNT_ID, 7),
        RestoreAccountCommand,
        CatalogOperation.ACCOUNT_RESTORED,
        {"owner_id": OWNER_ID, "account_id": ACCOUNT_ID, "expected_version": 7},
        True,
    ),
    _OperationCase(
        "set_default_account",
        VersionedAccountInput(ACCOUNT_ID, 7),
        SetDefaultAccountCommand,
        CatalogOperation.ACCOUNT_DEFAULT_SET,
        {"owner_id": OWNER_ID, "account_id": ACCOUNT_ID, "expected_version": 7},
        True,
    ),
    _OperationCase(
        "create_category",
        CreateCategoryInput("Секретная категория", TransactionType.INCOME),
        CreateCategoryCommand,
        CatalogOperation.CATEGORY_CREATED,
        {
            "owner_id": OWNER_ID,
            "name": "Секретная категория",
            "kind": TransactionType.INCOME,
        },
        False,
    ),
    _OperationCase(
        "update_category",
        UpdateCategoryInput(CATEGORY_ID, "Новое имя", 4),
        UpdateCategoryCommand,
        CatalogOperation.CATEGORY_UPDATED,
        {
            "owner_id": OWNER_ID,
            "category_id": CATEGORY_ID,
            "name": "Новое имя",
            "expected_version": 4,
        },
        False,
    ),
    _OperationCase(
        "archive_category",
        VersionedCategoryInput(CATEGORY_ID, 4),
        ArchiveCategoryCommand,
        CatalogOperation.CATEGORY_ARCHIVED,
        {"owner_id": OWNER_ID, "category_id": CATEGORY_ID, "expected_version": 4},
        False,
    ),
    _OperationCase(
        "restore_category",
        VersionedCategoryInput(CATEGORY_ID, 4),
        RestoreCategoryCommand,
        CatalogOperation.CATEGORY_RESTORED,
        {"owner_id": OWNER_ID, "category_id": CATEGORY_ID, "expected_version": 4},
        False,
    ),
)


def _request(*, update_id: int | None = 91_000_001) -> TelegramMutationRequest:
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
    repository: _RecordingCatalogRepository,
    reader: _RecordingCatalogReader,
    receipts: list[CatalogReceiptSnapshot],
    *,
    claimed: bool = True,
) -> CatalogController:
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
    executor = TelegramMutationExecutor(sessions)

    def use_case_factory(actual_session: AsyncSession) -> CatalogSessionUseCases:
        assert actual_session is cast(Any, session)
        events.append("use_case_factory")
        return CatalogSessionUseCases(
            catalogs=CatalogUseCases(repository),
            list_accounts=ListAccounts(reader),
            list_categories=ListCategories(reader),
            get_owner_settings=GetOwnerSettings(reader),
        )

    async def enqueue_receipt(
        actual_session: AsyncSession,
        request: TelegramMutationRequest,
        receipt: CatalogReceiptSnapshot,
    ) -> None:
        assert actual_session is cast(Any, session)
        assert request.update_id == 91_000_001
        events.append("outbox")
        receipts.append(receipt)

    return CatalogController(executor, use_case_factory, enqueue_receipt)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", OPERATION_CASES, ids=lambda case: case.method)
async def test_controller_maps_every_catalog_operation_and_enqueues_before_commit(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: _OperationCase,
) -> None:
    caplog.set_level(logging.DEBUG)
    events: list[str] = []
    receipts: list[CatalogReceiptSnapshot] = []
    repository = _RecordingCatalogRepository(events)
    reader = _RecordingCatalogReader(events)
    controller = _controller(monkeypatch, events, repository, reader, receipts)
    context = TelegramCatalogContext(_request(), message_id=77)

    result = await getattr(controller, case.method)(context, case.values)

    assert result is not None
    assert result.operation is case.operation
    assert result.message_id == 77
    assert receipts == [result]
    recorded_operation, command = repository.calls[0]
    assert recorded_operation == case.method
    assert isinstance(command, case.command_type)
    assert {name: getattr(command, name) for name in case.command_values} == case.command_values
    query_events = (
        ["query.owner", "query.accounts:False", "query.accounts:True"]
        if case.account_operation
        else ["query.owner", "query.categories:False", "query.categories:True"]
    )
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "use_case_factory",
        f"command.{case.method}",
        *query_events,
        "outbox",
        "commit",
        "session.exit",
    ]
    assert caplog.records == []


@pytest.mark.asyncio
async def test_duplicate_does_not_construct_use_cases_or_enqueue_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[CatalogReceiptSnapshot] = []
    repository = _RecordingCatalogRepository(events)
    controller = _controller(
        monkeypatch,
        events,
        repository,
        _RecordingCatalogReader(events),
        receipts,
        claimed=False,
    )

    result = await controller.archive_account(
        TelegramCatalogContext(_request()),
        VersionedAccountInput(ACCOUNT_ID, 3),
    )

    assert result is None
    assert repository.calls == []
    assert receipts == []
    assert events == ["session.enter", "claim", "rollback", "session.exit"]


@pytest.mark.asyncio
async def test_application_error_rolls_back_without_queries_or_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[CatalogReceiptSnapshot] = []
    repository = _RecordingCatalogRepository(events)
    controller = _controller(
        monkeypatch,
        events,
        repository,
        _RecordingCatalogReader(events),
        receipts,
    )

    with pytest.raises(ApplicationValidationError, match="Версия объекта"):
        await controller.update_category(
            TelegramCatalogContext(_request()),
            UpdateCategoryInput(CATEGORY_ID, "Скрытое имя", 0),
        )

    assert repository.calls == []
    assert receipts == []
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "use_case_factory",
        "rollback",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_none_update_returns_snapshot_after_commit_without_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipts: list[CatalogReceiptSnapshot] = []
    repository = _RecordingCatalogRepository(events)
    controller = _controller(
        monkeypatch,
        events,
        repository,
        _RecordingCatalogReader(events),
        receipts,
    )

    result = await controller.create_account(
        TelegramCatalogContext(_request(update_id=None), message_id=88),
        CreateAccountInput("Локальный счёт", "RUB"),
    )

    assert isinstance(result, AccountCatalogReceiptSnapshot)
    assert result.operation is CatalogOperation.ACCOUNT_CREATED
    assert result.message_id == 88
    assert receipts == []
    assert events == [
        "session.enter",
        "claim",
        "ensure_owner",
        "use_case_factory",
        "command.create_account",
        "query.owner",
        "query.accounts:False",
        "query.accounts:True",
        "commit",
        "session.exit",
    ]


def test_catalog_inputs_context_and_receipts_hide_private_values_from_repr() -> None:
    owner = OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", ACCOUNT_ID)
    account = AccountSnapshot(ACCOUNT_ID, "Тайный счёт", "card", "RUB", None, 1)
    category = CategorySnapshot(
        CATEGORY_ID,
        TransactionType.EXPENSE,
        "Тайная категория",
        "▫️",
        None,
        1,
    )
    account_receipt = AccountCatalogReceiptSnapshot(
        CatalogOperation.ACCOUNT_CREATED,
        mutation=MutationResult(ACCOUNT_ID, 1, "active"),
        owner=owner,
        active_accounts=(account,),
        archived_accounts=(),
        message_id=55,
    )
    category_receipt = CategoryCatalogReceiptSnapshot(
        CatalogOperation.CATEGORY_CREATED,
        mutation=MutationResult(CATEGORY_ID, 1, "active"),
        owner=owner,
        active_categories=(category,),
        archived_categories=(),
        message_id=55,
    )
    values = (
        TelegramCatalogContext(_request(), 55),
        CreateAccountInput("Тайный счёт", "RUB"),
        UpdateAccountInput(ACCOUNT_ID, "Тайный счёт", 1),
        VersionedAccountInput(ACCOUNT_ID, 1),
        CreateCategoryInput("Тайная категория", TransactionType.EXPENSE),
        UpdateCategoryInput(CATEGORY_ID, "Тайная категория", 1),
        VersionedCategoryInput(CATEGORY_ID, 1),
        account_receipt,
        category_receipt,
    )

    for value in values:
        rendered = repr(value)
        assert str(OWNER_ID) not in rendered
        assert str(ACCOUNT_ID) not in rendered
        assert str(CATEGORY_ID) not in rendered
        assert "Тайн" not in rendered
        assert "RUB" not in rendered
        assert "91000001" not in rendered
        assert "92000002" not in rendered
        assert "93000003" not in rendered


@pytest.mark.parametrize("message_id", [True, False, 0, -1])
def test_context_rejects_invalid_message_id(message_id: int) -> None:
    with pytest.raises(TypeError if isinstance(message_id, bool) else ValueError):
        TelegramCatalogContext(_request(), message_id)


def test_catalog_controller_has_no_database_or_sqlalchemy_dependency() -> None:
    path = Path(__file__).parents[2] / "src/finbot/adapters/telegram/controllers/catalogs.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imports.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(module == "sqlalchemy" or module.startswith("sqlalchemy.") for module in imports)
    assert not any(module.startswith("finbot.adapters.database") for module in imports)
