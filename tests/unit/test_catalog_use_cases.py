from dataclasses import FrozenInstanceError
from typing import Any, cast
from uuid import UUID, uuid7

import pytest
from fakes.catalogs import InMemoryCatalogRepository

from finbot.application.catalogs import (
    BOUNDED_CATALOG_FETCH_LIMIT,
    MAX_BOUNDED_CATALOG_ITEMS,
    ArchiveAccountCommand,
    ArchiveCategoryCommand,
    CatalogCommandRepository,
    CreateAccountCommand,
    CreateCategoryCommand,
    RestoreAccountCommand,
    RestoreCategoryCommand,
    SetDefaultAccountCommand,
    UpdateAccountCommand,
    UpdateCategoryCommand,
)
from finbot.application.dto import AccountSnapshot, CategorySnapshot
from finbot.application.errors import (
    ApplicationErrorCode,
    ApplicationValidationError,
    CatalogUnavailableError,
    EntityNotFoundError,
    ObjectVersionConflictError,
)
from finbot.application.use_cases.catalogs import (
    CatalogUseCases,
    ListBoundedAccounts,
    ListBoundedCategories,
)
from finbot.domain.transactions import TransactionType


def _account(name: str) -> AccountSnapshot:
    return AccountSnapshot(uuid7(), name, "card", "RUB", None, 1)


def _category(name: str, *, kind: TransactionType = TransactionType.EXPENSE) -> CategorySnapshot:
    return CategorySnapshot(uuid7(), kind, name, "▫️", None, 1)


def _seeded_repository(
    owner_id: UUID,
) -> tuple[
    InMemoryCatalogRepository,
    AccountSnapshot,
    AccountSnapshot,
    CategorySnapshot,
    CategorySnapshot,
]:
    primary = _account("Основной")
    reserve = _account("Резервный")
    base_category = _category("Базовая")
    mutable_category = _category("Изменяемая")
    repository = InMemoryCatalogRepository(
        owners=(owner_id,),
        accounts={owner_id: (primary, reserve)},
        categories={owner_id: (base_category, mutable_category)},
        default_account_ids={owner_id: primary.account_id},
    )
    return repository, primary, reserve, base_category, mutable_category


class RecordingBoundedCatalogReader:
    def __init__(
        self,
        *,
        accounts: tuple[AccountSnapshot, ...] = (),
        categories: tuple[CategorySnapshot, ...] = (),
    ) -> None:
        self.accounts = accounts
        self.categories = categories
        self.calls: list[tuple[str, object, bool, int]] = []

    async def list_accounts_bounded(
        self,
        owner_id: UUID,
        *,
        archived: bool,
        limit: int,
    ) -> tuple[AccountSnapshot, ...]:
        self.calls.append(("accounts", owner_id, archived, limit))
        return self.accounts[:limit]

    async def list_categories_bounded(
        self,
        owner_id: UUID,
        *,
        kind: str | None,
        archived: bool,
        limit: int,
    ) -> tuple[CategorySnapshot, ...]:
        self.calls.append((kind or "categories", owner_id, archived, limit))
        return self.categories[:limit]


@pytest.mark.asyncio
async def test_bounded_catalog_queries_use_exact_fetch_limit_and_filters() -> None:
    owner_id = uuid7()
    account = _account("Основной")
    category = _category("Доход", kind=TransactionType.INCOME)
    reader = RecordingBoundedCatalogReader(accounts=(account,), categories=(category,))

    accounts = await ListBoundedAccounts(reader)(owner_id, archived=True)
    categories = await ListBoundedCategories(reader)(
        owner_id,
        kind=TransactionType.INCOME,
        archived=False,
    )

    assert accounts == (account,)
    assert categories == (category,)
    assert reader.calls == [
        ("accounts", owner_id, True, BOUNDED_CATALOG_FETCH_LIMIT),
        ("income", owner_id, False, BOUNDED_CATALOG_FETCH_LIMIT),
    ]


@pytest.mark.asyncio
async def test_bounded_catalog_queries_return_200_and_reject_201_without_truncation() -> None:
    owner_id = uuid7()
    accounts = tuple(_account(f"Счёт {index}") for index in range(BOUNDED_CATALOG_FETCH_LIMIT))
    categories = tuple(
        _category(f"Категория {index}") for index in range(BOUNDED_CATALOG_FETCH_LIMIT)
    )
    exact = RecordingBoundedCatalogReader(
        accounts=accounts[:MAX_BOUNDED_CATALOG_ITEMS],
        categories=categories[:MAX_BOUNDED_CATALOG_ITEMS],
    )
    overflow = RecordingBoundedCatalogReader(accounts=accounts, categories=categories)

    assert len(await ListBoundedAccounts(exact)(owner_id)) == MAX_BOUNDED_CATALOG_ITEMS
    assert len(await ListBoundedCategories(exact)(owner_id)) == MAX_BOUNDED_CATALOG_ITEMS
    with pytest.raises(CatalogUnavailableError):
        await ListBoundedAccounts(overflow)(owner_id)
    with pytest.raises(CatalogUnavailableError):
        await ListBoundedCategories(overflow)(owner_id)


@pytest.mark.asyncio
async def test_bounded_catalog_queries_reject_invalid_selectors_before_reader() -> None:
    owner_id = uuid7()
    reader = RecordingBoundedCatalogReader()

    with pytest.raises(ApplicationValidationError):
        await ListBoundedAccounts(reader)(owner_id, archived=cast(bool, 1))
    with pytest.raises(ApplicationValidationError):
        await ListBoundedCategories(reader)(
            owner_id,
            kind=cast(TransactionType, "expense"),
        )

    assert reader.calls == []


