from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, TypeAdapter

from finbot.adapters.database.repositories.http_idempotency import IdempotencyResultKind
from finbot.adapters.http.budgets.cursor import BUDGET_CURSOR_LENGTH
from finbot.adapters.http.budgets.service import HttpBudgetPage
from finbot.adapters.http.mutations.service import MutationReceipt
from finbot.adapters.http.schemas.common import ApiModel
from finbot.application.budgets import BudgetProgressSnapshot

BudgetName = Annotated[str, Field(strict=True, min_length=1, max_length=60)]
PositiveMinor = Annotated[
    str,
    Field(strict=True, pattern=r"^[1-9][0-9]{0,18}$", min_length=1, max_length=19),
]
CurrencyCode = Annotated[str, Field(strict=True, pattern=r"^[A-Z]{3}$")]
PositiveVersion = Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]
TimezoneName = Annotated[str, Field(strict=True, min_length=1, max_length=64)]


class CreateBudgetRequest(ApiModel):
    name: BudgetName = Field(repr=False)
    limit_minor: PositiveMinor = Field(repr=False)
    currency: CurrencyCode = Field(repr=False)
    category_id: UUID | None = Field(default=None, repr=False)
    starts_on: date = Field(repr=False)
    ends_on: date = Field(repr=False)


class ReplaceBudgetRequest(CreateBudgetRequest):
    version: PositiveVersion = Field(repr=False)


class VersionedBudgetRequest(ApiModel):
    version: PositiveVersion = Field(repr=False)


CREATE_BUDGET_ADAPTER = TypeAdapter[CreateBudgetRequest](CreateBudgetRequest)
REPLACE_BUDGET_ADAPTER = TypeAdapter[ReplaceBudgetRequest](ReplaceBudgetRequest)
VERSIONED_BUDGET_ADAPTER = TypeAdapter[VersionedBudgetRequest](VersionedBudgetRequest)


class BudgetProgressResponse(ApiModel):
    spent_minor: str = Field(pattern=r"^(?:0|[1-9][0-9]{0,63})$", repr=False)
    remaining_minor: str = Field(pattern=r"^(?:0|[1-9][0-9]{0,18})$", repr=False)
    overspent_minor: str = Field(pattern=r"^(?:0|[1-9][0-9]{0,63})$", repr=False)
    progress_bps: int = Field(ge=0, le=10_000)
    measured_at: datetime = Field(repr=False)
    cutoff_at: datetime = Field(repr=False)


class BudgetResponse(ApiModel):
    id: UUID = Field(repr=False)
    name: str = Field(min_length=1, max_length=60, repr=False)
    limit_minor: PositiveMinor = Field(repr=False)
    currency: CurrencyCode = Field(repr=False)
    category_id: UUID | None = Field(repr=False)
    starts_on: date = Field(repr=False)
    ends_on: date = Field(repr=False)
    timezone: TimezoneName = Field(repr=False)
    version: PositiveVersion = Field(repr=False)
    deleted_at: datetime | None = Field(repr=False)
    created_at: datetime = Field(repr=False)
    updated_at: datetime = Field(repr=False)
    progress: BudgetProgressResponse = Field(repr=False)


class BudgetWindowResponse(ApiModel):
    starts_on: date = Field(repr=False)
    ends_on: date = Field(repr=False)


class BudgetPageResponse(ApiModel):
    items: tuple[BudgetResponse, ...] = Field(max_length=50, repr=False)
    window: BudgetWindowResponse = Field(repr=False)
    measured_at: datetime = Field(repr=False)
    next_cursor: str | None = Field(
        default=None,
        min_length=BUDGET_CURSOR_LENGTH,
        max_length=BUDGET_CURSOR_LENGTH,
        repr=False,
    )


class BudgetMutationResultResponse(ApiModel):
    kind: Literal["budget"]
    budget_id: UUID = Field(repr=False)
    version: PositiveVersion = Field(repr=False)


class BudgetMutationResponse(ApiModel):
    result: BudgetMutationResultResponse = Field(repr=False)


def budget_response(value: BudgetProgressSnapshot) -> BudgetResponse:
    budget = value.budget
    definition = budget.definition
    progress = value.progress
    return BudgetResponse(
        id=budget.budget_id,
        name=definition.name,
        limit_minor=str(definition.limit_minor),
        currency=definition.currency,
        category_id=definition.category_id,
        starts_on=definition.starts_on,
        ends_on=definition.ends_on,
        timezone=definition.timezone,
        version=budget.version,
        deleted_at=budget.deleted_at,
        created_at=budget.created_at,
        updated_at=budget.updated_at,
        progress=BudgetProgressResponse(
            spent_minor=str(progress.spent_minor),
            remaining_minor=str(progress.remaining_minor),
            overspent_minor=str(progress.overspent_minor),
            progress_bps=progress.progress_bps,
            measured_at=value.measured_at,
            cutoff_at=value.cutoff_at,
        ),
    )


def budget_page_response(value: HttpBudgetPage) -> BudgetPageResponse:
    return BudgetPageResponse(
        items=tuple(budget_response(item) for item in value.page.items),
        window=BudgetWindowResponse(
            starts_on=value.window_start,
            ends_on=value.window_end,
        ),
        measured_at=value.measured_at,
        next_cursor=value.next_cursor,
    )


def budget_mutation_response(value: MutationReceipt) -> BudgetMutationResponse:
    if (
        value.kind is not IdempotencyResultKind.BUDGET
        or value.result_id is None
        or value.revision is None
    ):
        raise ValueError("budget mutation receipt has no budget reference")
    return BudgetMutationResponse(
        result=BudgetMutationResultResponse(
            kind="budget",
            budget_id=value.result_id,
            version=value.revision,
        )
    )


__all__ = [
    "CREATE_BUDGET_ADAPTER",
    "REPLACE_BUDGET_ADAPTER",
    "VERSIONED_BUDGET_ADAPTER",
    "BudgetMutationResponse",
    "BudgetPageResponse",
    "BudgetResponse",
    "budget_mutation_response",
    "budget_page_response",
    "budget_response",
]
