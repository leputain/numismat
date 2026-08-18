from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from finbot.application.dto import DraftRef, TransactionSnapshot
from finbot.application.errors import ApplicationValidationError
from finbot.domain.bank_imports import (
    MAX_BANK_IMPORT_BYTES,
    MAX_BANK_IMPORT_ROWS,
    MAX_RECONCILIATION_CANDIDATES,
    ParsedBankImportRow,
    validate_bank_import_currency,
)
from finbot.domain.transactions import TransactionType

MAX_BANK_IMPORT_PAGE_SIZE = 50


def _positive_version(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2**31 - 1:
        raise ValueError(f"{field_name} version must be positive")


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.utcoffset() is not None


class BankImportProfile(StrEnum):
    CANONICAL_V1 = "canonical_v1"


class BankImportEncoding(StrEnum):
    UTF8 = "utf-8"
    WINDOWS_1251 = "windows-1251"


class BankImportField(StrEnum):
    HEADER = "header"
    OCCURRED_AT = "occurred_at"
    TYPE = "type"
    AMOUNT = "amount"
    CURRENCY = "currency"
    DESCRIPTION = "description"
    ACCOUNT_REFERENCE = "account_reference"
    REFERENCE = "reference"


class BankImportFailureReason(StrEnum):
    EMPTY_FILE = "empty_file"
    FILE_TOO_LARGE = "file_too_large"
    UNSUPPORTED_PROFILE = "unsupported_profile"
    UNSUPPORTED_ENCODING = "unsupported_encoding"
    INVALID_ENCODING = "invalid_encoding"
    INVALID_CSV = "invalid_csv"
    HEADER_MISMATCH = "header_mismatch"
    COLUMN_LIMIT = "column_limit"
    ROW_LIMIT = "row_limit"
    FIELD_LIMIT = "field_limit"
    INVALID_FIELD = "invalid_field"


class BankImportValidationError(ApplicationValidationError):
    """Safe parse failure carrying only a closed reason and bounded row position."""

    def __init__(
        self,
        reason: BankImportFailureReason,
        *,
        row_number: int | None = None,
        field: BankImportField | None = None,
    ) -> None:
        super().__init__("Файл импорта не прошёл проверку")
        if not isinstance(reason, BankImportFailureReason):
            raise TypeError("Bank import failure reason is invalid")
        if row_number is not None and (
            isinstance(row_number, bool)
            or not isinstance(row_number, int)
            or not 1 <= row_number <= MAX_BANK_IMPORT_ROWS + 2
        ):
            raise ValueError("Bank import error row number is invalid")
        if field is not None and not isinstance(field, BankImportField):
            raise TypeError("Bank import error field is invalid")
        self.reason = reason
        self.row_number = row_number
        self.field = field

    @property
    def details(self) -> dict[str, object]:
        result: dict[str, object] = {"reason": self.reason.value}
        if self.row_number is not None:
            result["row_number"] = self.row_number
        if self.field is not None:
            result["field"] = self.field.value
        return result


class BankImportAccountCurrencyMismatchError(ApplicationValidationError):
    """The selected account cannot accept at least one normalized import row."""


@dataclass(frozen=True, slots=True, repr=False)
class KeyedBankImportDigest:
    value: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.value) is not bytes or len(self.value) != 32:
            raise ValueError("Bank import digest must contain exactly 32 bytes")


@dataclass(frozen=True, slots=True, repr=False)
class BankImportRequestDigest:
    """Opaque digest used as the only import payload in HTTP idempotency semantics."""

    value: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.value) is not bytes or len(self.value) != 32:
            raise ValueError("Bank import request digest must contain exactly 32 bytes")

    def canonical_token(self) -> str:
        return base64.urlsafe_b64encode(self.value).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True, repr=False)
class ParsedBankImport:
    profile: BankImportProfile
    encoding: BankImportEncoding
    rows: tuple[ParsedBankImportRow, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.profile, BankImportProfile):
            raise ValueError("Bank import profile is invalid")
        if not isinstance(self.encoding, BankImportEncoding):
            raise ValueError("Bank import encoding is invalid")
        if type(self.rows) is not tuple or not 1 <= len(self.rows) <= MAX_BANK_IMPORT_ROWS:
            raise ValueError("Bank import row count is invalid")
        if not all(isinstance(row, ParsedBankImportRow) for row in self.rows):
            raise ValueError("Bank import contains an invalid row")


