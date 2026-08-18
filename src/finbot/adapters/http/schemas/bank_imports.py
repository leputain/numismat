from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, TypeAdapter

from finbot.adapters.database.repositories.http_idempotency import IdempotencyResultKind
from finbot.adapters.http.bank_imports.cursor import (
    BANK_IMPORT_BATCH_CURSOR_LENGTH,
    BANK_IMPORT_ROW_CURSOR_LENGTH,
)
from finbot.adapters.http.bank_imports.service import (
    HttpBankImportBatchPage,
    HttpBankImportRowPage,
)
from finbot.adapters.http.mutations.service import MutationReceipt
from finbot.adapters.http.schemas.common import ApiModel
from finbot.adapters.http.schemas.finance import TransactionResponse, transaction_response
from finbot.application.bank_imports import (
    BankImportBatchSnapshot,
    BankImportRowSnapshot,
    ReconciliationCandidate,
)

PositiveVersion = Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]
PositiveMinor = Annotated[
    str,
    Field(strict=True, pattern=r"^[1-9][0-9]{0,18}$", min_length=1, max_length=19),
]


class VersionedBankImportRowRequest(ApiModel):
    batch_version: PositiveVersion = Field(repr=False)
    row_version: PositiveVersion = Field(repr=False)


class LinkBankImportRowRequest(VersionedBankImportRowRequest):
    transaction_id: UUID = Field(repr=False)
    transaction_version: PositiveVersion = Field(repr=False)


class CancelBankImportRequest(ApiModel):
    version: PositiveVersion = Field(repr=False)


VERSIONED_BANK_IMPORT_ROW_ADAPTER = TypeAdapter[VersionedBankImportRowRequest](
    VersionedBankImportRowRequest
)
LINK_BANK_IMPORT_ROW_ADAPTER = TypeAdapter[LinkBankImportRowRequest](LinkBankImportRowRequest)
CANCEL_BANK_IMPORT_ADAPTER = TypeAdapter[CancelBankImportRequest](CancelBankImportRequest)


class BankImportCountsResponse(ApiModel):
    total: int = Field(ge=1, le=2000)
    pending: int = Field(ge=0, le=2000)
    staged: int = Field(ge=0, le=2000)
    confirmed: int = Field(ge=0, le=2000)
    linked: int = Field(ge=0, le=2000)
    skipped: int = Field(ge=0, le=2000)
    cancelled: int = Field(ge=0, le=2000)


class BankImportBatchResponse(ApiModel):
    id: UUID = Field(repr=False)
    account_id: UUID = Field(repr=False)
    profile: Literal["canonical_v1"]
    encoding: Literal["utf-8", "windows-1251"]
    state: Literal["open", "completed", "cancelled"]
    counts: BankImportCountsResponse = Field(repr=False)
    version: PositiveVersion = Field(repr=False)
    created_at: datetime = Field(repr=False)
    updated_at: datetime = Field(repr=False)
    completed_at: datetime | None = Field(repr=False)
    cancelled_at: datetime | None = Field(repr=False)


class BankImportBatchPageResponse(ApiModel):
    items: tuple[BankImportBatchResponse, ...] = Field(max_length=50, repr=False)
    next_cursor: str | None = Field(
        default=None,
        min_length=BANK_IMPORT_BATCH_CURSOR_LENGTH,
        max_length=BANK_IMPORT_BATCH_CURSOR_LENGTH,
        repr=False,
    )


class BankImportRowResponse(ApiModel):
    id: UUID = Field(repr=False)
    batch_id: UUID = Field(repr=False)
    position: int = Field(ge=1, le=2000)
    occurred_at: datetime = Field(repr=False)
    type: Literal["expense", "income"] = Field(repr=False)
    amount_minor: PositiveMinor = Field(repr=False)
    currency: str = Field(pattern=r"^[A-Z]{3}$", repr=False)
    description: str = Field(max_length=500, repr=False)
    state: Literal["pending", "staged", "confirmed", "linked", "skipped", "cancelled"]
    outcome: Literal[
        "pending",
        "awaiting_review",
        "dismissed",
        "confirmed",
        "linked",
        "skipped",
        "cancelled",
    ]
    has_reference: bool
    possible_duplicate: bool
    version: PositiveVersion = Field(repr=False)
    draft_id: UUID | None = Field(repr=False)
    transaction_id: UUID | None = Field(repr=False)
    resolved_at: datetime | None = Field(repr=False)
    created_at: datetime = Field(repr=False)
    updated_at: datetime = Field(repr=False)


class BankImportRowPageResponse(ApiModel):
    items: tuple[BankImportRowResponse, ...] = Field(max_length=50, repr=False)
    next_cursor: str | None = Field(
        default=None,
        min_length=BANK_IMPORT_ROW_CURSOR_LENGTH,
        max_length=BANK_IMPORT_ROW_CURSOR_LENGTH,
        repr=False,
    )


class ReconciliationCandidateResponse(ApiModel):
    rank: int = Field(ge=1, le=5)
    transaction: TransactionResponse = Field(repr=False)


class ReconciliationCandidatesResponse(ApiModel):
    items: tuple[ReconciliationCandidateResponse, ...] = Field(max_length=5, repr=False)


class BankImportBatchMutationResultResponse(ApiModel):
    kind: Literal["bank_import_batch"]
    batch_id: UUID = Field(repr=False)
    version: PositiveVersion = Field(repr=False)


