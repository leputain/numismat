from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from fakes.repositories import InMemoryDraftRepository

from finbot.application.catalogs import (
    CatalogCommandRepository,
    CreateAccountCommand,
    CreateCategoryCommand,
    UpdateAccountCommand,
    UpdateCategoryCommand,
)
from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    DraftSnapshot,
    OwnerSnapshot,
)
from finbot.application.errors import ApplicationValidationError, InvalidStateError
from finbot.application.settings_text_input import (
    SettingsTextInputCommand,
    SettingsTextInputError,
    SettingsTextInputOperation,
    SettingsTextInputStatus,
    SettingsTextInputTarget,
    SettingsTextInputTargetRepository,
)
from finbot.application.use_cases.catalogs import CatalogUseCases
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.application.use_cases.settings_text_input import SettingsTextInputUseCase
from finbot.domain.transactions import TransactionType

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000201")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000301")


def _owner(default_account_id: UUID | None = ACCOUNT_ID) -> OwnerSnapshot:
    return OwnerSnapshot(
        OWNER_ID,
        "ru",
        "Europe/Moscow",
        "RUB",
        default_account_id,
    )


def _account(*, name: str = "Закрытый счёт", version: int = 4) -> AccountSnapshot:
    return AccountSnapshot(ACCOUNT_ID, name, "other", "RUB", None, version)


def _category(*, name: str = "Закрытая категория", version: int = 3) -> CategorySnapshot:
    return CategorySnapshot(
        CATEGORY_ID,
        TransactionType.EXPENSE,
        name,
        "▫️",
        None,
        version,
    )


class _Targets:
    def __init__(
        self,
        draft: DraftSnapshot,
        *,
        owner: OwnerSnapshot | None = None,
        account: AccountSnapshot | None = None,
        category: CategorySnapshot | None = None,
    ) -> None:
        self.target = SettingsTextInputTarget(
            owner or _owner(),
            draft,
            account,
            category,
        )
        self.accounts: dict[UUID, AccountSnapshot] = (
            {account.account_id: account} if account is not None else {}
        )
        self.categories: dict[UUID, CategorySnapshot] = (
            {category.category_id: category} if category is not None else {}
        )

    async def lock_target(self, owner_id: UUID, expected: object) -> SettingsTextInputTarget:
        assert (owner_id, expected) == (OWNER_ID, self.target.draft.ref)
        return self.target

    async def get_account(
        self,
        owner_id: UUID,
        account_id: UUID,
    ) -> AccountSnapshot | None:
        assert owner_id == OWNER_ID
        return self.accounts.get(account_id)

    async def get_category(
        self,
        owner_id: UUID,
        category_id: UUID,
    ) -> CategorySnapshot | None:
        assert owner_id == OWNER_ID
        return self.categories.get(category_id)


class _Catalogs:
    def __init__(self, targets: _Targets) -> None:
        self.targets = targets
        self.calls: list[object] = []

    async def create_account(self, command: CreateAccountCommand) -> AccountSnapshot:
        self.calls.append(command)
        account = _account(name=command.name, version=1)
        self.targets.accounts[account.account_id] = account
        return account

    async def set_default_account(self, command: object) -> AccountSnapshot:
        self.calls.append(command)
        current = self.targets.accounts[ACCOUNT_ID]
        account = AccountSnapshot(
            current.account_id,
            current.name,
            current.account_type,
            current.currency,
            None,
            current.version + 1,
        )
        self.targets.accounts[ACCOUNT_ID] = account
        return account

    async def update_account(self, command: UpdateAccountCommand) -> AccountSnapshot:
        self.calls.append(command)
        account = _account(name=command.name, version=command.expected_version + 1)
        self.targets.accounts[account.account_id] = account
        return account

    async def create_category(self, command: CreateCategoryCommand) -> CategorySnapshot:
        self.calls.append(command)
        category = _category(name=command.name, version=1)
        self.targets.categories[category.category_id] = category
        return category

    async def update_category(self, command: UpdateCategoryCommand) -> CategorySnapshot:
        self.calls.append(command)
        category = _category(name=command.name, version=command.expected_version + 1)
        self.targets.categories[category.category_id] = category
        return category


