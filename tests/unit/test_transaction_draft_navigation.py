from datetime import UTC, datetime
from uuid import UUID

import pytest
from fakes.repositories import InMemoryDraftRepository

from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    DraftRef,
    OwnerSnapshot,
    TransactionSnapshot,
    UpdateDraftCommand,
)
from finbot.application.errors import (
    ApplicationValidationError,
    DraftRevisionConflictError,
    EntityNotFoundError,
    InvalidStateError,
    ObjectVersionConflictError,
)
from finbot.application.transaction_draft_navigation import (
    TRANSACTION_DRAFT_NAVIGATION_ALLOWED_STATES,
    TRANSACTION_DRAFT_NAVIGATION_TARGET_STATES,
    TransactionDraftNavigationAction,
    TransactionDraftNavigationCommand,
)
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.application.use_cases.transaction_draft_navigation import (
    TransactionDraftNavigationUseCases,
)
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
    version: int = 4,
    deleted_at: datetime | None = None,
    transaction_id: UUID = TRANSACTION_ID,
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
        deleted_at=deleted_at,
        version=version,
    )


def _account() -> AccountSnapshot:
    return AccountSnapshot(ACCOUNT_ID, "Закрытый счёт", "cash", "RUB", None, 3)


def _category() -> CategorySnapshot:
    return CategorySnapshot(
        CATEGORY_ID,
        TransactionType.EXPENSE,
        "Закрытая категория",
        "▫️",
        None,
        5,
    )


class _Queries:
    def __init__(self, transaction: TransactionSnapshot | None = None) -> None:
        self.transaction = transaction or _transaction()
        self.owner_calls: list[UUID] = []
        self.transaction_calls: list[tuple[UUID, UUID]] = []
        self.account_calls: list[tuple[UUID, bool]] = []
        self.category_calls: list[tuple[UUID, TransactionType | str | None, bool]] = []
        self.transaction_error: Exception | None = None
        self.owner_error: Exception | None = None

    async def owner(self, owner_id: UUID) -> OwnerSnapshot:
        self.owner_calls.append(owner_id)
        if self.owner_error is not None:
            raise self.owner_error
        return _owner()

    async def get_transaction(
        self,
        owner_id: UUID,
        transaction_id: UUID,
    ) -> TransactionSnapshot:
        self.transaction_calls.append((owner_id, transaction_id))
        if self.transaction_error is not None:
            raise self.transaction_error
        return self.transaction

    async def accounts(
        self,
        owner_id: UUID,
        *,
        archived: bool = False,
    ) -> tuple[AccountSnapshot, ...]:
        self.account_calls.append((owner_id, archived))
        return (_account(),)

    async def categories(
        self,
        owner_id: UUID,
        *,
        kind: TransactionType | str | None = None,
        archived: bool = False,
    ) -> tuple[CategorySnapshot, ...]:
        self.category_calls.append((owner_id, kind, archived))
        return (_category(),)


def _use_cases(
    drafts: InMemoryDraftRepository,
    queries: _Queries,
) -> TransactionDraftNavigationUseCases:
    return TransactionDraftNavigationUseCases(
        DraftUseCases(drafts),
        queries.owner,
        queries.get_transaction,
        queries.accounts,
        queries.categories,
    )


