from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fakes.repositories import InMemoryDraftRepository

from finbot.application.draft_navigation import DraftCatalogRef, DraftDateChoice
from finbot.application.dto import (
    DraftRef,
    OwnerSnapshot,
    TransactionMutationResult,
    TransactionSnapshot,
)
from finbot.application.errors import (
    DraftRevisionConflictError,
    InvalidStateError,
    ObjectVersionConflictError,
)
from finbot.application.transaction_draft_selection import (
    TransactionDraftSelectionAction,
    TransactionDraftSelectionCommand,
    TransactionDraftSelectionStatus,
    TransactionTypeSelection,
)
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.application.use_cases.transaction_draft_selection import (
    TransactionDraftSelectionUseCases,
)
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
DRAFT_ID = UUID("00000000-0000-7000-8000-000000000201")
TRANSACTION_ID = UUID("00000000-0000-7000-8000-000000000301")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000401")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000501")
SELECTED_ID = UUID("00000000-0000-7000-8000-000000000601")
NOW = datetime(2026, 8, 13, 15, 30, tzinfo=UTC)


def _owner() -> OwnerSnapshot:
    return OwnerSnapshot(
        owner_id=OWNER_ID,
        locale="ru_RU",
        timezone="Europe/Moscow",
        base_currency="RUB",
        default_account_id=ACCOUNT_ID,
    )


def _snapshot() -> TransactionSnapshot:
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
        version=2,
    )


class _Selections:
    def __init__(self) -> None:
        self.calls: list[tuple[TransactionDraftSelectionCommand, datetime | None]] = []
        self.error: Exception | None = None

    async def apply(
        self,
        command: TransactionDraftSelectionCommand,
        *,
        occurred_at: datetime | None = None,
    ) -> TransactionMutationResult:
        self.calls.append((command, occurred_at))
        if self.error is not None:
            raise self.error
        snapshot = _snapshot()
        return TransactionMutationResult(
            entity_id=snapshot.transaction_id,
            version=snapshot.version,
            resulting_state="updated",
            transaction=snapshot,
        )


class _Clock:
    def now(self, timezone: str) -> datetime:
        assert timezone == "Europe/Moscow"
        return NOW


async def _owner_query(owner_id: UUID) -> OwnerSnapshot:
    assert owner_id == OWNER_ID
    return _owner()


def _use_cases(
    drafts: InMemoryDraftRepository,
    selections: _Selections,
) -> TransactionDraftSelectionUseCases:
    return TransactionDraftSelectionUseCases(
        DraftUseCases(drafts),
        _owner_query,
        selections,
        _Clock(),
    )


async def _draft(
    drafts: InMemoryDraftRepository,
    state: str,
    *,
    history_page: object = 3,
) -> DraftRef:
    created = await drafts.create_if_absent(
        OWNER_ID,
        state,
        {
            "transaction_id": str(TRANSACTION_ID),
            "version": 1,
            "history_page": history_page,
        },
    )
    return created.ref


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "state", "choice"),
    [
        (
            TransactionDraftSelectionAction.TYPE,
            "edit_type",
            TransactionTypeSelection(
                TransactionType.INCOME,
                DraftCatalogRef(SELECTED_ID, 7),
            ),
        ),
        (
            TransactionDraftSelectionAction.CATEGORY,
            "edit_category",
            DraftCatalogRef(SELECTED_ID, 7),
        ),
        (
            TransactionDraftSelectionAction.ACCOUNT,
            "edit_account",
            DraftCatalogRef(SELECTED_ID, 7),
        ),
        (
            TransactionDraftSelectionAction.DATE,
            "edit_date_menu",
            DraftDateChoice.TODAY,
        ),
    ],
)
async def test_exact_state_matrix_delegates_one_atomic_transaction_edit(
    action: TransactionDraftSelectionAction,
    state: str,
    choice: DraftCatalogRef | DraftDateChoice | TransactionTypeSelection,
) -> None:
    drafts = InMemoryDraftRepository()
    selections = _Selections()
    expected = await _draft(drafts, state)
    command = TransactionDraftSelectionCommand(
        OWNER_ID,
        expected,
        action,
        choice,
    )

    result = await _use_cases(drafts, selections).execute(command)

    assert result.status is TransactionDraftSelectionStatus.TRANSACTION_UPDATED
    assert result.transaction == _snapshot()
    assert result.draft is None
    assert selections.calls[0][0] is command


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "choice"),
    [
        (
            TransactionDraftSelectionAction.CATEGORY,
            DraftCatalogRef(SELECTED_ID, 7),
        ),
        (
            TransactionDraftSelectionAction.ACCOUNT,
            DraftCatalogRef(SELECTED_ID, 7),
        ),
        (
            TransactionDraftSelectionAction.DATE,
            DraftDateChoice.TODAY,
        ),
    ],
)
async def test_wrong_state_is_rejected_before_repository_mutation(
    action: TransactionDraftSelectionAction,
    choice: DraftCatalogRef | DraftDateChoice,
) -> None:
    drafts = InMemoryDraftRepository()
    selections = _Selections()
    expected = await _draft(drafts, "edit_menu")

    with pytest.raises(InvalidStateError):
        await _use_cases(drafts, selections).execute(
            TransactionDraftSelectionCommand(OWNER_ID, expected, action, choice)
        )

    assert selections.calls == []


