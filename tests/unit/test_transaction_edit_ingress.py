from datetime import UTC, datetime
from uuid import UUID

import pytest
from fakes.repositories import InMemoryDraftRepository

from finbot.application.draft_conflicts import (
    PendingEditIntent,
    decode_pending_draft_intent,
)
from finbot.application.dto import (
    DraftSnapshot,
    OwnerSnapshot,
    TransactionSnapshot,
)
from finbot.application.errors import (
    ApplicationValidationError,
    DraftRevisionConflictError,
    InvalidStateError,
    ObjectVersionConflictError,
)
from finbot.application.transaction_edit_ingress import (
    BeginTransactionEditCommand,
    BeginTransactionEditResult,
    TransactionEditIngressStatus,
)
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.application.use_cases.transaction_edit_ingress import BeginTransactionEdit
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000401")
TRANSACTION_ID = UUID("00000000-0000-7000-8000-000000000501")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000601")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000701")
NOW = datetime(2026, 8, 13, 9, 30, tzinfo=UTC)


def _owner() -> OwnerSnapshot:
    return OwnerSnapshot(OWNER_ID, "ru", "Europe/Moscow", "RUB", ACCOUNT_ID)


def _transaction(
    *,
    transaction_id: UUID = TRANSACTION_ID,
    version: int = 4,
) -> TransactionSnapshot:
    return TransactionSnapshot(
        transaction_id=transaction_id,
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
        version=version,
    )


class _Readers:
    def __init__(self, transaction: TransactionSnapshot | None = None) -> None:
        self.transaction = transaction or _transaction()
        self.events: list[str] = []
        self.target_error: Exception | None = None

    async def target(
        self,
        owner_id: UUID,
        transaction_id: UUID,
        expected_version: int,
    ) -> TransactionSnapshot:
        assert (owner_id, transaction_id, expected_version) == (
            OWNER_ID,
            TRANSACTION_ID,
            4,
        )
        self.events.append("target")
        if self.target_error is not None:
            raise self.target_error
        return self.transaction

    async def owner(self, owner_id: UUID) -> OwnerSnapshot:
        assert owner_id == OWNER_ID
        self.events.append("owner")
        return _owner()


def _use_case(
    repository: InMemoryDraftRepository,
    readers: _Readers,
) -> BeginTransactionEdit:
    return BeginTransactionEdit(DraftUseCases(repository), readers.owner, readers.target)


@pytest.mark.asyncio
async def test_begin_creates_canonical_channel_neutral_edit_draft() -> None:
    repository = InMemoryDraftRepository()
    readers = _Readers()

    result = await _use_case(repository, readers).execute(
        BeginTransactionEditCommand(OWNER_ID, TRANSACTION_ID, 4)
    )

    assert result.status is TransactionEditIngressStatus.DRAFT_CREATED
    assert result.transaction is readers.transaction
    assert result.owner == _owner()
    assert result.draft.state == "edit_menu"
    assert result.draft.payload == {
        "transaction_id": str(TRANSACTION_ID),
        "version": 4,
    }
    assert result.draft.revision == 1
    assert result.draft.suspended is False
    assert "history_page" not in result.draft.payload
    assert "ui_message_id" not in result.draft.payload
    assert readers.events == ["target", "owner"]


@pytest.mark.asyncio
@pytest.mark.parametrize("suspended", [False, True])
async def test_active_draft_stages_only_typed_edit_intent_and_preserves_suspension(
    suspended: bool,
) -> None:
    repository = InMemoryDraftRepository()
    original = await repository.create_if_absent(
        OWNER_ID,
        "wizard_amount",
        {
            "flow": "wizard",
            "history_page": 9,
        },
    )
    if suspended:
        original = await repository.set_suspended(OWNER_ID, original.ref, True)
    readers = _Readers()

    result = await _use_case(repository, readers).execute(
        BeginTransactionEditCommand(OWNER_ID, TRANSACTION_ID, 4)
    )

    assert result.status is TransactionEditIngressStatus.CONFLICT_STAGED
    assert result.draft.draft_id == original.draft_id
    assert result.draft.revision == original.revision + 1
    assert result.draft.state == original.state
    assert result.draft.suspended is suspended
    assert result.draft.payload["flow"] == "wizard"
    assert "history_page" not in result.draft.payload
    assert decode_pending_draft_intent(result.draft.payload["pending_intent"]) == (
        PendingEditIntent(TRANSACTION_ID, 4)
    )
    pending = result.draft.payload["pending_intent"]
    assert isinstance(pending, dict)
    assert set(pending) == {"kind", "transaction_id", "version"}
    assert "history_page" not in pending
    assert "ui_message_id" not in pending