@dataclass(frozen=True, slots=True, repr=False)
class StagedBankImportRow:
    position: int
    occurred_at: datetime = field(repr=False)
    kind: TransactionType = field(repr=False)
    amount_minor: int = field(repr=False)
    currency: str = field(repr=False)
    description: str = field(repr=False)
    fingerprint: KeyedBankImportDigest = field(repr=False)
    reference_digest: KeyedBankImportDigest | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.position, bool)
            or not isinstance(self.position, int)
            or not 1 <= self.position <= MAX_BANK_IMPORT_ROWS
        ):
            raise ValueError("Bank import row position is invalid")
        if not _aware(self.occurred_at):
            raise ValueError("Bank import timestamp must contain a timezone")
        if not isinstance(self.kind, TransactionType):
            raise ValueError("Bank import transaction type is invalid")
        if (
            isinstance(self.amount_minor, bool)
            or not isinstance(self.amount_minor, int)
            or not 1 <= self.amount_minor <= 2**63 - 1
        ):
            raise ValueError("Bank import amount is invalid")
        validate_bank_import_currency(self.currency)
        if (
            not isinstance(self.description, str)
            or len(self.description) > 500
            or (self.description and not self.description.isprintable())
        ):
            raise ValueError("Bank import description is invalid")
        if not isinstance(self.fingerprint, KeyedBankImportDigest):
            raise ValueError("Bank import fingerprint is invalid")
        if self.reference_digest is not None and not isinstance(
            self.reference_digest,
            KeyedBankImportDigest,
        ):
            raise ValueError("Bank import reference digest is invalid")


class BankImportBatchState(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class BankImportRowState(StrEnum):
    PENDING = "pending"
    STAGED = "staged"
    CONFIRMED = "confirmed"
    LINKED = "linked"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class BankImportRowOutcome(StrEnum):
    PENDING = "pending"
    AWAITING_REVIEW = "awaiting_review"
    DISMISSED = "dismissed"
    CONFIRMED = "confirmed"
    LINKED = "linked"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True, repr=False)
class BankImportBatchRef:
    batch_id: UUID = field(repr=False)
    version: int = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.batch_id, UUID):
            raise ValueError("Bank import batch id is invalid")
        _positive_version(self.version, field_name="Bank import batch")


@dataclass(frozen=True, slots=True, repr=False)
class BankImportRowRef:
    row_id: UUID = field(repr=False)
    version: int = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.row_id, UUID):
            raise ValueError("Bank import row id is invalid")
        _positive_version(self.version, field_name="Bank import row")


