from datetime import UTC, datetime
from uuid import UUID

import pytest
from fakes.undo import InMemoryUndoRepository

from finbot.application.dto import TransactionSnapshot
from finbot.application.undo import UndoAction, UndoActionResult
from finbot.application.use_cases.undo import UndoLastAction
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
TRANSACTION_ID = UUID("00000000-0000-7000-8000-000000000401")


def _transaction() -> TransactionSnapshot:
    return TransactionSnapshot(
        transaction_id=TRANSACTION_ID,
        kind=TransactionType.EXPENSE,
        amount_minor=12_500,
        currency="RUB",
        account_id=UUID("00000000-0000-7000-8000-000000000201"),
        account_name="Private account",
        category_id=UUID("00000000-0000-7000-8000-000000000301"),
        category_name="Private category",
        category_emoji="▫️",
        occurred_at=datetime(2026, 8, 13, 12, tzinfo=UTC),
        description="private description",
        deleted_at=datetime(2026, 8, 13, 13, tzinfo=UTC),
        version=4,
    )


@pytest.mark.asyncio
async def test_undo_delegates_only_the_explicit_owner_to_the_audit_port() -> None:
    expected = UndoActionResult(UndoAction.CREATE, _transaction())
    repository = InMemoryUndoRepository(expected)

    result = await UndoLastAction(repository).execute(OWNER_ID)

    assert result is expected
    assert repository.calls == [OWNER_ID]


@pytest.mark.asyncio
async def test_undo_without_an_audit_result_does_not_guess_a_transaction() -> None:
    repository = InMemoryUndoRepository(None)

    assert await UndoLastAction(repository).execute(OWNER_ID) is None
    assert repository.calls == [OWNER_ID]


@pytest.mark.asyncio
async def test_undo_rejects_non_uuid_owner_before_calling_the_port() -> None:
    repository = InMemoryUndoRepository(None)

    with pytest.raises(TypeError, match="UUID"):
        await UndoLastAction(repository).execute("owner")  # type: ignore[arg-type]

    assert repository.calls == []


def test_undo_contract_repr_hides_transaction_values() -> None:
    result = UndoActionResult(UndoAction.UPDATE, _transaction())

    assert "Private" not in repr(result)
    assert "12500" not in repr(result)
