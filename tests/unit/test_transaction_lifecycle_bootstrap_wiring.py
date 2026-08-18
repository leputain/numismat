import ast
import inspect
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import finbot.bootstrap as bootstrap
from finbot.adapters.telegram.controllers.transaction_lifecycle import (
    TransactionLifecycleOperation,
    TransactionLifecycleReceiptSnapshot,
)
from finbot.adapters.telegram.routers.transaction_lifecycle import TransactionLifecycleRouter
from finbot.application.dto import (
    OwnerSnapshot,
    TransactionMutationResult,
    TransactionSnapshot,
)
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
TRANSACTION_ID = UUID("00000000-0000-7000-8000-000000000401")


def _receipt(
    operation: TransactionLifecycleOperation,
    history_page: int | None,
) -> TransactionLifecycleReceiptSnapshot:
    deleted_at = (
        datetime(2026, 8, 13, 13, tzinfo=UTC)
        if operation is TransactionLifecycleOperation.DELETE
        else None
    )
    transaction = TransactionSnapshot(
        transaction_id=TRANSACTION_ID,
        kind=TransactionType.EXPENSE,
        amount_minor=12_500,
        currency="RUB",
        account_id=UUID("00000000-0000-7000-8000-000000000201"),
        account_name="Счёт",
        category_id=UUID("00000000-0000-7000-8000-000000000301"),
        category_name="Категория",
        category_emoji="▫️",
        occurred_at=datetime(2026, 8, 13, 12, tzinfo=UTC),
        description="description",
        deleted_at=deleted_at,
        version=8,
    )
    return TransactionLifecycleReceiptSnapshot(
        operation,
        TransactionMutationResult(
            TRANSACTION_ID,
            8,
            "deleted" if deleted_at is not None else "active",
            transaction,
        ),
        OwnerSnapshot(OWNER_ID, "ru", "UTC", "RUB", None),
        700,
        history_page,
    )


def _callbacks(receipt: TransactionLifecycleReceiptSnapshot) -> set[str]:
    rendered = bootstrap._transaction_lifecycle_receipt(receipt)
    assert rendered.reply_markup is not None
    return {
        button.callback_data
        for row in rendered.reply_markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    }


def test_lifecycle_renderer_preserves_delete_and_restore_page_semantics() -> None:
    deleted = bootstrap._transaction_lifecycle_receipt(
        _receipt(TransactionLifecycleOperation.DELETE, 3)
    )
    restored = bootstrap._transaction_lifecycle_receipt(
        _receipt(TransactionLifecycleOperation.RESTORE, 3)
    )
    trash_restored = bootstrap._transaction_lifecycle_receipt(
        _receipt(TransactionLifecycleOperation.RESTORE, None)
    )

    assert "🗑 Операция удалена" in deleted.text
    assert f"tx:restore:{TRANSACTION_ID}:8:3" in _callbacks(
        _receipt(TransactionLifecycleOperation.DELETE, 3)
    )
    assert "✅ Операция восстановлена" in restored.text
    assert f"tx:repeat:{TRANSACTION_ID}:8:3" in _callbacks(
        _receipt(TransactionLifecycleOperation.RESTORE, 3)
    )
    assert f"tx:repeat:{TRANSACTION_ID}:8:0" in _callbacks(
        _receipt(TransactionLifecycleOperation.RESTORE, None)
    )
    assert trash_restored.reply_markup is not None


def test_bootstrap_only_constructs_and_registers_transaction_lifecycle_router() -> None:
    source = inspect.getsource(bootstrap.build_dispatcher)

    assert "TransactionLifecycleRouter(" in source
    assert "transaction_lifecycle_router.register(dp)" in source
    assert source.index("transaction_lifecycle_router.register(dp)") < source.index(
        "register_late_callback_fallbacks("
    )
    assert source.index("transaction_lifecycle_router.register(dp)") < source.index(
        "text_input_router.register(dp)"
    )
    for handler_name in (
        "delete_transaction",
        "legacy_delete_requires_confirmation",
        "restore_deleted",
        "repeat_transaction",
        "trash_restore",
        "trash_noop",
        "edit_menu",
        "undo",
    ):
        assert f"async def {handler_name}(" not in source


def test_transaction_lifecycle_router_has_no_database_or_bootstrap_imports() -> None:
    path = Path(inspect.getfile(TransactionLifecycleRouter))
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    forbidden = ("sqlalchemy", "finbot.adapters.database", "finbot.bootstrap")
    violations: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = tuple(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules = (node.module,)
        else:
            continue
        violations.extend(
            (node.lineno, module) for module in modules if module.startswith(forbidden)
        )

    assert violations == []


def test_lifecycle_handlers_do_not_own_sessions_commits_or_database_calls() -> None:
    source = inspect.getsource(TransactionLifecycleRouter)
    tree = ast.parse(source)
    forbidden_names = {
        "claim_update",
        "ensure_user",
        "get_transaction_details",
        "undo_last_action",
    }
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    session_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"session", "replay_session"}
    }

    assert called_names.isdisjoint(forbidden_names)
    assert {"commit", "rollback", "scalar", "execute"}.isdisjoint(session_calls)