@pytest.mark.asyncio
async def test_existing_pending_intent_fails_closed_without_overwriting_user_choice() -> None:
    repository = InMemoryDraftRepository()
    original = await repository.create_if_absent(
        OWNER_ID,
        "wizard_amount",
        {
            "flow": "wizard",
            "pending_intent": {"kind": "quick", "text": "old bounded intent"},
        },
    )

    with pytest.raises(InvalidStateError, match="Сначала завершите выбор"):
        await _use_case(repository, _Readers()).execute(
            BeginTransactionEditCommand(OWNER_ID, TRANSACTION_ID, 4)
        )

    current = await repository.get_active(OWNER_ID)
    assert current is not None
    assert current.ref == original.ref
    assert current.payload == original.payload


@pytest.mark.asyncio
async def test_stale_target_fails_before_draft_or_owner_read() -> None:
    repository = InMemoryDraftRepository()
    readers = _Readers()
    readers.target_error = ObjectVersionConflictError(current_version=5)

    with pytest.raises(ObjectVersionConflictError) as conflict:
        await _use_case(repository, readers).execute(
            BeginTransactionEditCommand(OWNER_ID, TRANSACTION_ID, 4)
        )

    assert conflict.value.current_version == 5
    assert await repository.get_active(OWNER_ID) is None
    assert readers.events == ["target"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transaction",
    [
        _transaction(transaction_id=UUID("00000000-0000-7000-8000-000000000999")),
        _transaction(version=5),
    ],
)
async def test_non_authoritative_target_result_fails_closed(
    transaction: TransactionSnapshot,
) -> None:
    repository = InMemoryDraftRepository()
    readers = _Readers(transaction)

    with pytest.raises(ApplicationValidationError):
        await _use_case(repository, readers).execute(
            BeginTransactionEditCommand(OWNER_ID, TRANSACTION_ID, 4)
        )

    assert await repository.get_active(OWNER_ID) is None
    assert readers.events == ["target"]


class _RacingDraftRepository(InMemoryDraftRepository):
    def __init__(self) -> None:
        super().__init__()
        self.raced = False

    async def get_active(self, owner_id: UUID) -> DraftSnapshot | None:
        current = await super().get_active(owner_id)
        if current is not None and not self.raced:
            self.raced = True
            await super().update(
                owner_id,
                current.ref,
                current.state,
                {"flow": "winner"},
            )
        return current


@pytest.mark.asyncio
async def test_active_draft_cas_race_does_not_overwrite_winner() -> None:
    repository = _RacingDraftRepository()
    initial = await repository.create_if_absent(OWNER_ID, "wizard_type", {"flow": "wizard"})

    with pytest.raises(DraftRevisionConflictError):
        await _use_case(repository, _Readers()).execute(
            BeginTransactionEditCommand(OWNER_ID, TRANSACTION_ID, 4)
        )

    current = await repository.get_active(OWNER_ID)
    assert current is not None
    assert current.draft_id == initial.draft_id
    assert current.revision == initial.revision + 1
    assert current.payload == {"flow": "winner"}


def test_contracts_reject_invalid_values_and_hide_private_repr() -> None:
    command = BeginTransactionEditCommand(OWNER_ID, TRANSACTION_ID, 4)
    result = BeginTransactionEditResult(
        TransactionEditIngressStatus.DRAFT_CREATED,
        _owner(),
        _transaction(),
        DraftSnapshot(
            DRAFT_ID,
            "edit_menu",
            {"transaction_id": str(TRANSACTION_ID), "version": 4},
        ),
    )

    with pytest.raises(TypeError):
        BeginTransactionEditCommand(  # type: ignore[arg-type]
            OWNER_ID,
            str(TRANSACTION_ID),
            4,
        )
    with pytest.raises(ValueError):
        BeginTransactionEditCommand(OWNER_ID, TRANSACTION_ID, 0)

    rendered = repr((command, result))
    for private in (
        str(OWNER_ID),
        str(DRAFT_ID),
        str(TRANSACTION_ID),
        str(ACCOUNT_ID),
        str(CATEGORY_ID),
        "12345",
        "RUB",
        "Закрытый",
        "закрытое",
    ):
        assert private not in rendered
