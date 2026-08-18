from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from fakes.repositories import InMemoryDraftRepository

from finbot.application.dto import (
    DraftRef,
    DraftSnapshot,
    EditTransactionCommand,
    OwnerSnapshot,
    TransactionMutationResult,
    TransactionSnapshot,
)
from finbot.application.transaction_edit_text_input import (
    TransactionEditTextInputCommand,
    TransactionEditTextInputError,
    TransactionEditTextInputField,
    TransactionEditTextInputStatus,
    TransactionEditTextTarget,
    TransactionEditTextTargetRepository,
)
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.application.use_cases.transaction_edit_text_input import (
    TransactionEditTextInputUseCase,
)
from finbot.application.use_cases.transactions import TransactionUseCases
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
TRANSACTION_ID = UUID("00000000-0000-7000-8000-000000000501")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000601")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000701")
NOW = datetime(2026, 8, 13, 9, 30, tzinfo=UTC)


class _Clock:
    def now(self, timezone: str) -> datetime:
        assert timezone == "Europe/Moscow"
        return datetime(2026, 8, 13, 12, 30, tzinfo=ZoneInfo(timezone))


class _TargetRepository:
    def __init__(self, target: TransactionEditTextTarget) -> None:
        self.target = target
        self.calls: list[tuple[UUID, object]] = []

    async def lock_target(
        self,
        owner_id: UUID,
        expected: DraftRef,
    ) -> TransactionEditTextTarget:
        self.calls.append((owner_id, expected))
        assert expected == self.target.draft.ref
        return self.target


class _Transactions:
    def __init__(self, current: TransactionSnapshot) -> None:
        self.current = current
        self.calls: list[EditTransactionCommand] = []

    async def edit(self, command: EditTransactionCommand) -> TransactionMutationResult:
        self.calls.append(command)
        updated = replace(
            self.current,
            amount_minor=command.amount_minor or self.current.amount_minor,
            occurred_at=command.occurred_at or self.current.occurred_at,
            description=(
                command.description if command.description is not None else self.current.description
            ),
            version=self.current.version + 1,
        )
        return TransactionMutationResult(
            updated.transaction_id,
            updated.version,
            "updated",
            updated,
        )


def _owner() -> OwnerSnapshot:
    return OwnerSnapshot(
        OWNER_ID,
        "ru",
        "Europe/Moscow",
        "RUB",
        ACCOUNT_ID,
    )


def _transaction() -> TransactionSnapshot:
    return TransactionSnapshot(
        transaction_id=TRANSACTION_ID,
        kind=TransactionType.EXPENSE,
        amount_minor=12_345,
        currency="RUB",
        account_id=ACCOUNT_ID,
        account_name="Закрытый счёт",
        category_id=CATEGORY_ID,
        category_name="Закрытая категория",
        category_emoji="▫️",
        occurred_at=NOW,
        description="закрытое описание",
        version=4,
    )


async def _subject(
    state: str,
) -> tuple[
    TransactionEditTextInputUseCase,
    DraftSnapshot,
    InMemoryDraftRepository,
    _TargetRepository,
    _Transactions,
]:
    repository = InMemoryDraftRepository()
    drafts = DraftUseCases(repository)
    active = await repository.create_if_absent(
        OWNER_ID,
        state,
        {"transaction_id": str(TRANSACTION_ID), "version": 4},
    )
    target_repository = _TargetRepository(
        TransactionEditTextTarget(_owner(), active, _transaction())
    )
    transactions = _Transactions(_transaction())
    use_case = TransactionEditTextInputUseCase(
        cast(TransactionEditTextTargetRepository, target_repository),
        cast(TransactionUseCases, transactions),
        drafts,
        _Clock(),
    )
    return use_case, active, repository, target_repository, transactions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "text", "field", "expected_value"),
    [
        ("edit_amount", "222,22", "amount_minor", 22_222),
        ("edit_date", "12.08.2026", "occurred_at", (2026, 8, 12)),
        ("edit_description", " - ", "description", ""),
    ],
)
async def test_valid_text_edits_transaction_and_deletes_exact_draft(
    state: str,
    text: str,
    field: str,
    expected_value: object,
) -> None:
    use_case, active, repository, targets, transactions = await _subject(state)

    result = await use_case.execute(TransactionEditTextInputCommand(OWNER_ID, active.ref, text))

    assert result.status is TransactionEditTextInputStatus.UPDATED
    assert (
        result.edited_field
        is {
            "edit_amount": TransactionEditTextInputField.AMOUNT,
            "edit_date": TransactionEditTextInputField.DATE,
            "edit_description": TransactionEditTextInputField.DESCRIPTION,
        }[state]
    )
    assert result.retry_error is None
    assert result.draft is None
    assert result.transaction.version == 5
    assert await repository.get_active(OWNER_ID) is None
    assert targets.calls == [(OWNER_ID, active.ref)]
    assert len(transactions.calls) == 1
    value = getattr(transactions.calls[0], field)
    if field == "occurred_at":
        assert value is not None
        assert (value.year, value.month, value.day) == expected_value
    else:
        assert value == expected_value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "text", "error"),
    [
        ("edit_amount", "не сумма", TransactionEditTextInputError.INVALID_AMOUNT),
        ("edit_date", "завтра после обеда", TransactionEditTextInputError.INVALID_DATE),
        (
            "edit_description",
            "x" * 501,
            TransactionEditTextInputError.INVALID_DESCRIPTION,
        ),
    ],
)
async def test_invalid_text_preserves_transaction_and_advances_exact_retry_revision(
    state: str,
    text: str,
    error: TransactionEditTextInputError,
) -> None:
    use_case, active, repository, _targets, transactions = await _subject(state)

    result = await use_case.execute(TransactionEditTextInputCommand(OWNER_ID, active.ref, text))

    assert result.status is TransactionEditTextInputStatus.RETRY
    assert result.retry_error is error
    assert result.transaction == _transaction()
    assert result.draft is not None
    assert result.draft.ref.draft_id == active.draft_id
    assert result.draft.revision == active.revision + 1
    assert result.draft.state == state
    assert dict(result.draft.payload) == {
        "transaction_id": str(TRANSACTION_ID),
        "version": 4,
    }
    assert await repository.get_active(OWNER_ID) == result.draft
    assert transactions.calls == []


def test_contracts_reject_noncanonical_target_and_hide_financial_values_from_repr() -> None:
    draft = DraftSnapshot(
        UUID("00000000-0000-7000-8000-000000000401"),
        "edit_amount",
        {"transaction_id": str(TRANSACTION_ID), "version": 4},
    )
    target = TransactionEditTextTarget(_owner(), draft, _transaction())
    command = TransactionEditTextInputCommand(OWNER_ID, draft.ref, "123,45")

    with pytest.raises(ValueError, match="canonical"):
        TransactionEditTextTarget(
            _owner(),
            replace(draft, payload={**draft.payload, "history_page": 3}),
            _transaction(),
        )
    with pytest.raises(ValueError, match="too long"):
        TransactionEditTextInputCommand(OWNER_ID, draft.ref, "x" * 4097)

    rendered = repr((target, command))
    for private in (
        str(OWNER_ID),
        str(TRANSACTION_ID),
        str(ACCOUNT_ID),
        str(CATEGORY_ID),
        "12345",
        "123,45",
        "RUB",
        "Закрытый",
        "закрытое",
    ):
        assert private not in rendered
