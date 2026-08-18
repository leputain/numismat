import ast
from datetime import UTC, datetime
from inspect import getsource
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from aiogram.types import CallbackQuery, Chat, Message

from finbot.adapters.telegram.controllers.catalogs import (
    AccountCatalogReceiptSnapshot,
    CatalogController,
    CatalogOperation,
    CatalogReceiptSnapshot,
    TelegramCatalogContext,
    VersionedAccountInput,
    VersionedCategoryInput,
)
from finbot.adapters.telegram.executor import TelegramMutationRequest
from finbot.adapters.telegram.routers.catalog_callbacks import CatalogCallbackRouter
from finbot.application.dto import (
    AccountSnapshot,
    MutationResult,
    OwnerSnapshot,
)
from finbot.application.errors import (
    ApplicationValidationError,
    ObjectVersionConflictError,
)
from finbot.application.interactions import MAX_OBJECT_VERSION
from finbot.bootstrap import build_dispatcher

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000201")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000301")


def _request(update_id: int | None) -> TelegramMutationRequest:
    return TelegramMutationRequest(
        update_id=update_id,
        owner_telegram_user_id=92_000_002,
        chat_id=93_000_003,
        locale="ru",
        timezone="UTC",
        currency="RUB",
    )


def _context(update_id: int | None, message: Message) -> TelegramCatalogContext:
    return TelegramCatalogContext(_request(update_id), message.message_id)


def _receipt() -> AccountCatalogReceiptSnapshot:
    account = AccountSnapshot(ACCOUNT_ID, "Private", "card", "RUB", None, 2)
    return AccountCatalogReceiptSnapshot(
        CatalogOperation.ACCOUNT_DEFAULT_SET,
        MutationResult(ACCOUNT_ID, 2, "active"),
        OwnerSnapshot(OWNER_ID, "ru", "UTC", "RUB", ACCOUNT_ID),
        (account,),
        (),
        message_id=700,
    )


class _Callback:
    def __init__(self, data: str | None) -> None:
        self.message = Message(
            message_id=700,
            date=datetime(2026, 8, 13, tzinfo=UTC),
            chat=Chat(id=93_000_003, type="private"),
            text="settings",
        )
        self.data = data
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(
        self,
        text: str | None = None,
        *,
        show_alert: bool = False,
    ) -> None:
        self.answers.append((text, show_alert))


def _callback(data: str | None) -> tuple[CallbackQuery, _Callback]:
    callback = _Callback(data)
    return cast(CallbackQuery, callback), callback


class _Controller:
    def __init__(
        self,
        receipt: CatalogReceiptSnapshot | None = None,
        error: Exception | None = None,
    ) -> None:
        self.receipt = receipt
        self.error = error
        self.calls: list[tuple[str, TelegramCatalogContext, object]] = []

    async def _run(
        self,
        name: str,
        context: TelegramCatalogContext,
        values: object,
    ) -> CatalogReceiptSnapshot | None:
        self.calls.append((name, context, values))
        if self.error is not None:
            raise self.error
        return self.receipt

    async def set_default_account(
        self,
        context: TelegramCatalogContext,
        values: VersionedAccountInput,
    ) -> CatalogReceiptSnapshot | None:
        return await self._run("set_default_account", context, values)

    async def archive_account(
        self,
        context: TelegramCatalogContext,
        values: VersionedAccountInput,
    ) -> CatalogReceiptSnapshot | None:
        return await self._run("archive_account", context, values)

    async def restore_account(
        self,
        context: TelegramCatalogContext,
        values: VersionedAccountInput,
    ) -> CatalogReceiptSnapshot | None:
        return await self._run("restore_account", context, values)

    async def archive_category(
        self,
        context: TelegramCatalogContext,
        values: VersionedCategoryInput,
    ) -> CatalogReceiptSnapshot | None:
        return await self._run("archive_category", context, values)

    async def restore_category(
        self,
        context: TelegramCatalogContext,
        values: VersionedCategoryInput,
    ) -> CatalogReceiptSnapshot | None:
        return await self._run("restore_category", context, values)