class BankImportBatchMutationResponse(ApiModel):
    result: BankImportBatchMutationResultResponse = Field(repr=False)


class BankImportRowMutationResultResponse(ApiModel):
    kind: Literal["bank_import_row"]
    row_id: UUID = Field(repr=False)
    version: PositiveVersion = Field(repr=False)


class BankImportRowMutationResponse(ApiModel):
    result: BankImportRowMutationResultResponse = Field(repr=False)


class BankImportDraftMutationResultResponse(ApiModel):
    kind: Literal["draft"]
    draft_id: UUID = Field(repr=False)
    revision: PositiveVersion = Field(repr=False)


class BankImportDraftMutationResponse(ApiModel):
    result: BankImportDraftMutationResultResponse = Field(repr=False)


def bank_import_batch_response(value: BankImportBatchSnapshot) -> BankImportBatchResponse:
    counts = value.counts
    return BankImportBatchResponse(
        id=value.batch_id,
        account_id=value.account_id,
        profile=value.profile.value,
        encoding=value.encoding.value,
        state=value.state.value,
        counts=BankImportCountsResponse(
            total=counts.total,
            pending=counts.pending,
            staged=counts.staged,
            confirmed=counts.confirmed,
            linked=counts.linked,
            skipped=counts.skipped,
            cancelled=counts.cancelled,
        ),
        version=value.version,
        created_at=value.created_at,
        updated_at=value.updated_at,
        completed_at=value.completed_at,
        cancelled_at=value.cancelled_at,
    )


def bank_import_batch_page_response(
    value: HttpBankImportBatchPage,
) -> BankImportBatchPageResponse:
    return BankImportBatchPageResponse(
        items=tuple(bank_import_batch_response(item) for item in value.page.items),
        next_cursor=value.next_cursor,
    )


def bank_import_row_response(value: BankImportRowSnapshot) -> BankImportRowResponse:
    return BankImportRowResponse(
        id=value.row_id,
        batch_id=value.batch_id,
        position=value.position,
        occurred_at=value.occurred_at,
        type=value.kind.value,
        amount_minor=str(value.amount_minor),
        currency=value.currency,
        description=value.description,
        state=value.state.value,
        outcome=value.outcome.value,
        has_reference=value.has_reference,
        possible_duplicate=value.possible_duplicate,
        version=value.version,
        draft_id=value.draft_id,
        transaction_id=value.transaction_id,
        resolved_at=value.resolved_at,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def bank_import_row_page_response(value: HttpBankImportRowPage) -> BankImportRowPageResponse:
    return BankImportRowPageResponse(
        items=tuple(bank_import_row_response(item) for item in value.page.items),
        next_cursor=value.next_cursor,
    )


def reconciliation_candidates_response(
    values: tuple[ReconciliationCandidate, ...],
) -> ReconciliationCandidatesResponse:
    return ReconciliationCandidatesResponse(
        items=tuple(
            ReconciliationCandidateResponse(
                rank=value.rank,
                transaction=transaction_response(value.transaction),
            )
            for value in values
        )
    )


def bank_import_batch_mutation_response(
    value: MutationReceipt,
) -> BankImportBatchMutationResponse:
    if (
        value.kind is not IdempotencyResultKind.BANK_IMPORT_BATCH
        or value.result_id is None
        or value.revision is None
    ):
        raise ValueError("bank import mutation receipt has no batch reference")
    return BankImportBatchMutationResponse(
        result=BankImportBatchMutationResultResponse(
            kind="bank_import_batch",
            batch_id=value.result_id,
            version=value.revision,
        )
    )


def bank_import_row_mutation_response(value: MutationReceipt) -> BankImportRowMutationResponse:
    if (
        value.kind is not IdempotencyResultKind.BANK_IMPORT_ROW
        or value.result_id is None
        or value.revision is None
    ):
        raise ValueError("bank import mutation receipt has no row reference")
    return BankImportRowMutationResponse(
        result=BankImportRowMutationResultResponse(
            kind="bank_import_row",
            row_id=value.result_id,
            version=value.revision,
        )
    )


def bank_import_draft_mutation_response(
    value: MutationReceipt,
) -> BankImportDraftMutationResponse:
    if (
        value.kind is not IdempotencyResultKind.DRAFT
        or value.result_id is None
        or value.revision is None
    ):
        raise ValueError("bank import mutation receipt has no draft reference")
    return BankImportDraftMutationResponse(
        result=BankImportDraftMutationResultResponse(
            kind="draft",
            draft_id=value.result_id,
            revision=value.revision,
        )
    )


__all__ = [
    "CANCEL_BANK_IMPORT_ADAPTER",
    "LINK_BANK_IMPORT_ROW_ADAPTER",
    "VERSIONED_BANK_IMPORT_ROW_ADAPTER",
    "BankImportBatchMutationResponse",
    "BankImportBatchPageResponse",
    "BankImportBatchResponse",
    "BankImportDraftMutationResponse",
    "BankImportRowMutationResponse",
    "BankImportRowPageResponse",
    "BankImportRowResponse",
    "ReconciliationCandidatesResponse",
    "bank_import_batch_mutation_response",
    "bank_import_batch_page_response",
    "bank_import_batch_response",
    "bank_import_draft_mutation_response",
    "bank_import_row_mutation_response",
    "bank_import_row_page_response",
    "bank_import_row_response",
    "reconciliation_candidates_response",
]