@dataclass(frozen=True, slots=True, repr=False)
class BankImportCounts:
    total: int
    pending: int
    staged: int
    confirmed: int
    linked: int
    skipped: int
    cancelled: int

    def __post_init__(self) -> None:
        values = (
            self.total,
            self.pending,
            self.staged,
            self.confirmed,
            self.linked,
            self.skipped,
            self.cancelled,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise ValueError("Bank import counts must be integers")
        if not 1 <= self.total <= MAX_BANK_IMPORT_ROWS or any(
            not 0 <= value <= self.total for value in values[1:]
        ):
            raise ValueError("Bank import counts are out of bounds")
        if sum(values[1:]) != self.total:
            raise ValueError("Bank import row counts do not match the total")


@dataclass(frozen=True, slots=True, repr=False)
class BankImportBatchSnapshot:
    batch_id: UUID = field(repr=False)
    owner_id: UUID = field(repr=False)
    account_id: UUID = field(repr=False)
    profile: BankImportProfile
    encoding: BankImportEncoding
    state: BankImportBatchState
    counts: BankImportCounts = field(repr=False)
    version: int = field(repr=False)
    created_at: datetime = field(repr=False)
    updated_at: datetime = field(repr=False)
    completed_at: datetime | None = field(default=None, repr=False)
    cancelled_at: datetime | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        BankImportBatchRef(self.batch_id, self.version)
        if not all(isinstance(value, UUID) for value in (self.owner_id, self.account_id)):
            raise ValueError("Bank import batch ownership is invalid")
        if not isinstance(self.profile, BankImportProfile) or not isinstance(
            self.encoding,
            BankImportEncoding,
        ):
            raise ValueError("Bank import batch format is invalid")
        if not isinstance(self.state, BankImportBatchState):
            raise ValueError("Bank import batch state is invalid")
        if not isinstance(self.counts, BankImportCounts):
            raise ValueError("Bank import counts are invalid")
        for value in (self.created_at, self.updated_at, self.completed_at, self.cancelled_at):
            if value is not None and not _aware(value):
                raise ValueError("Bank import batch timestamps must contain a timezone")
        if self.updated_at < self.created_at or any(
            value is not None and value < self.created_at
            for value in (self.completed_at, self.cancelled_at)
        ):
            raise ValueError("Bank import batch timestamps are inconsistent")
        if self.state is BankImportBatchState.OPEN:
            if self.completed_at is not None or self.cancelled_at is not None:
                raise ValueError("Open bank import contains a terminal timestamp")
            if self.counts.pending + self.counts.staged < 1 or self.counts.cancelled:
                raise ValueError("Open bank import contains no actionable rows")
        elif self.state is BankImportBatchState.COMPLETED:
            if self.completed_at is None or self.cancelled_at is not None:
                raise ValueError("Completed bank import timestamps are inconsistent")
            if self.counts.pending or self.counts.staged or self.counts.cancelled:
                raise ValueError("Completed bank import contains unresolved rows")
        else:
            if self.cancelled_at is None or self.completed_at is not None:
                raise ValueError("Cancelled bank import timestamps are inconsistent")
            if self.counts.pending or self.counts.staged or self.counts.cancelled < 1:
                raise ValueError("Cancelled bank import counts are inconsistent")

    @property
    def ref(self) -> BankImportBatchRef:
        return BankImportBatchRef(self.batch_id, self.version)


@dataclass(frozen=True, slots=True, repr=False)
class BankImportRowSnapshot:
    row_id: UUID = field(repr=False)
    batch_id: UUID = field(repr=False)
    owner_id: UUID = field(repr=False)
    position: int
    occurred_at: datetime = field(repr=False)
    kind: TransactionType = field(repr=False)
    amount_minor: int = field(repr=False)
    currency: str = field(repr=False)
    description: str = field(repr=False)
    state: BankImportRowState
    has_reference: bool
    possible_duplicate: bool
    version: int = field(repr=False)
    created_at: datetime = field(repr=False)
    updated_at: datetime = field(repr=False)
    draft_id: UUID | None = field(default=None, repr=False)
    transaction_id: UUID | None = field(default=None, repr=False)
    resolved_at: datetime | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        BankImportRowRef(self.row_id, self.version)
        if not isinstance(self.batch_id, UUID) or not isinstance(self.owner_id, UUID):
            raise ValueError("Bank import row ownership is invalid")
        StagedBankImportRow(
            position=self.position,
            occurred_at=self.occurred_at,
            kind=self.kind,
            amount_minor=self.amount_minor,
            currency=self.currency,
            description=self.description,
            fingerprint=KeyedBankImportDigest(bytes(32)),
        )
        if not isinstance(self.state, BankImportRowState):
            raise ValueError("Bank import row state is invalid")
        if type(self.has_reference) is not bool or type(self.possible_duplicate) is not bool:
            raise ValueError("Bank import row flags must be boolean")
        for timestamp in (self.created_at, self.updated_at, self.resolved_at):
            if timestamp is not None and not _aware(timestamp):
                raise ValueError("Bank import row timestamps must contain a timezone")
        if self.updated_at < self.created_at or (
            self.resolved_at is not None and self.resolved_at < self.created_at
        ):
            raise ValueError("Bank import row timestamps are inconsistent")
        for result_id in (self.draft_id, self.transaction_id):
            if result_id is not None and not isinstance(result_id, UUID):
                raise ValueError("Bank import row result reference is invalid")
        if self.state is BankImportRowState.PENDING:
            valid = (
                self.draft_id is None and self.transaction_id is None and self.resolved_at is None
            )
        elif self.state is BankImportRowState.STAGED:
            valid = self.transaction_id is None and self.resolved_at is None
        elif self.state in {BankImportRowState.CONFIRMED, BankImportRowState.LINKED}:
            valid = (
                self.draft_id is None
                and self.transaction_id is not None
                and self.resolved_at is not None
            )
        else:
            valid = (
                self.draft_id is None
                and self.transaction_id is None
                and self.resolved_at is not None
            )
        if not valid:
            raise ValueError("Bank import row state is inconsistent")

    @property
    def ref(self) -> BankImportRowRef:
        return BankImportRowRef(self.row_id, self.version)

    @property
    def outcome(self) -> BankImportRowOutcome:
        if self.state is BankImportRowState.PENDING:
            return BankImportRowOutcome.PENDING
        if self.state is BankImportRowState.STAGED:
            return (
                BankImportRowOutcome.AWAITING_REVIEW
                if self.draft_id is not None
                else BankImportRowOutcome.DISMISSED
            )
        return BankImportRowOutcome(self.state.value)


@dataclass(frozen=True, slots=True, repr=False)
class BankImportBatchCursor:
    created_at: datetime = field(repr=False)
    batch_id: UUID = field(repr=False)

    def __post_init__(self) -> None:
        if not _aware(self.created_at) or not isinstance(self.batch_id, UUID):
            raise ValueError("Bank import batch cursor is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class BankImportBatchCursorItem:
    batch: BankImportBatchSnapshot = field(repr=False)
    cursor: BankImportBatchCursor = field(repr=False)

    def __post_init__(self) -> None:
        if (
            self.batch.batch_id != self.cursor.batch_id
            or self.batch.created_at != self.cursor.created_at
        ):
            raise ValueError("Bank import batch cursor does not match its item")


@dataclass(frozen=True, slots=True, repr=False)
class BankImportBatchPage:
    items: tuple[BankImportBatchSnapshot, ...] = field(repr=False)
    next_cursor: BankImportBatchCursor | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if len(self.items) > MAX_BANK_IMPORT_PAGE_SIZE:
            raise ValueError("Bank import batch page is too large")
        if self.next_cursor is not None and not self.items:
            raise ValueError("Empty bank import batch page cannot contain a cursor")


@dataclass(frozen=True, slots=True, repr=False)
class BankImportRowCursor:
    position: int
    row_id: UUID = field(repr=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.position, bool)
            or not isinstance(self.position, int)
            or not 1 <= self.position <= MAX_BANK_IMPORT_ROWS
            or not isinstance(self.row_id, UUID)
        ):
            raise ValueError("Bank import row cursor is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class BankImportRowCursorItem:
    row: BankImportRowSnapshot = field(repr=False)
    cursor: BankImportRowCursor = field(repr=False)

    def __post_init__(self) -> None:
        if self.row.row_id != self.cursor.row_id or self.row.position != self.cursor.position:
            raise ValueError("Bank import row cursor does not match its item")


@dataclass(frozen=True, slots=True, repr=False)
class BankImportRowPage:
    items: tuple[BankImportRowSnapshot, ...] = field(repr=False)
    next_cursor: BankImportRowCursor | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if len(self.items) > MAX_BANK_IMPORT_PAGE_SIZE:
            raise ValueError("Bank import row page is too large")
        if self.next_cursor is not None and not self.items:
            raise ValueError("Empty bank import row page cannot contain a cursor")


@dataclass(frozen=True, slots=True, repr=False)
class ReconciliationCandidate:
    transaction: TransactionSnapshot = field(repr=False)
    distance_microseconds: int = field(repr=False)
    rank: int

    def __post_init__(self) -> None:
        if not isinstance(self.transaction, TransactionSnapshot):
            raise ValueError("Reconciliation candidate transaction is invalid")
        if (
            isinstance(self.distance_microseconds, bool)
            or not isinstance(self.distance_microseconds, int)
            or self.distance_microseconds < 0
        ):
            raise ValueError("Reconciliation candidate distance is invalid")
        if (
            isinstance(self.rank, bool)
            or not isinstance(self.rank, int)
            or not 1 <= self.rank <= MAX_RECONCILIATION_CANDIDATES
        ):
            raise ValueError("Reconciliation candidate rank is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class CreateBankImportCommand:
    owner_id: UUID = field(repr=False)
    account_id: UUID = field(repr=False)
    expected_account_version: int = field(repr=False)
    profile: BankImportProfile
    content: bytes = field(repr=False)
    encoding: BankImportEncoding | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID) or not isinstance(self.account_id, UUID):
            raise ValueError("Bank import ownership is invalid")
        _positive_version(self.expected_account_version, field_name="Bank import account")
        if not isinstance(self.profile, BankImportProfile):
            raise ValueError("Bank import profile is invalid")
        if type(self.content) is not bytes or not 1 <= len(self.content) <= MAX_BANK_IMPORT_BYTES:
            raise ValueError("Bank import content size is invalid")
        if self.encoding is not None and not isinstance(self.encoding, BankImportEncoding):
            raise ValueError("Bank import encoding is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class StageBankImportBatchCommand:
    owner_id: UUID = field(repr=False)
    account_id: UUID = field(repr=False)
    expected_account_version: int = field(repr=False)
    profile: BankImportProfile
    encoding: BankImportEncoding
    rows: tuple[StagedBankImportRow, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID) or not isinstance(self.account_id, UUID):
            raise ValueError("Bank import ownership is invalid")
        _positive_version(self.expected_account_version, field_name="Bank import account")
        if not isinstance(self.profile, BankImportProfile) or not isinstance(
            self.encoding,
            BankImportEncoding,
        ):
            raise ValueError("Bank import format is invalid")
        if not 1 <= len(self.rows) <= MAX_BANK_IMPORT_ROWS:
            raise ValueError("Bank import row count is invalid")
        if not all(isinstance(row, StagedBankImportRow) for row in self.rows):
            raise ValueError("Bank import contains an invalid staged row")
        if tuple(row.position for row in self.rows) != tuple(range(1, len(self.rows) + 1)):
            raise ValueError("Bank import row positions are not contiguous")


@dataclass(frozen=True, slots=True, repr=False)
class VersionedBankImportRowCommand:
    owner_id: UUID = field(repr=False)
    batch: BankImportBatchRef = field(repr=False)
    row: BankImportRowRef = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise ValueError("Bank import owner id is invalid")
        if not isinstance(self.batch, BankImportBatchRef) or not isinstance(
            self.row,
            BankImportRowRef,
        ):
            raise ValueError("Bank import expected references are invalid")


@dataclass(frozen=True, slots=True, repr=False)
class LinkBankImportRowCommand:
    owner_id: UUID = field(repr=False)
    batch: BankImportBatchRef = field(repr=False)
    row: BankImportRowRef = field(repr=False)
    transaction_id: UUID = field(repr=False)
    expected_transaction_version: int = field(repr=False)

    def __post_init__(self) -> None:
        VersionedBankImportRowCommand(self.owner_id, self.batch, self.row)
        if not isinstance(self.transaction_id, UUID):
            raise ValueError("Reconciliation transaction id is invalid")
        _positive_version(
            self.expected_transaction_version,
            field_name="Reconciliation transaction",
        )


@dataclass(frozen=True, slots=True, repr=False)
class CancelBankImportBatchCommand:
    owner_id: UUID = field(repr=False)
    expected: BankImportBatchRef = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID) or not isinstance(
            self.expected,
            BankImportBatchRef,
        ):
            raise ValueError("Bank import cancellation reference is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class BankImportMutationResult:
    batch: BankImportBatchSnapshot = field(repr=False)
    row: BankImportRowSnapshot = field(repr=False)

    def __post_init__(self) -> None:
        if self.batch.batch_id != self.row.batch_id or self.batch.owner_id != self.row.owner_id:
            raise ValueError("Bank import mutation result ownership is inconsistent")


@dataclass(frozen=True, slots=True, repr=False)
class BankImportDraftResult:
    batch: BankImportBatchSnapshot = field(repr=False)
    row: BankImportRowSnapshot = field(repr=False)
    draft: DraftRef = field(repr=False)

    def __post_init__(self) -> None:
        BankImportMutationResult(self.batch, self.row)
        if not isinstance(self.draft, DraftRef) or self.row.draft_id != self.draft.draft_id:
            raise ValueError("Bank import draft result is inconsistent")


class BankImportParser(Protocol):
    def parse(
        self,
        content: bytes,
        *,
        profile: BankImportProfile,
        encoding: BankImportEncoding | None,
    ) -> ParsedBankImport: ...


class BankImportDigester(Protocol):
    """Domain-separated keyed digest adapter; raw references stay in memory only."""

    def fingerprint(self, row: ParsedBankImportRow) -> KeyedBankImportDigest: ...

    def reference(self, value: str) -> KeyedBankImportDigest: ...

    def request_digest(
        self,
        command: StageBankImportBatchCommand,
    ) -> BankImportRequestDigest: ...


class BankImportCommandRepository(Protocol):
    """Owner-scoped commands; the enclosing channel owns commit and rollback."""

    async def create_batch(
        self,
        command: StageBankImportBatchCommand,
    ) -> BankImportBatchSnapshot: ...

    async def stage_draft(
        self,
        command: VersionedBankImportRowCommand,
    ) -> BankImportDraftResult: ...

    async def link(
        self,
        command: LinkBankImportRowCommand,
    ) -> BankImportMutationResult: ...

    async def skip(
        self,
        command: VersionedBankImportRowCommand,
    ) -> BankImportMutationResult: ...

    async def cancel_batch(
        self,
        command: CancelBankImportBatchCommand,
    ) -> BankImportBatchSnapshot: ...


class BankImportReader(Protocol):
    async def get_batch(
        self,
        owner_id: UUID,
        batch_id: UUID,
    ) -> BankImportBatchSnapshot | None: ...

    async def list_batches_after(
        self,
        owner_id: UUID,
        *,
        state: BankImportBatchState | None,
        cursor: BankImportBatchCursor | None,
        limit: int,
    ) -> tuple[BankImportBatchCursorItem, ...]: ...

    async def get_row(
        self,
        owner_id: UUID,
        batch_id: UUID,
        row_id: UUID,
    ) -> BankImportRowSnapshot | None: ...

    async def list_rows_after(
        self,
        owner_id: UUID,
        batch_id: UUID,
        *,
        state: BankImportRowState | None,
        cursor: BankImportRowCursor | None,
        limit: int,
    ) -> tuple[BankImportRowCursorItem, ...]: ...

    async def list_candidate_transactions(
        self,
        owner_id: UUID,
        row_id: UUID,
        *,
        limit: int,
    ) -> tuple[TransactionSnapshot, ...]: ...


__all__ = [
    "BankImportBatchCursor",
    "BankImportBatchPage",
    "BankImportBatchRef",
    "BankImportBatchSnapshot",
    "BankImportBatchState",
    "BankImportCommandRepository",
    "BankImportCounts",
    "BankImportDigester",
    "BankImportDraftResult",
    "BankImportEncoding",
    "BankImportFailureReason",
    "BankImportField",
    "BankImportMutationResult",
    "BankImportParser",
    "BankImportProfile",
    "BankImportReader",
    "BankImportRequestDigest",
    "BankImportRowCursor",
    "BankImportRowOutcome",
    "BankImportRowPage",
    "BankImportRowRef",
    "BankImportRowSnapshot",
    "BankImportRowState",
    "BankImportValidationError",
    "CancelBankImportBatchCommand",
    "CreateBankImportCommand",
    "KeyedBankImportDigest",
    "LinkBankImportRowCommand",
    "ParsedBankImport",
    "ReconciliationCandidate",
    "StageBankImportBatchCommand",
    "StagedBankImportRow",
    "VersionedBankImportRowCommand",
]