def test_catalog_commands_are_immutable_and_repository_is_structural_port() -> None:
    command = CreateAccountCommand(uuid7(), "Новый", "RUB")
    repository: CatalogCommandRepository = InMemoryCatalogRepository(owners=(command.owner_id,))

    assert repository is not None
    with pytest.raises(FrozenInstanceError):
        mutable = cast(Any, command)
        mutable.name = "Изменённый"


@pytest.mark.asyncio
async def test_account_commands_return_versioned_results_and_preserve_archive_rules() -> None:
    owner_id = uuid7()
    repository, primary, reserve, _, _ = _seeded_repository(owner_id)
    use_cases = CatalogUseCases(repository)

    created = await use_cases.create_account(CreateAccountCommand(owner_id, "Валюта", " usd "))
    created_snapshot = next(
        account
        for account in repository.accounts_for(owner_id)
        if account.account_id == created.entity_id
    )
    assert (created.version, created.resulting_state, created_snapshot.currency) == (
        1,
        "active",
        "USD",
    )

    updated = await use_cases.update_account(
        UpdateAccountCommand(owner_id, reserve.account_id, "  Запасной   счёт ", 1)
    )
    assert (updated.version, updated.resulting_state) == (2, "active")

    selected = await use_cases.set_default_account(
        SetDefaultAccountCommand(owner_id, reserve.account_id, 2)
    )
    assert (selected.version, selected.resulting_state) == (3, "default")
    assert repository.default_account_id_for(owner_id) == reserve.account_id

    primary_selected = await use_cases.set_default_account(
        SetDefaultAccountCommand(owner_id, primary.account_id, 1)
    )
    assert primary_selected.version == 2

    archived = await use_cases.archive_account(
        ArchiveAccountCommand(owner_id, reserve.account_id, 3)
    )
    assert (archived.version, archived.resulting_state) == (4, "archived")

    restored = await use_cases.restore_account(
        RestoreAccountCommand(owner_id, reserve.account_id, 4)
    )
    assert (restored.version, restored.resulting_state) == (5, "active")


@pytest.mark.asyncio
async def test_category_commands_return_versioned_results_and_keep_kind_stable() -> None:
    owner_id = uuid7()
    repository, _, _, _, mutable = _seeded_repository(owner_id)
    use_cases = CatalogUseCases(repository)

    created = await use_cases.create_category(
        CreateCategoryCommand(owner_id, "Зарплата", TransactionType.INCOME)
    )
    created_snapshot = next(
        category
        for category in repository.categories_for(owner_id)
        if category.category_id == created.entity_id
    )
    assert created_snapshot.kind is TransactionType.INCOME
    assert (created.version, created.resulting_state) == (1, "active")

    updated = await use_cases.update_category(
        UpdateCategoryCommand(owner_id, mutable.category_id, "Обновлённая", 1)
    )
    archived = await use_cases.archive_category(
        ArchiveCategoryCommand(owner_id, mutable.category_id, 2)
    )
    restored = await use_cases.restore_category(
        RestoreCategoryCommand(owner_id, mutable.category_id, 3)
    )

    assert (updated.version, updated.resulting_state) == (2, "active")
    assert (archived.version, archived.resulting_state) == (3, "archived")
    assert (restored.version, restored.resulting_state) == (4, "active")


@pytest.mark.asyncio
async def test_catalog_use_cases_translate_legacy_failures_to_stable_errors() -> None:
    owner_id = uuid7()
    other_owner_id = uuid7()
    repository, primary, reserve, base_category, mutable_category = _seeded_repository(owner_id)
    use_cases = CatalogUseCases(repository)

    with pytest.raises(ObjectVersionConflictError) as stale:
        await use_cases.update_account(
            UpdateAccountCommand(owner_id, reserve.account_id, "Устаревшее", 2)
        )
    assert stale.value.code is ApplicationErrorCode.OBJECT_VERSION_CONFLICT

    with pytest.raises(EntityNotFoundError) as foreign:
        await use_cases.update_account(
            UpdateAccountCommand(other_owner_id, reserve.account_id, "Чужое", 1)
        )
    assert foreign.value.code is ApplicationErrorCode.NOT_FOUND

    with pytest.raises(ApplicationValidationError) as default_account:
        await use_cases.archive_account(ArchiveAccountCommand(owner_id, primary.account_id, 1))
    assert default_account.value.code is ApplicationErrorCode.VALIDATION_FAILED

    with pytest.raises(ApplicationValidationError):
        await use_cases.create_account(CreateAccountCommand(owner_id, "Новый", "RUBLE"))
    with pytest.raises(ApplicationValidationError):
        await use_cases.update_category(
            UpdateCategoryCommand(owner_id, mutable_category.category_id, "Имя", 0)
        )
    with pytest.raises(ApplicationValidationError):
        await use_cases.create_category(
            CreateCategoryCommand(
                owner_id,
                "Неизвестная",
                cast(TransactionType, "unknown"),
            )
        )

    last_category_repository = InMemoryCatalogRepository(
        owners=(owner_id,),
        categories={owner_id: (base_category,)},
    )
    with pytest.raises(ApplicationValidationError):
        await CatalogUseCases(last_category_repository).archive_category(
            ArchiveCategoryCommand(owner_id, base_category.category_id, 1)
        )
