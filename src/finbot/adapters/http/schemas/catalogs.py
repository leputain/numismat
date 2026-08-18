from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, TypeAdapter

from finbot.adapters.database.repositories.http_idempotency import IdempotencyResultKind
from finbot.adapters.http.catalogs.service import AccountCatalog
from finbot.adapters.http.mutations.service import MutationReceipt
from finbot.adapters.http.schemas.common import ApiModel
from finbot.application.catalogs import MAX_BOUNDED_CATALOG_ITEMS
from finbot.application.dto import AccountSnapshot, CategorySnapshot

CatalogName = Annotated[str, Field(strict=True, min_length=1, max_length=60)]
CurrencyCode = Annotated[str, Field(strict=True, pattern=r"^[A-Z]{3}$")]
PositiveVersion = Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]


class CreateAccountRequest(ApiModel):
    name: CatalogName = Field(repr=False)
    currency: CurrencyCode = Field(repr=False)


class RenameCatalogRequest(ApiModel):
    name: CatalogName = Field(repr=False)
    version: PositiveVersion = Field(repr=False)


class VersionedCatalogRequest(ApiModel):
    version: PositiveVersion = Field(repr=False)


class CreateCategoryRequest(ApiModel):
    name: CatalogName = Field(repr=False)
    kind: Literal["expense", "income"] = Field(repr=False)


CREATE_ACCOUNT_ADAPTER = TypeAdapter[CreateAccountRequest](CreateAccountRequest)
RENAME_CATALOG_ADAPTER = TypeAdapter[RenameCatalogRequest](RenameCatalogRequest)
VERSIONED_CATALOG_ADAPTER = TypeAdapter[VersionedCatalogRequest](VersionedCatalogRequest)
CREATE_CATEGORY_ADAPTER = TypeAdapter[CreateCategoryRequest](CreateCategoryRequest)


class AccountResponse(ApiModel):
    id: UUID = Field(repr=False)
    name: str = Field(min_length=1, max_length=100, repr=False)
    type: str = Field(min_length=1, max_length=20, repr=False)
    currency: str = Field(pattern=r"^[A-Z]{3}$", repr=False)
    archived: bool
    version: int = Field(ge=1, le=2**31 - 1, repr=False)


class AccountsResponse(ApiModel):
    default_account_id: UUID | None = Field(repr=False)
    items: tuple[AccountResponse, ...] = Field(
        max_length=MAX_BOUNDED_CATALOG_ITEMS,
        repr=False,
    )


class CategoryResponse(ApiModel):
    id: UUID = Field(repr=False)
    kind: Literal["expense", "income"] = Field(repr=False)
    name: str = Field(min_length=1, max_length=100, repr=False)
    emoji: str = Field(max_length=8, repr=False)
    archived: bool
    version: int = Field(ge=1, le=2**31 - 1, repr=False)


class CategoriesResponse(ApiModel):
    items: tuple[CategoryResponse, ...] = Field(
        max_length=MAX_BOUNDED_CATALOG_ITEMS,
        repr=False,
    )


class AccountMutationResultResponse(ApiModel):
    kind: Literal["account"]
    account_id: UUID = Field(repr=False)
    version: int = Field(ge=1, le=2**31 - 1, repr=False)


class CategoryMutationResultResponse(ApiModel):
    kind: Literal["category"]
    category_id: UUID = Field(repr=False)
    version: int = Field(ge=1, le=2**31 - 1, repr=False)


class AccountMutationResponse(ApiModel):
    result: AccountMutationResultResponse = Field(repr=False)


class CategoryMutationResponse(ApiModel):
    result: CategoryMutationResultResponse = Field(repr=False)


type CatalogMutationResponse = AccountMutationResponse | CategoryMutationResponse


def account_response(value: AccountSnapshot) -> AccountResponse:
    return AccountResponse(
        id=value.account_id,
        name=value.name,
        type=value.account_type,
        currency=value.currency,
        archived=value.archived_at is not None,
        version=value.version,
    )


def accounts_response(value: AccountCatalog) -> AccountsResponse:
    return AccountsResponse(
        default_account_id=value.default_account_id,
        items=tuple(account_response(item) for item in value.items),
    )


def category_response(value: CategorySnapshot) -> CategoryResponse:
    return CategoryResponse(
        id=value.category_id,
        kind=value.kind.value,
        name=value.name,
        emoji=value.emoji,
        archived=value.archived_at is not None,
        version=value.version,
    )


def categories_response(values: tuple[CategorySnapshot, ...]) -> CategoriesResponse:
    return CategoriesResponse(items=tuple(category_response(item) for item in values))


def catalog_mutation_response(value: MutationReceipt) -> CatalogMutationResponse:
    if value.result_id is None or value.revision is None:
        raise ValueError("catalog mutation receipt has no entity reference")
    if value.kind is IdempotencyResultKind.ACCOUNT:
        return AccountMutationResponse(
            result=AccountMutationResultResponse(
                kind="account",
                account_id=value.result_id,
                version=value.revision,
            )
        )
    if value.kind is IdempotencyResultKind.CATEGORY:
        return CategoryMutationResponse(
            result=CategoryMutationResultResponse(
                kind="category",
                category_id=value.result_id,
                version=value.revision,
            )
        )
    raise ValueError("unsupported catalog mutation response kind")