class _InvalidCatalogs(_Catalogs):
    async def update_account(self, command: UpdateAccountCommand) -> AccountSnapshot:
        self.calls.append(command)
        raise ApplicationValidationError("owner-submitted secret")


async def _subject(
    state: str,
    payload: dict[str, object],
    *,
    owner: OwnerSnapshot | None = None,
    account: AccountSnapshot | None = None,
    category: CategorySnapshot | None = None,
    invalid_catalogs: bool = False,
) -> tuple[
    SettingsTextInputUseCase,
    DraftSnapshot,
    InMemoryDraftRepository,
    _Targets,
    _Catalogs,
]:
    repository = InMemoryDraftRepository()
    draft = await repository.create_if_absent(OWNER_ID, state, payload)
    targets = _Targets(
        draft,
        owner=owner,
        account=account,
        category=category,
    )
    catalogs = _InvalidCatalogs(targets) if invalid_catalogs else _Catalogs(targets)
    use_case = SettingsTextInputUseCase(
        cast(SettingsTextInputTargetRepository, targets),
        CatalogUseCases(cast(CatalogCommandRepository, catalogs)),
        DraftUseCases(repository),
    )
    return use_case, draft, repository, targets, catalogs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "payload", "account", "category", "operation"),
    [
        (
            "settings_account_create",
            {},
            None,
            None,
            SettingsTextInputOperation.ACCOUNT_CREATE,
        ),
        (
            "settings_account_rename",
            {"account_id": str(ACCOUNT_ID), "object_version": 4},
            _account(),
            None,
            SettingsTextInputOperation.ACCOUNT_RENAME,
        ),
        (
            "settings_category_create",
            {"kind": "expense"},
            None,
            None,
            SettingsTextInputOperation.CATEGORY_CREATE,
        ),
        (
            "settings_category_rename",
            {
                "category_id": str(CATEGORY_ID),
                "kind": "expense",
                "object_version": 3,
            },
            None,
            _category(),
            SettingsTextInputOperation.CATEGORY_RENAME,
        ),
    ],
)
async def test_valid_input_mutates_catalog_and_deletes_exact_draft(
    state: str,
    payload: dict[str, object],
    account: AccountSnapshot | None,
    category: CategorySnapshot | None,
    operation: SettingsTextInputOperation,
) -> None:
    use_case, draft, repository, _targets, catalogs = await _subject(
        state,
        payload,
        account=account,
        category=category,
    )

    result = await use_case.execute(SettingsTextInputCommand(OWNER_ID, draft.ref, " Новое имя "))

    assert result.operation is operation
    assert result.status is SettingsTextInputStatus.UPDATED
    assert result.draft is None
    assert result.retry_error is None
    assert await repository.get_active(OWNER_ID) is None
    assert len(catalogs.calls) == 1
    mutated = result.account or result.category
    assert mutated is not None
    assert mutated.name == "Новое имя"


@pytest.mark.asyncio
async def test_first_created_account_becomes_default_in_same_use_case() -> None:
    use_case, draft, repository, _targets, catalogs = await _subject(
        "settings_account_create",
        {},
        owner=_owner(None),
    )

    result = await use_case.execute(SettingsTextInputCommand(OWNER_ID, draft.ref, "Первый"))

    assert result.owner.default_account_id == ACCOUNT_ID
    assert result.account is not None and result.account.version == 2
    assert len(catalogs.calls) == 2
    assert await repository.get_active(OWNER_ID) is None


@pytest.mark.asyncio
async def test_invalid_name_keeps_catalog_and_advances_retry_draft_without_raw_text() -> None:
    use_case, draft, repository, _targets, catalogs = await _subject(
        "settings_account_rename",
        {"account_id": str(ACCOUNT_ID), "object_version": 4},
        account=_account(),
        invalid_catalogs=True,
    )

    result = await use_case.execute(
        SettingsTextInputCommand(OWNER_ID, draft.ref, "very-secret-invalid-name")
    )

    assert result.status is SettingsTextInputStatus.RETRY
    assert result.retry_error is SettingsTextInputError.INVALID_NAME
    assert result.draft is not None and result.draft.revision == draft.revision + 1
    assert dict(result.draft.payload) == dict(draft.payload)
    assert await repository.get_active(OWNER_ID) == result.draft
    assert len(catalogs.calls) == 1
    assert "very-secret-invalid-name" not in repr(result)