def _router(
    controller: _Controller,
    deliveries: list[CatalogReceiptSnapshot],
) -> CatalogCallbackRouter:
    async def deliver(message: Message, receipt: CatalogReceiptSnapshot) -> None:
        assert message.message_id == 700
        deliveries.append(receipt)

    return CatalogCallbackRouter(
        cast(CatalogController, controller),
        _context,
        deliver,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "data", "operation", "input_type", "entity_id", "success"),
    [
        (
            "settings_account_default",
            f"sa:default:{ACCOUNT_ID}:2",
            "set_default_account",
            VersionedAccountInput,
            ACCOUNT_ID,
            "Основной счёт изменён",
        ),
        (
            "settings_account_archive_do",
            f"sa:archive:do:{ACCOUNT_ID}:2",
            "archive_account",
            VersionedAccountInput,
            ACCOUNT_ID,
            "Счёт перенесён в архив",
        ),
        (
            "settings_account_restore",
            f"sa:restore:{ACCOUNT_ID}:2",
            "restore_account",
            VersionedAccountInput,
            ACCOUNT_ID,
            "Счёт восстановлен",
        ),
        (
            "settings_category_archive_do",
            f"sc:archive:do:{CATEGORY_ID}:2",
            "archive_category",
            VersionedCategoryInput,
            CATEGORY_ID,
            "Категория перенесена в архив",
        ),
        (
            "settings_category_restore",
            f"sc:restore:{CATEGORY_ID}:2",
            "restore_category",
            VersionedCategoryInput,
            CATEGORY_ID,
            "Категория восстановлена",
        ),
    ],
    ids=(
        "account-default",
        "account-archive",
        "account-restore",
        "category-archive",
        "category-restore",
    ),
)
async def test_catalog_callbacks_preserve_operation_parsing_and_success_ack(
    method_name: str,
    data: str,
    operation: str,
    input_type: type[VersionedAccountInput] | type[VersionedCategoryInput],
    entity_id: UUID,
    success: str,
) -> None:
    receipt = _receipt()
    controller = _Controller(receipt)
    deliveries: list[CatalogReceiptSnapshot] = []
    callback, tracked = _callback(data)

    await getattr(_router(controller, deliveries), method_name)(callback, None)

    name, context, values = controller.calls[0]
    assert name == operation
    assert context.request.update_id is None
    assert isinstance(values, input_type)
    assert values.expected_version == 2
    assert (
        values.account_id if isinstance(values, VersionedAccountInput) else values.category_id
    ) == entity_id
    assert deliveries == [receipt]
    assert tracked.answers == [(success, False)]


@pytest.mark.asyncio
async def test_tracked_callback_never_performs_direct_delivery() -> None:
    receipt = _receipt()
    controller = _Controller(receipt)
    deliveries: list[CatalogReceiptSnapshot] = []
    callback, _ = _callback(f"sa:default:{ACCOUNT_ID}:2")

    await _router(controller, deliveries).settings_account_default(
        callback,
        91_000_001,
    )

    assert controller.calls[0][1].request.update_id == 91_000_001
    assert deliveries == []


@pytest.mark.asyncio
async def test_duplicate_callback_keeps_legacy_already_processed_ack() -> None:
    controller = _Controller(None)
    callback, tracked = _callback(f"sa:restore:{ACCOUNT_ID}:2")

    await _router(controller, []).settings_account_restore(callback, 91_000_001)

    assert tracked.answers == [("Уже обработано", False)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        "sa:default:bad",
        f"sa:default:{ACCOUNT_ID}:0",
        "sa:default:00000000-0000-7000-8000-00000000020A:2",
        f"sa:default:{ACCOUNT_ID}:02",
        f"sa:default:{ACCOUNT_ID}:+2",
        f"sa:default:{ACCOUNT_ID}:{MAX_OBJECT_VERSION + 1}",
    ],
    ids=(
        "malformed",
        "zero-version",
        "uppercase-uuid",
        "leading-zero-version",
        "signed-version",
        "version-overflow",
    ),
)
async def test_malformed_callback_fails_closed_before_controller(data: str) -> None:
    controller = _Controller(_receipt())
    callback, tracked = _callback(data)

    await _router(controller, []).settings_account_default(callback)

    assert controller.calls == []
    assert tracked.answers == [("Кнопка повреждена", True)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ObjectVersionConflictError(), "Счёт уже изменён. Обновите список"),
        (ApplicationValidationError("Безопасная ошибка"), "Безопасная ошибка"),
    ],
)
async def test_account_errors_preserve_alert_contract(
    error: Exception,
    expected: str,
) -> None:
    callback, tracked = _callback(f"sa:archive:do:{ACCOUNT_ID}:2")

    await _router(_Controller(error=error), []).settings_account_archive_do(callback)

    assert tracked.answers == [(expected, True)]


def test_router_hides_injected_dependencies_and_has_no_database_imports() -> None:
    router = _router(_Controller(_receipt()), [])
    path = (
        Path(__file__).parents[2]
        / "src"
        / "finbot"
        / "adapters"
        / "telegram"
        / "routers"
        / "catalog_callbacks.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert repr(router) == "CatalogCallbackRouter()"
    assert not any(
        module.startswith(("sqlalchemy", "finbot.adapters.database", "finbot.bootstrap"))
        for module in imports
    )


def test_bootstrap_only_wires_catalog_router_and_extracted_draft_router() -> None:
    source = getsource(build_dispatcher)

    assert "catalog_callback_router = CatalogCallbackRouter(" in source
    assert "set_default_account=catalog_callback_router.settings_account_default" in source
    assert "archive_account=catalog_callback_router.settings_account_archive_do" in source
    assert "restore_account=catalog_callback_router.settings_account_restore" in source
    assert "archive_category=catalog_callback_router.settings_category_archive_do" in source
    assert "restore_category=catalog_callback_router.settings_category_restore" in source
    assert "versioned_draft=draft_interaction_router.versioned_draft" in source
    assert "async def versioned_draft_button(" not in source
    for name in (
        "settings_account_default",
        "settings_account_archive_do",
        "settings_account_restore",
        "settings_category_archive_do",
        "settings_category_restore",
    ):
        assert f"async def {name}(" not in source
