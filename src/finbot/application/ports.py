from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from finbot.application.dto import (
    EMPTY_TRANSACTION_LIST_FILTERS,
    AccountSnapshot,
    CategorySnapshot,
    CategoryTotalSnapshot,
    ConfirmTransactionDraftCommand,
    CurrencyTotals,
    DeletedTransactionCursor,
    DeletedTransactionCursorItem,
    DraftRef,
    DraftSnapshot,
    EditTransactionCommand,
    OcrOwnerContext,
    OcrQueueMutationCommand,
    OcrQueueMutationResult,
    OwnerSnapshot,
    PreparedOcrDraft,
    PreparedTransactionDraft,
    PrepareRepeatDraftCommand,
    TimeSeriesAggregateRow,
    TimeSeriesGrain,
    TransactionCursor,
    TransactionCursorItem,
    TransactionListFilters,
    TransactionMutationResult,
    TransactionSnapshot,
    VersionedTransactionCommand,
)
from finbot.domain.transactions import TransactionDraft, TransactionView


class TransactionParser(Protocol):
    def parse(self, text: str) -> TransactionDraft: ...


class TransactionRepository(Protocol):
    async def add(self, draft: TransactionDraft, user_id: object) -> TransactionView: ...


class DraftRepository(Protocol):
    async def get_active(self, owner_id: UUID) -> DraftSnapshot | None: ...

    async def create_if_absent(
        self, owner_id: UUID, state: str, payload: Mapping[str, Any]
    ) -> DraftSnapshot: ...

    async def update(
        self,
        owner_id: UUID,
        expected: DraftRef,
        state: str,
        payload: Mapping[str, Any],
    ) -> DraftSnapshot: ...

    async def replace(
        self,
        owner_id: UUID,
        expected: DraftRef,
        state: str,
        payload: Mapping[str, Any],
    ) -> DraftSnapshot: ...

    async def set_suspended(
        self, owner_id: UUID, expected: DraftRef, suspended: bool
    ) -> DraftSnapshot: ...

    async def delete(self, owner_id: UUID, expected: DraftRef) -> None: ...


class TransactionCommandRepository(Protocol):
    """Atomic persistence operations; the caller owns commit/rollback."""

    async def confirm_reviewed_draft(
        self, command: ConfirmTransactionDraftCommand
    ) -> TransactionMutationResult: ...

    async def edit(self, command: EditTransactionCommand) -> TransactionMutationResult: ...

    async def delete(self, command: VersionedTransactionCommand) -> TransactionMutationResult: ...

    async def restore(self, command: VersionedTransactionCommand) -> TransactionMutationResult: ...

    async def prepare_repeat(
        self, command: PrepareRepeatDraftCommand
    ) -> PreparedTransactionDraft: ...


class OcrDraftPreparer(Protocol):
    async def prepare(
        self,
        owner_id: UUID,
        draft: TransactionDraft,
    ) -> PreparedOcrDraft: ...


class OcrContextReader(Protocol):
    async def get_ocr_context(self, owner_id: UUID) -> OcrOwnerContext | None: ...


class OcrQueueCommandRepository(Protocol):
    """Atomic OCR queue mutations; the caller owns commit/rollback."""

    async def confirm_and_continue(
        self,
        command: OcrQueueMutationCommand,
    ) -> OcrQueueMutationResult: ...

    async def skip_and_continue(
        self,
        command: OcrQueueMutationCommand,
    ) -> OcrQueueMutationResult: ...

    async def cancel(self, command: OcrQueueMutationCommand) -> OcrQueueMutationResult: ...


class CatalogReader(Protocol):
    async def list_accounts(
        self, owner_id: UUID, *, archived: bool = False
    ) -> tuple[AccountSnapshot, ...]: ...

    async def list_categories(
        self, owner_id: UUID, *, kind: str | None = None, archived: bool = False
    ) -> tuple[CategorySnapshot, ...]: ...


class OwnerReader(Protocol):
    async def get_owner(self, owner_id: UUID) -> OwnerSnapshot | None: ...


class FinanceReader(Protocol):
    async def get_transaction(
        self, owner_id: UUID, transaction_id: UUID
    ) -> TransactionSnapshot | None: ...

    async def list_transactions(
        self,
        owner_id: UUID,
        *,
        page: int,
        page_size: int,
        deleted: bool = False,
    ) -> tuple[tuple[TransactionSnapshot, ...], int]: ...

    async def list_transactions_after(
        self,
        owner_id: UUID,
        *,
        cursor: TransactionCursor | None,
        limit: int,
        filters: TransactionListFilters = EMPTY_TRANSACTION_LIST_FILTERS,
    ) -> tuple[TransactionCursorItem, ...]: ...

    async def list_deleted_transactions_after(
        self,
        owner_id: UUID,
        *,
        cursor: DeletedTransactionCursor | None,
        limit: int,
    ) -> tuple[DeletedTransactionCursorItem, ...]: ...

    async def totals_by_currency(
        self, owner_id: UUID, start: datetime, end: datetime
    ) -> tuple[CurrencyTotals, ...]: ...

    async def category_totals(
        self,
        owner_id: UUID,
        start: datetime,
        end: datetime,
        *,
        limit: int,
    ) -> tuple[CategoryTotalSnapshot, ...]: ...

    async def period_transactions(
        self,
        owner_id: UUID,
        start: datetime,
        end: datetime,
        *,
        limit: int,
    ) -> tuple[TransactionSnapshot, ...]: ...

    async def timeseries_by_currency(
        self,
        owner_id: UUID,
        start: datetime,
        end: datetime,
        *,
        timezone: str,
        grain: TimeSeriesGrain,
        row_limit: int,
    ) -> tuple[TimeSeriesAggregateRow, ...]: ...


@dataclass(frozen=True, slots=True)
class ParserResult:
    draft: TransactionDraft = field(repr=False)
    category: str | None = field(repr=False)