async def _draft(
    drafts: InMemoryDraftRepository,
    state: str,
    *,
    version: object = 4,
    history_page: object = 3,
    transaction_id: object = TRANSACTION_ID,
) -> DraftRef:
    created = await drafts.create_if_absent(
        OWNER_ID,
        state,
        {
            "transaction_id": str(transaction_id),
            "version": version,
            "history_page": history_page,
        },
    )
    return created.ref


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "source_state", "target_state"),
    [
        (TransactionDraftNavigationAction.EDIT_TYPE, "edit_menu", "edit_type"),
        (TransactionDraftNavigationAction.EDIT_AMOUNT, "edit_menu", "edit_amount"),
        (TransactionDraftNavigationAction.EDIT_CATEGORY, "edit_menu", "edit_category"),
        (TransactionDraftNavigationAction.EDIT_ACCOUNT, "edit_menu", "edit_account"),
        (TransactionDraftNavigationAction.EDIT_DATE, "edit_menu", "edit_date_menu"),
        (
            TransactionDraftNavigationAction.EDIT_DESCRIPTION,
            "edit_menu",
            "edit_description",
        ),
        (TransactionDraftNavigationAction.DATE_BACK, "edit_date", "edit_date_menu"),
        (TransactionDraftNavigationAction.BACK, "edit_type", "edit_menu"),
        (TransactionDraftNavigationAction.BACK, "edit_amount", "edit_menu"),
        (TransactionDraftNavigationAction.BACK, "edit_category", "edit_menu"),
        (TransactionDraftNavigationAction.BACK, "edit_account", "edit_menu"),
        (TransactionDraftNavigationAction.BACK, "edit_date_menu", "edit_menu"),
        (TransactionDraftNavigationAction.BACK, "edit_description", "edit_menu"),
    ],
)
async def test_exact_state_action_matrix_updates_only_the_exact_draft(
    action: TransactionDraftNavigationAction,
    source_state: str,
    target_state: str,
) -> None:
    drafts = InMemoryDraftRepository()
    queries = _Queries()
    expected = await _draft(drafts, source_state)

    result = await _use_cases(drafts, queries).execute(
        TransactionDraftNavigationCommand(OWNER_ID, expected, action)
    )

    assert result.action is action
    assert result.draft.state == target_state
    assert result.draft.revision == expected.revision + 1
    assert result.draft.payload == {
        "transaction_id": str(TRANSACTION_ID),
        "version": 4,
    }
    assert result.transaction is queries.transaction
    assert queries.transaction == _transaction()
    assert queries.owner_calls == [OWNER_ID]


def test_state_contract_is_exhaustive_and_maps_every_action_to_a_target() -> None:
    assert set(TRANSACTION_DRAFT_NAVIGATION_ALLOWED_STATES) == set(TransactionDraftNavigationAction)
    assert set(TRANSACTION_DRAFT_NAVIGATION_TARGET_STATES) == set(TransactionDraftNavigationAction)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", list(TransactionDraftNavigationAction))
async def test_every_action_rejects_a_state_outside_its_closed_matrix(
    action: TransactionDraftNavigationAction,
) -> None:
    drafts = InMemoryDraftRepository()
    queries = _Queries()
    expected = await _draft(drafts, "unrelated")

    with pytest.raises(InvalidStateError):
        await _use_cases(drafts, queries).execute(
            TransactionDraftNavigationCommand(OWNER_ID, expected, action)
        )

    assert queries.transaction_calls == []
    assert queries.owner_calls == []


@pytest.mark.asyncio
async def test_category_and_account_edits_capture_active_typed_choice_snapshots() -> None:
    category_drafts = InMemoryDraftRepository()
    category_queries = _Queries()
    category_expected = await _draft(category_drafts, "edit_menu")

    category_result = await _use_cases(category_drafts, category_queries).execute(
        TransactionDraftNavigationCommand(
            OWNER_ID,
            category_expected,
            TransactionDraftNavigationAction.EDIT_CATEGORY,
        )
    )

    assert category_result.choices.categories == (_category(),)
    assert category_result.choices.accounts == ()
    assert category_queries.category_calls == [(OWNER_ID, TransactionType.EXPENSE, False)]
    assert category_queries.account_calls == []

    account_drafts = InMemoryDraftRepository()
    account_queries = _Queries()
    account_expected = await _draft(account_drafts, "edit_menu")
    account_result = await _use_cases(account_drafts, account_queries).execute(
        TransactionDraftNavigationCommand(
            OWNER_ID,
            account_expected,
            TransactionDraftNavigationAction.EDIT_ACCOUNT,
        )
    )

    assert account_result.choices.accounts == (_account(),)
    assert account_result.choices.categories == ()
    assert account_queries.account_calls == [(OWNER_ID, False)]
    assert account_queries.category_calls == []