@pytest.mark.asyncio
async def test_stale_version_refreshes_retry_target_without_mutating_catalog() -> None:
    use_case, draft, repository, _targets, catalogs = await _subject(
        "settings_category_rename",
        {
            "category_id": str(CATEGORY_ID),
            "kind": "expense",
            "object_version": 2,
        },
        category=_category(version=3),
    )

    result = await use_case.execute(SettingsTextInputCommand(OWNER_ID, draft.ref, "Не применяется"))

    assert result.retry_error is SettingsTextInputError.VERSION_CONFLICT
    assert result.draft is not None
    assert result.draft.payload["object_version"] == 3
    assert await repository.get_active(OWNER_ID) == result.draft
    assert catalogs.calls == []


@pytest.mark.asyncio
async def test_pending_intent_is_rejected_before_catalog_mutation() -> None:
    repository = InMemoryDraftRepository()
    draft = await repository.create_if_absent(
        OWNER_ID,
        "settings_account_rename",
        {
            "account_id": str(ACCOUNT_ID),
            "object_version": 4,
            "pending_intent": {"kind": "wizard"},
        },
    )

    class _PendingTargets:
        def __init__(self, pending_draft: DraftSnapshot) -> None:
            self.draft = pending_draft

        async def lock_target(
            self,
            owner_id: UUID,
            expected: object,
        ) -> SettingsTextInputTarget:
            assert (owner_id, expected) == (OWNER_ID, draft.ref)
            # Exercise the use-case guard against a faulty/malicious port that
            # violates the target DTO's stricter canonical-payload invariant.
            return cast(SettingsTextInputTarget, self)

        async def get_account(
            self,
            owner_id: UUID,
            account_id: UUID,
        ) -> AccountSnapshot | None:
            raise AssertionError("pending draft must not query an account")

        async def get_category(
            self,
            owner_id: UUID,
            category_id: UUID,
        ) -> CategorySnapshot | None:
            raise AssertionError("pending draft must not query a category")

        owner = _owner()
        account = _account()
        category = None

    targets = _PendingTargets(draft)
    catalogs = _Catalogs(cast(_Targets, targets))
    use_case = SettingsTextInputUseCase(
        cast(SettingsTextInputTargetRepository, targets),
        CatalogUseCases(cast(CatalogCommandRepository, catalogs)),
        DraftUseCases(repository),
    )

    with pytest.raises(InvalidStateError):
        await use_case.execute(SettingsTextInputCommand(OWNER_ID, draft.ref, "Не применяется"))

    assert catalogs.calls == []
    assert await repository.get_active(OWNER_ID) == draft


def test_contracts_are_channel_neutral_and_hide_private_values_from_repr() -> None:
    draft = DraftSnapshot(
        UUID("00000000-0000-7000-8000-000000000401"),
        "settings_account_rename",
        {"account_id": str(ACCOUNT_ID), "object_version": 4},
        updated_at=datetime(2026, 8, 13, tzinfo=UTC),
    )
    target = SettingsTextInputTarget(_owner(), draft, _account())
    command = SettingsTextInputCommand(OWNER_ID, draft.ref, "Тайное новое имя")

    with pytest.raises(ValueError, match="canonical"):
        SettingsTextInputTarget(
            _owner(),
            DraftSnapshot(
                draft.draft_id,
                draft.state,
                {**draft.payload, "history_page": 3},
            ),
            _account(),
        )
    with pytest.raises(ValueError, match="too long"):
        SettingsTextInputCommand(OWNER_ID, draft.ref, "x" * 4097)

    rendered = repr((target, command))
    for private in (
        str(OWNER_ID),
        str(ACCOUNT_ID),
        "Тайное",
        "Закрытый",
        "RUB",
    ):
        assert private not in rendered