@pytest.mark.asyncio
async def test_pending_intent_is_rejected_before_repository_mutation() -> None:
    drafts = InMemoryDraftRepository()
    selections = _Selections()
    active = await drafts.create_if_absent(
        OWNER_ID,
        "edit_category",
        {
            "transaction_id": str(TRANSACTION_ID),
            "version": 1,
            "pending_intent": {"kind": "wizard"},
        },
    )

    with pytest.raises(InvalidStateError):
        await _use_cases(drafts, selections).execute(
            TransactionDraftSelectionCommand(
                OWNER_ID,
                active.ref,
                TransactionDraftSelectionAction.CATEGORY,
                DraftCatalogRef(SELECTED_ID, 7),
            )
        )

    assert selections.calls == []
    assert await drafts.get_active(OWNER_ID) == active


@pytest.mark.asyncio
async def test_stale_and_suspended_drafts_fail_closed() -> None:
    drafts = InMemoryDraftRepository()
    selections = _Selections()
    expected = await _draft(drafts, "edit_category")
    command = TransactionDraftSelectionCommand(
        OWNER_ID,
        DraftRef(expected.draft_id, expected.revision + 1),
        TransactionDraftSelectionAction.CATEGORY,
        DraftCatalogRef(SELECTED_ID, 7),
    )

    with pytest.raises(DraftRevisionConflictError):
        await _use_cases(drafts, selections).execute(command)

    suspended = await drafts.set_suspended(OWNER_ID, expected, True)
    with pytest.raises(InvalidStateError):
        await _use_cases(drafts, selections).execute(
            TransactionDraftSelectionCommand(
                OWNER_ID,
                suspended.ref,
                TransactionDraftSelectionAction.CATEGORY,
                DraftCatalogRef(SELECTED_ID, 7),
            )
        )
    assert selections.calls == []


@pytest.mark.asyncio
async def test_custom_date_only_advances_the_exact_draft_to_text_input() -> None:
    drafts = InMemoryDraftRepository()
    selections = _Selections()
    expected = await _draft(drafts, "edit_date_menu")

    result = await _use_cases(drafts, selections).execute(
        TransactionDraftSelectionCommand(
            OWNER_ID,
            expected,
            TransactionDraftSelectionAction.DATE,
            DraftDateChoice.CUSTOM,
        )
    )

    assert result.status is TransactionDraftSelectionStatus.DATE_INPUT_REQUIRED
    assert result.transaction is None
    assert result.draft is not None
    assert result.draft.state == "edit_date"
    assert result.draft.revision == expected.revision + 1
    assert result.draft.payload["transaction_id"] == str(TRANSACTION_ID)
    assert selections.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choice", "expected_delta"),
    [(DraftDateChoice.TODAY, timedelta()), (DraftDateChoice.YESTERDAY, timedelta(days=-1))],
)
async def test_relative_date_is_resolved_from_the_owner_timezone_clock(
    choice: DraftDateChoice,
    expected_delta: timedelta,
) -> None:
    drafts = InMemoryDraftRepository()
    selections = _Selections()
    expected = await _draft(drafts, "edit_date_menu")

    await _use_cases(drafts, selections).execute(
        TransactionDraftSelectionCommand(
            OWNER_ID,
            expected,
            TransactionDraftSelectionAction.DATE,
            choice,
        )
    )

    occurred_at = selections.calls[0][1]
    assert occurred_at is not None
    assert occurred_at == NOW.astimezone(occurred_at.tzinfo) + expected_delta


@pytest.mark.asyncio
async def test_repository_version_conflict_propagates_and_leaves_draft_current() -> None:
    drafts = InMemoryDraftRepository()
    selections = _Selections()
    selections.error = ObjectVersionConflictError(current_version=8)
    expected = await _draft(drafts, "edit_account")

    with pytest.raises(ObjectVersionConflictError) as conflict:
        await _use_cases(drafts, selections).execute(
            TransactionDraftSelectionCommand(
                OWNER_ID,
                expected,
                TransactionDraftSelectionAction.ACCOUNT,
                DraftCatalogRef(SELECTED_ID, 7),
            )
        )

    assert conflict.value.current_version == 8
    active = await drafts.get_active(OWNER_ID)
    assert active is not None and active.ref == expected


def test_contracts_reject_mismatched_choices_and_hide_sensitive_repr() -> None:
    expected = DraftRef(DRAFT_ID, 5)
    reference = DraftCatalogRef(SELECTED_ID, 7)
    command = TransactionDraftSelectionCommand(
        OWNER_ID,
        expected,
        TransactionDraftSelectionAction.CATEGORY,
        reference,
    )

    with pytest.raises(TypeError):
        TransactionDraftSelectionCommand(
            OWNER_ID,
            expected,
            TransactionDraftSelectionAction.CATEGORY,
            DraftDateChoice.TODAY,
        )

    rendered = repr((command, reference, _snapshot()))
    for private in (
        str(OWNER_ID),
        str(DRAFT_ID),
        str(SELECTED_ID),
        str(TRANSACTION_ID),
        "12345",
        "Закрытый",
        "закрытое",
    ):
        assert private not in rendered
