from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import uuid7

import pytest
from fakes.repositories import InMemoryDraftRepository

from finbot.application.dto import (
    AccountSnapshot,
    CategoryTotalSnapshot,
    CurrencyTotals,
    DraftSnapshot,
    TransactionSnapshot,
)
from finbot.application.errors import (
    ActiveDraftConflictError,
    ApplicationErrorCode,
    DraftRevisionConflictError,
)
from finbot.application.interactions import DraftAction, DraftInteraction
from finbot.application.services.transactions import TransactionPatch
from finbot.domain.transactions import TransactionDraft, TransactionType


def test_application_errors_expose_stable_codes_and_bounded_details() -> None:
    error = DraftRevisionConflictError(current_revision=12)

    assert error.code is ApplicationErrorCode.DRAFT_REVISION_CONFLICT
    assert error.details == {"current_revision": 12}
    assert "owner" not in error.details


def test_draft_snapshot_is_immutable_and_hides_payload_from_repr() -> None:
    payload = {"state": "review"}
    snapshot = DraftSnapshot(draft_id=uuid7(), state="review", payload=payload)
    payload["state"] = "mutated"

    assert snapshot.payload == {"state": "review"}
    assert "payload" not in repr(snapshot)
    with pytest.raises(TypeError):
        snapshot.payload["state"] = "changed"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        snapshot.state = "changed"  # type: ignore[misc]


def test_financial_domain_and_application_values_are_hidden_from_repr() -> None:
    account_id = uuid7()
    category_id = uuid7()
    transaction = TransactionSnapshot(
        transaction_id=uuid7(),
        kind=TransactionType.EXPENSE,
        amount_minor=98_765,
        currency="RUB",
        account_id=account_id,
        account_name="private-account-marker",
        category_id=category_id,
        category_name="private-category-marker",
        category_emoji="x",
        occurred_at=datetime(2026, 8, 13, tzinfo=UTC),
        description="private-description-marker",
    )
    values = (
        TransactionDraft(
            amount_minor=98_765,
            type=TransactionType.EXPENSE,
            description="private-description-marker",
        ),
        transaction,
        AccountSnapshot(
            account_id,
            "private-account-marker",
            "card",
            "RUB",
            None,
            1,
        ),
        CurrencyTotals("RUB", 12_345, 98_765),
        CategoryTotalSnapshot(
            category_id,
            "private-category-marker",
            "x",
            "RUB",
            98_765,
        ),
        TransactionPatch(
            transaction_id=transaction.transaction_id,
            version=7,
            amount_minor=98_765,
            account_id=account_id,
            category_id=category_id,
            description="private-description-marker",
        ),
        DraftInteraction(
            action=DraftAction.TX_SELECT_CATEGORY,
            draft_id=transaction.transaction_id,
            revision=7,
            object_id=category_id,
            object_version=3,
        ),
    )

    for value in values:
        rendered = repr(value)
        assert "98_765" not in rendered
        assert "98765" not in rendered
        assert "private-" not in rendered
        assert str(account_id) not in rendered
        assert str(category_id) not in rendered


@pytest.mark.parametrize("key", ["ui_message_id", "presentation_ref"])
def test_draft_snapshot_rejects_channel_presentation_state(key: str) -> None:
    with pytest.raises(ValueError, match="presentation state"):
        DraftSnapshot(draft_id=uuid7(), state="review", payload={key: "adapter-only"})


@pytest.mark.asyncio
async def test_in_memory_draft_repository_enforces_single_active_and_cas() -> None:
    owner_id = uuid7()
    repository = InMemoryDraftRepository()
    created = await repository.create_if_absent(owner_id, "review", {"value": 1})

    with pytest.raises(ActiveDraftConflictError) as conflict:
        await repository.create_if_absent(owner_id, "review", {})
    assert conflict.value.details == {"current_revision": 1}

    updated = await repository.update(owner_id, created.ref, "review", {"value": 2})
    assert updated.draft_id == created.draft_id
    assert updated.revision == 2
    with pytest.raises(DraftRevisionConflictError):
        await repository.update(owner_id, created.ref, "review", {})

    replacement = await repository.replace(owner_id, updated.ref, "amount", {})
    assert replacement.draft_id != updated.draft_id
    assert replacement.revision == 1


@pytest.mark.asyncio
async def test_in_memory_draft_repository_copies_payloads_at_boundaries() -> None:
    owner_id = uuid7()
    repository = InMemoryDraftRepository()
    payload: dict[str, object] = {"nested": {"value": 1}}
    created = await repository.create_if_absent(owner_id, "review", payload)
    nested = payload["nested"]
    assert isinstance(nested, dict)
    nested["value"] = 2

    loaded = await repository.get_active(owner_id)
    assert loaded is not None
    assert loaded.payload == {"nested": {"value": 1}}
    assert created.payload == loaded.payload
