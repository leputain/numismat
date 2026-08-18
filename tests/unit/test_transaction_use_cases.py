from datetime import UTC, datetime
from uuid import uuid7

import pytest
from fakes.repositories import InMemoryDraftRepository
from fakes.transactions import InMemoryTransactionCommandRepository

from finbot.application.dto import (
    ConfirmTransactionDraftCommand,
    DraftRef,
    EditTransactionCommand,
    PreparedTransactionDraft,
    PrepareRepeatDraftCommand,
    ReviewedTransactionInput,
    TransactionMutationResult,
    TransactionSnapshot,
    VersionedTransactionCommand,
)
from finbot.application.errors import (
    ApplicationValidationError,
    DraftRevisionConflictError,
    InvalidStateError,
    ReviewRequiredError,
)
from finbot.application.use_cases.transactions import TransactionUseCases
from finbot.domain.transactions import TransactionType


def _transaction_snapshot() -> TransactionSnapshot:
    return TransactionSnapshot(
        transaction_id=uuid7(),
        kind=TransactionType.EXPENSE,
        amount_minor=1250,
        currency="RUB",
        account_id=uuid7(),
        account_name="Счёт",
        category_id=uuid7(),
        category_name="Категория",
        category_emoji="▫️",
        occurred_at=datetime(2026, 8, 13, 12, tzinfo=UTC),
        description="private description",
    )


def _harness() -> tuple[
    TransactionUseCases,
    InMemoryDraftRepository,
    InMemoryTransactionCommandRepository,
    TransactionSnapshot,
]:
    snapshot = _transaction_snapshot()
    result = TransactionMutationResult(
        entity_id=snapshot.transaction_id,
        version=snapshot.version,
        resulting_state="confirmed",
        transaction=snapshot,
    )
    prepared = PreparedTransactionDraft(
        source_transaction_id=snapshot.transaction_id,
        source_version=snapshot.version,
        transaction=ReviewedTransactionInput(
            kind=snapshot.kind,
            amount_minor=snapshot.amount_minor,
            account_id=snapshot.account_id,
            category_id=snapshot.category_id,
            occurred_at=datetime(2026, 8, 14, 12, tzinfo=UTC),
            description=snapshot.description,
        ),
        currency=snapshot.currency,
        account_name=snapshot.account_name,
        category_name=snapshot.category_name,
        category_emoji=snapshot.category_emoji,
    )
    drafts = InMemoryDraftRepository()
    commands = InMemoryTransactionCommandRepository(
        mutation_result=result,
        prepared_repeat=prepared,
    )
    return TransactionUseCases(commands, drafts), drafts, commands, snapshot


@pytest.mark.asyncio
async def test_confirm_accepts_only_the_current_reviewed_draft() -> None:
    use_cases, drafts, commands, _snapshot = _harness()
    owner_id = uuid7()
    active = await drafts.create_if_absent(owner_id, "quick_confirm", {"step": "review"})
    command = ConfirmTransactionDraftCommand(owner_id=owner_id, expected=active.ref)

    result = await use_cases.confirm(command)

    assert result.resulting_state == "confirmed"
    assert commands.calls == [command]


@pytest.mark.asyncio
async def test_confirm_rejects_stale_non_review_and_suspended_drafts_before_mutation() -> None:
    use_cases, drafts, commands, _snapshot = _harness()
    owner_id = uuid7()
    active = await drafts.create_if_absent(owner_id, "amount", {"step": "input"})

    with pytest.raises(DraftRevisionConflictError):
        await use_cases.confirm(
            ConfirmTransactionDraftCommand(
                owner_id=owner_id,
                expected=DraftRef(active.draft_id, active.revision + 1),
            )
        )
    with pytest.raises(ReviewRequiredError):
        await use_cases.confirm(
            ConfirmTransactionDraftCommand(owner_id=owner_id, expected=active.ref)
        )

    reviewed = await drafts.update(owner_id, active.ref, "review", {})
    suspended = await drafts.set_suspended(owner_id, reviewed.ref, True)
    with pytest.raises(ReviewRequiredError):
        await use_cases.confirm(
            ConfirmTransactionDraftCommand(owner_id=owner_id, expected=suspended.ref)
        )
    assert commands.calls == []


@pytest.mark.asyncio
async def test_base_confirm_refuses_ocr_queue_orchestration() -> None:
    use_cases, drafts, commands, _snapshot = _harness()
    owner_id = uuid7()
    active = await drafts.create_if_absent(owner_id, "quick_confirm", {"ocr_batch": {}})

    with pytest.raises(InvalidStateError):
        await use_cases.confirm(
            ConfirmTransactionDraftCommand(owner_id=owner_id, expected=active.ref)
        )

    assert commands.calls == []


@pytest.mark.asyncio
async def test_confirm_delegates_plain_review_with_pending_rule_to_atomic_repository() -> None:
    use_cases, drafts, commands, _snapshot = _harness()
    owner_id = uuid7()
    active = await drafts.create_if_absent(
        owner_id,
        "quick_confirm",
        {"pending_rule": {"pattern": "private merchant", "scope": "global"}},
    )
    command = ConfirmTransactionDraftCommand(owner_id=owner_id, expected=active.ref)

    result = await use_cases.confirm(command)

    assert result.resulting_state == "confirmed"
    assert commands.calls == [command]


@pytest.mark.asyncio
async def test_edit_requires_at_least_one_explicit_patch() -> None:
    use_cases, _drafts, commands, snapshot = _harness()
    command = EditTransactionCommand(
        owner_id=uuid7(),
        transaction_id=snapshot.transaction_id,
        expected_version=snapshot.version,
    )

    with pytest.raises(ApplicationValidationError):
        await use_cases.edit(command)
    assert commands.calls == []


@pytest.mark.asyncio
async def test_delete_and_restore_preserve_the_versioned_command() -> None:
    use_cases, _drafts, commands, snapshot = _harness()
    command = VersionedTransactionCommand(
        owner_id=uuid7(),
        transaction_id=snapshot.transaction_id,
        expected_version=snapshot.version,
    )

    await use_cases.delete(command)
    await use_cases.restore(command)

    assert commands.calls == [command, command]


@pytest.mark.asyncio
async def test_repeat_prepares_a_review_draft_and_never_confirms_a_transaction() -> None:
    use_cases, drafts, commands, snapshot = _harness()
    owner_id = uuid7()
    command = PrepareRepeatDraftCommand(
        owner_id=owner_id,
        transaction_id=snapshot.transaction_id,
        expected_version=snapshot.version,
        occurred_at=datetime(2026, 8, 14, 12, tzinfo=UTC),
    )

    active = await use_cases.prepare_repeat(command)

    assert active.state == "quick_confirm"
    assert active.revision == 1
    assert active.payload["flow"] == "repeat"
    assert active.payload["amount_minor"] == snapshot.amount_minor
    assert commands.calls == [command]
    assert await drafts.get_active(owner_id) == active


def test_sensitive_transaction_values_are_hidden_from_command_and_result_repr() -> None:
    _use_cases, _drafts, _commands, snapshot = _harness()
    marker = "must-not-appear"
    edited = EditTransactionCommand(
        owner_id=uuid7(),
        transaction_id=snapshot.transaction_id,
        expected_version=1,
        amount_minor=999,
        description=marker,
    )
    result = TransactionMutationResult(
        entity_id=snapshot.transaction_id,
        version=snapshot.version,
        resulting_state="updated",
        transaction=snapshot,
    )

    assert marker not in repr(edited)
    assert "999" not in repr(edited)
    assert snapshot.description not in repr(result)
