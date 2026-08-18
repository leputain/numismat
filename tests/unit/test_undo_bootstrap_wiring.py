import ast
import inspect
import textwrap
from datetime import UTC, datetime
from uuid import UUID

import pytest

import finbot.bootstrap as bootstrap
from finbot.adapters.telegram.controllers.undo import UndoReceiptSnapshot
from finbot.adapters.telegram.routers.transaction_lifecycle import (
    TransactionLifecycleRouter,
)
from finbot.application.dto import OwnerSnapshot, TransactionSnapshot
from finbot.application.undo import UndoAction, UndoActionResult
from finbot.domain.transactions import TransactionType

TRANSACTION_ID = UUID("00000000-0000-7000-8000-000000000401")


def _receipt(action: UndoAction, *, deleted: bool) -> UndoReceiptSnapshot:
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
        deleted_at=datetime(2026, 8, 13, 13, tzinfo=UTC) if deleted else None,
        version=7,
    )
    return UndoReceiptSnapshot(
        owner=OwnerSnapshot(
            UUID("00000000-0000-7000-8000-000000000101"),
            "ru",
            "UTC",
            "RUB",
            None,
        ),
        result=UndoActionResult(action, transaction),
    )


@pytest.mark.parametrize(
    ("action", "label"),
    [
        (UndoAction.CREATE, "Создание операции отменено"),
        (UndoAction.DELETE, "Удаление отменено"),
        (UndoAction.RESTORE, "Восстановление отменено"),
        (UndoAction.UPDATE, "Последнее изменение отменено"),
    ],
)
def test_undo_renderer_preserves_labels_and_versioned_keyboards(
    action: UndoAction,
    label: str,
) -> None:
    deleted = action in {UndoAction.CREATE, UndoAction.RESTORE}

    rendered = bootstrap._undo_receipt(_receipt(action, deleted=deleted))

    assert label in rendered.text
    assert rendered.reply_markup is not None
    callbacks = [
        button.callback_data
        for row in rendered.reply_markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]
    expected_prefix = "tx:restore:" if deleted else "tx:"
    assert any(value.startswith(expected_prefix) for value in callbacks)
    assert any(f":{TRANSACTION_ID}:7:" in value for value in callbacks)


def test_empty_undo_result_preserves_the_existing_message_without_buttons() -> None:
    receipt = UndoReceiptSnapshot(
        OwnerSnapshot(
            UUID("00000000-0000-7000-8000-000000000101"),
            "ru",
            "UTC",
            "RUB",
            None,
        )
    )

    rendered = bootstrap._undo_receipt(receipt)

    assert rendered.text == "Отменять пока нечего."
    assert rendered.reply_markup is None


def test_bootstrap_undo_handler_contains_no_database_or_manual_commit_calls() -> None:
    source = textwrap.dedent(inspect.getsource(TransactionLifecycleRouter.undo))
    tree = ast.parse(source)
    undo = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "undo"
    )
    forbidden_names = {
        "claim_update",
        "ensure_user",
        "get_transaction_details",
        "undo_last_action",
    }
    called_names = {
        node.func.id
        for node in ast.walk(undo)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    session_calls = {
        node.func.attr
        for node in ast.walk(undo)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"session", "replay_session"}
    }

    assert called_names.isdisjoint(forbidden_names)
    assert {"commit", "rollback", "scalar", "execute"}.isdisjoint(session_calls)
    assert "async def undo(" not in inspect.getsource(bootstrap.build_dispatcher)