@pytest.mark.asyncio
async def test_edit_action_rejects_a_changed_transaction_version_without_advancing_draft() -> None:
    drafts = InMemoryDraftRepository()
    queries = _Queries(_transaction(version=8))
    expected = await _draft(drafts, "edit_menu", version=7)

    with pytest.raises(ObjectVersionConflictError) as conflict:
        await _use_cases(drafts, queries).execute(
            TransactionDraftNavigationCommand(
                OWNER_ID,
                expected,
                TransactionDraftNavigationAction.EDIT_AMOUNT,
            )
        )

    assert conflict.value.current_version == 8
    current = await drafts.get_active(OWNER_ID)
    assert current is not None and current.ref == expected
    assert queries.owner_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "state"),
    [
        (TransactionDraftNavigationAction.DATE_BACK, "edit_date"),
        (TransactionDraftNavigationAction.BACK, "edit_description"),
    ],
)
async def test_back_paths_refresh_the_current_active_transaction_version(
    action: TransactionDraftNavigationAction,
    state: str,
) -> None:
    drafts = InMemoryDraftRepository()
    queries = _Queries(_transaction(version=11))
    expected = await _draft(drafts, state, version="broken")

    result = await _use_cases(drafts, queries).execute(
        TransactionDraftNavigationCommand(OWNER_ID, expected, action)
    )

    assert result.transaction.version == 11
    assert result.draft.payload["version"] == 11


@pytest.mark.asyncio
async def test_stale_missing_and_suspended_drafts_fail_before_queries() -> None:
    drafts = InMemoryDraftRepository()
    queries = _Queries()
    expected = await _draft(drafts, "edit_menu")

    with pytest.raises(DraftRevisionConflictError):
        await _use_cases(drafts, queries).execute(
            TransactionDraftNavigationCommand(
                OWNER_ID,
                DraftRef(expected.draft_id, expected.revision + 1),
                TransactionDraftNavigationAction.EDIT_AMOUNT,
            )
        )

    suspended = await drafts.set_suspended(OWNER_ID, expected, True)
    with pytest.raises(InvalidStateError):
        await _use_cases(drafts, queries).execute(
            TransactionDraftNavigationCommand(
                OWNER_ID,
                suspended.ref,
                TransactionDraftNavigationAction.EDIT_AMOUNT,
            )
        )

    missing_drafts = InMemoryDraftRepository()
    with pytest.raises(DraftRevisionConflictError):
        await _use_cases(missing_drafts, queries).execute(
            TransactionDraftNavigationCommand(
                OWNER_ID,
                expected,
                TransactionDraftNavigationAction.EDIT_AMOUNT,
            )
        )
    assert queries.transaction_calls == []


@pytest.mark.asyncio
async def test_deleted_missing_or_mismatched_transaction_fails_closed() -> None:
    for transaction, error in (
        (_transaction(deleted_at=NOW), InvalidStateError),
        (
            _transaction(transaction_id=UUID("00000000-0000-7000-8000-000000000999")),
            ApplicationValidationError,
        ),
    ):
        drafts = InMemoryDraftRepository()
        queries = _Queries(transaction)
        expected = await _draft(drafts, "edit_menu")
        with pytest.raises(error):
            await _use_cases(drafts, queries).execute(
                TransactionDraftNavigationCommand(
                    OWNER_ID,
                    expected,
                    TransactionDraftNavigationAction.EDIT_AMOUNT,
                )
            )
        current = await drafts.get_active(OWNER_ID)
        assert current is not None and current.ref == expected

    drafts = InMemoryDraftRepository()
    queries = _Queries()
    queries.transaction_error = EntityNotFoundError("Операция не найдена")
    expected = await _draft(drafts, "edit_menu")
    with pytest.raises(EntityNotFoundError):
        await _use_cases(drafts, queries).execute(
            TransactionDraftNavigationCommand(
                OWNER_ID,
                expected,
                TransactionDraftNavigationAction.EDIT_AMOUNT,
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("transaction_id", "broken"),
        ("version", True),
        ("version", 0),
        ("version", "broken"),
    ],
)
async def test_malformed_edit_context_is_rejected_without_advancing_draft(
    field: str,
    value: object,
) -> None:
    drafts = InMemoryDraftRepository()
    queries = _Queries()
    values: dict[str, object] = {
        "transaction_id": TRANSACTION_ID,
        "version": 4,
        "history_page": 3,
    }
    values[field] = value
    expected = await _draft(
        drafts,
        "edit_menu",
        transaction_id=values["transaction_id"],
        version=values["version"],
        history_page=values["history_page"],
    )

    with pytest.raises(ApplicationValidationError):
        await _use_cases(drafts, queries).execute(
            TransactionDraftNavigationCommand(
                OWNER_ID,
                expected,
                TransactionDraftNavigationAction.EDIT_AMOUNT,
            )
        )
    current = await drafts.get_active(OWNER_ID)
    assert current is not None and current.ref == expected


@pytest.mark.asyncio
async def test_cas_conflict_after_snapshot_read_does_not_overwrite_newer_draft() -> None:
    drafts = InMemoryDraftRepository()
    queries = _Queries()
    expected = await _draft(drafts, "edit_menu")

    async def racing_transaction(
        owner_id: UUID,
        transaction_id: UUID,
    ) -> TransactionSnapshot:
        assert (owner_id, transaction_id) == (OWNER_ID, TRANSACTION_ID)
        await DraftUseCases(drafts).update(
            UpdateDraftCommand(
                OWNER_ID,
                expected,
                "edit_menu",
                {
                    "transaction_id": str(TRANSACTION_ID),
                    "version": 4,
                    "history_page": 9,
                },
            )
        )
        return _transaction()

    use_cases = TransactionDraftNavigationUseCases(
        DraftUseCases(drafts),
        queries.owner,
        racing_transaction,
        queries.accounts,
        queries.categories,
    )
    with pytest.raises(DraftRevisionConflictError):
        await use_cases.execute(
            TransactionDraftNavigationCommand(
                OWNER_ID,
                expected,
                TransactionDraftNavigationAction.EDIT_AMOUNT,
            )
        )

    current = await drafts.get_active(OWNER_ID)
    assert current is not None
    assert current.revision == expected.revision + 1
    assert current.payload["history_page"] == 9
    assert queries.owner_calls == [OWNER_ID]


@pytest.mark.asyncio
async def test_all_fallible_queries_complete_before_the_draft_is_advanced() -> None:
    drafts = InMemoryDraftRepository()
    queries = _Queries()
    queries.owner_error = EntityNotFoundError("Владелец не найден")
    expected = await _draft(drafts, "edit_menu")

    with pytest.raises(EntityNotFoundError):
        await _use_cases(drafts, queries).execute(
            TransactionDraftNavigationCommand(
                OWNER_ID,
                expected,
                TransactionDraftNavigationAction.EDIT_AMOUNT,
            )
        )

    current = await drafts.get_active(OWNER_ID)
    assert current is not None and current.ref == expected


def test_contracts_reject_invalid_values_and_hide_sensitive_repr() -> None:
    expected = DraftRef(DRAFT_ID, 7)
    command = TransactionDraftNavigationCommand(
        OWNER_ID,
        expected,
        TransactionDraftNavigationAction.EDIT_AMOUNT,
    )

    with pytest.raises(TypeError):
        TransactionDraftNavigationCommand(
            OWNER_ID,
            expected,
            "edit_amount",
        )
    rendered = repr((command, _owner(), _transaction(), _account(), _category()))
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
