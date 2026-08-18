from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID

from finbot.application.ocr import MAX_OCR_IMAGE_BYTES, SUPPORTED_OCR_IMAGE_MIME_TYPES
from finbot.domain.money import validate_minor
from finbot.domain.transactions import TransactionDraft, TransactionType

_CHANNEL_PRESENTATION_KEYS = frozenset(
    {"presentation_ref", "telegram_chat_id", "telegram_message_id", "ui_message_id"}
)


def _channel_neutral_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    leaked = _CHANNEL_PRESENTATION_KEYS.intersection(payload)
    if leaked:
        raise ValueError("Draft payload must not contain channel presentation state")
    return MappingProxyType(deepcopy(dict(payload)))


class DraftConflictResolution(StrEnum):
    RESUME = "resume"
    REPLACE = "replace"
    KEEP = "keep"


class OcrQueueStatus(StrEnum):
    ADVANCED = "advanced"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class OcrImageIngressStatus(StrEnum):
    STARTED = "started"
    ACTIVE_DRAFT = "active_draft"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class OwnerContext:
    owner_id: UUID = field(repr=False)


@dataclass(frozen=True, slots=True)
class OwnerSnapshot:
    owner_id: UUID = field(repr=False)
    locale: str = field(repr=False)
    timezone: str = field(repr=False)
    base_currency: str = field(repr=False)
    default_account_id: UUID | None = field(repr=False)
    fast_mode: bool = field(default=False, repr=False)


@dataclass(frozen=True, slots=True)
class DraftRef:
    draft_id: UUID = field(repr=False)
    revision: int = field(repr=False)

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValueError("Draft revision must be positive")


@dataclass(frozen=True, slots=True)
class DraftSnapshot:
    draft_id: UUID = field(repr=False)
    state: str = field(repr=False)
    payload: Mapping[str, Any] = field(repr=False)
    schema_version: int = 1
    revision: int = field(default=1, repr=False)
    suspended: bool = False
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.state:
            raise ValueError("Draft state must not be empty")
        if self.schema_version < 1 or self.revision < 1:
            raise ValueError("Draft versions must be positive")
        object.__setattr__(self, "payload", _channel_neutral_payload(self.payload))

    @property
    def ref(self) -> DraftRef:
        return DraftRef(self.draft_id, self.revision)


@dataclass(frozen=True, slots=True)
class CreateDraftCommand:
    owner_id: UUID = field(repr=False)
    state: str = field(repr=False)
    payload: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _channel_neutral_payload(self.payload))


@dataclass(frozen=True, slots=True)
class UpdateDraftCommand:
    owner_id: UUID = field(repr=False)
    expected: DraftRef = field(repr=False)
    state: str = field(repr=False)
    payload: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _channel_neutral_payload(self.payload))


@dataclass(frozen=True, slots=True)
class DraftMutationResult:
    draft_id: UUID = field(repr=False)
    revision: int = field(repr=False)
    resulting_state: str


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    account_id: UUID = field(repr=False)
    name: str = field(repr=False)
    account_type: str = field(repr=False)
    currency: str = field(repr=False)
    archived_at: datetime | None
    version: int


@dataclass(frozen=True, slots=True)
class CategorySnapshot:
    category_id: UUID = field(repr=False)
    kind: TransactionType = field(repr=False)
    name: str = field(repr=False)
    emoji: str = field(repr=False)
    archived_at: datetime | None
    version: int


@dataclass(frozen=True, slots=True)
class TransactionSnapshot:
    transaction_id: UUID = field(repr=False)
    kind: TransactionType = field(repr=False)
    amount_minor: int = field(repr=False)
    currency: str = field(repr=False)
    account_id: UUID = field(repr=False)
    account_name: str = field(repr=False)
    category_id: UUID = field(repr=False)
    category_name: str = field(repr=False)
    category_emoji: str = field(repr=False)
    occurred_at: datetime = field(repr=False)
    description: str = field(repr=False)
    source: str = "manual"
    deleted_at: datetime | None = None
    version: int = 1

    def __post_init__(self) -> None:
        if self.amount_minor <= 0:
            raise ValueError("Transaction amount must be positive")
        if self.version < 1:
            raise ValueError("Transaction version must be positive")


@dataclass(frozen=True, slots=True)
class ConfirmTransactionDraftCommand:
    owner_id: UUID = field(repr=False)
    expected: DraftRef = field(repr=False)


@dataclass(frozen=True, slots=True)
class VersionedTransactionCommand:
    owner_id: UUID = field(repr=False)
    transaction_id: UUID = field(repr=False)
    expected_version: int = field(repr=False)

    def __post_init__(self) -> None:
        if self.expected_version < 1:
            raise ValueError("Transaction version must be positive")


@dataclass(frozen=True, slots=True)
class EditTransactionCommand:
    owner_id: UUID = field(repr=False)
    transaction_id: UUID = field(repr=False)
    expected_version: int = field(repr=False)
    amount_minor: int | None = field(default=None, repr=False)
    category_id: UUID | None = field(default=None, repr=False)
    account_id: UUID | None = field(default=None, repr=False)
    occurred_at: datetime | None = field(default=None, repr=False)
    description: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.expected_version < 1:
            raise ValueError("Transaction version must be positive")
        if self.amount_minor is not None:
            validate_minor(self.amount_minor)
        if self.occurred_at is not None and self.occurred_at.utcoffset() is None:
            raise ValueError("Transaction date must contain a timezone")
        if self.description is not None and len(self.description) > 500:
            raise ValueError("Transaction description is too long")

    @property
    def has_changes(self) -> bool:
        return any(
            value is not None
            for value in (
                self.amount_minor,
                self.category_id,
                self.account_id,
                self.occurred_at,
                self.description,
            )
        )


@dataclass(frozen=True, slots=True)
class PrepareRepeatDraftCommand(VersionedTransactionCommand):
    occurred_at: datetime = field(repr=False)

    def __post_init__(self) -> None:
        VersionedTransactionCommand.__post_init__(self)
        if self.occurred_at.utcoffset() is None:
            raise ValueError("Transaction date must contain a timezone")


@dataclass(frozen=True, slots=True)
class ReviewedTransactionInput:
    """Validated, channel-neutral transaction values read from a review draft."""

    kind: TransactionType = field(repr=False)
    amount_minor: int = field(repr=False)
    account_id: UUID = field(repr=False)
    category_id: UUID = field(repr=False)
    occurred_at: datetime = field(repr=False)
    description: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        validate_minor(self.amount_minor)
        if self.occurred_at.utcoffset() is None:
            raise ValueError("Transaction date must contain a timezone")
        if len(self.description) > 500:
            raise ValueError("Transaction description is too long")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ReviewedTransactionInput:
        """Decode both the stable payload and the pre-M1 Telegram amount key."""

        amount = payload.get("amount_minor", payload.get("amount"))
        if isinstance(amount, bool) or not isinstance(amount, (int, str)):
            raise ValueError("Draft amount is invalid")
        description = payload.get("description", "")
        if not isinstance(description, str):
            raise ValueError("Draft description is invalid")
        occurred_raw = payload.get("occurred_at")
        occurred_at = (
            occurred_raw
            if isinstance(occurred_raw, datetime)
            else datetime.fromisoformat(str(occurred_raw))
        )
        return cls(
            kind=TransactionType(str(payload.get("type", ""))),
            amount_minor=int(amount),
            account_id=UUID(str(payload.get("account_id", ""))),
            category_id=UUID(str(payload.get("category_id", ""))),
            occurred_at=occurred_at,
            description=description,
        )

    def to_payload(self) -> Mapping[str, Any]:
        return _channel_neutral_payload(
            {
                "type": self.kind.value,
                "amount_minor": self.amount_minor,
                "account_id": str(self.account_id),
                "category_id": str(self.category_id),
                "occurred_at": self.occurred_at.isoformat(),
                "description": self.description,
            }
        )


@dataclass(frozen=True, slots=True)
class PreparedTransactionDraft:
    source_transaction_id: UUID = field(repr=False)
    source_version: int = field(repr=False)
    transaction: ReviewedTransactionInput = field(repr=False)
    currency: str = field(repr=False)
    account_name: str = field(repr=False)
    category_name: str = field(repr=False)
    category_emoji: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if self.source_version < 1:
            raise ValueError("Source transaction version must be positive")
        if len(self.currency) != 3 or self.currency != self.currency.upper():
            raise ValueError("Currency must be an uppercase ISO-like code")

    def to_payload(self) -> Mapping[str, Any]:
        payload = dict(self.transaction.to_payload())
        payload.update(
            {
                "flow": "repeat",
                "currency": self.currency,
                "account_name": self.account_name,
                "category_name": self.category_name,
                "category_emoji": self.category_emoji,
                "source_transaction_id": str(self.source_transaction_id),
                "source_version": self.source_version,
            }
        )
        return _channel_neutral_payload(payload)


@dataclass(frozen=True, slots=True)
class TransactionMutationResult:
    entity_id: UUID = field(repr=False)
    version: int = field(repr=False)
    resulting_state: str
    transaction: TransactionSnapshot = field(repr=False)

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("Transaction result version must be positive")
        if self.entity_id != self.transaction.transaction_id:
            raise ValueError("Transaction result id does not match its snapshot")
        if self.version != self.transaction.version:
            raise ValueError("Transaction result version does not match its snapshot")
        if not self.resulting_state:
            raise ValueError("Transaction result state must not be empty")


@dataclass(frozen=True, slots=True)
class TransactionPageSnapshot:
    items: tuple[TransactionSnapshot, ...] = field(repr=False)
    page: int
    page_size: int
    total: int

    def __post_init__(self) -> None:
        if self.page < 0:
            raise ValueError("Transaction page must not be negative")
        if self.page_size < 1:
            raise ValueError("Transaction page size must be positive")
        if self.total < 0:
            raise ValueError("Transaction total must not be negative")

    @property
    def total_pages(self) -> int:
        if self.total == 0:
            return 0
        return (self.total + self.page_size - 1) // self.page_size


@dataclass(frozen=True, slots=True, repr=False)
class TransactionCursor:
    occurred_at: datetime = field(repr=False)
    transaction_id: UUID = field(repr=False)

    def __post_init__(self) -> None:
        if self.occurred_at.utcoffset() is None:
            raise ValueError("Transaction cursor timestamp must contain a timezone")


@dataclass(frozen=True, slots=True, repr=False)
class TransactionCursorItem:
    transaction: TransactionSnapshot = field(repr=False)
    cursor: TransactionCursor = field(repr=False)

    def __post_init__(self) -> None:
        if (
            self.cursor.occurred_at != self.transaction.occurred_at
            or self.cursor.transaction_id != self.transaction.transaction_id
        ):
            raise ValueError("Transaction cursor does not match its snapshot")


@dataclass(frozen=True, slots=True, repr=False)
class TransactionCursorPageSnapshot:
    items: tuple[TransactionCursorItem, ...] = field(repr=False)
    has_more: bool

    def __post_init__(self) -> None:
        if type(self.has_more) is not bool:
            raise TypeError("Transaction cursor page has_more must be boolean")


@dataclass(frozen=True, slots=True, repr=False)
class DeletedTransactionCursor:
    deleted_at: datetime = field(repr=False)
    transaction_id: UUID = field(repr=False)

    def __post_init__(self) -> None:
        if self.deleted_at.utcoffset() is None:
            raise ValueError("Deleted transaction cursor timestamp must contain a timezone")


@dataclass(frozen=True, slots=True, repr=False)
class DeletedTransactionCursorItem:
    transaction: TransactionSnapshot = field(repr=False)
    cursor: DeletedTransactionCursor = field(repr=False)

    def __post_init__(self) -> None:
        if (
            self.transaction.deleted_at is None
            or self.cursor.deleted_at != self.transaction.deleted_at
            or self.cursor.transaction_id != self.transaction.transaction_id
        ):
            raise ValueError("Deleted transaction cursor does not match its snapshot")


@dataclass(frozen=True, slots=True, repr=False)
class DeletedTransactionCursorPageSnapshot:
    items: tuple[DeletedTransactionCursorItem, ...] = field(repr=False)
    has_more: bool

    def __post_init__(self) -> None:
        if type(self.has_more) is not bool:
            raise TypeError("Deleted transaction cursor page has_more must be boolean")


@dataclass(frozen=True, slots=True)
class MutationResult:
    entity_id: UUID = field(repr=False)
    version: int = field(repr=False)
    resulting_state: str


@dataclass(frozen=True, slots=True)
class CurrencyTotals:
    currency: str = field(repr=False)
    income_minor: int = field(repr=False)
    expense_minor: int = field(repr=False)

    @property
    def net_minor(self) -> int:
        return self.income_minor - self.expense_minor


@dataclass(frozen=True, slots=True)
class CategoryTotalSnapshot:
    category_id: UUID = field(repr=False)
    name: str = field(repr=False)
    emoji: str = field(repr=False)
    currency: str = field(repr=False)
    amount_minor: int = field(repr=False)


@dataclass(frozen=True, slots=True)
class PeriodReportSnapshot:
    start: datetime = field(repr=False)
    end: datetime = field(repr=False)
    totals: tuple[CurrencyTotals, ...] = field(repr=False)
    category_totals: tuple[CategoryTotalSnapshot, ...] = field(repr=False)
    transactions: tuple[TransactionSnapshot, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class PeriodComparisonSnapshot:
    current_start: datetime = field(repr=False)
    current_end: datetime = field(repr=False)
    previous_start: datetime = field(repr=False)
    previous_end: datetime = field(repr=False)
    current_totals: tuple[CurrencyTotals, ...] = field(repr=False)
    previous_totals: tuple[CurrencyTotals, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    start: datetime = field(repr=False)
    end: datetime = field(repr=False)
    previous_start: datetime = field(repr=False)
    previous_end: datetime = field(repr=False)
    totals: tuple[CurrencyTotals, ...] = field(repr=False)
    previous_totals: tuple[CurrencyTotals, ...] = field(repr=False)
    top_categories: tuple[CategoryTotalSnapshot, ...] = field(repr=False)
    recent_transactions: tuple[TransactionSnapshot, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class OcrQueueCandidate:
    """A parsed OCR candidate retained for sequential explicit review.

    This is derived transaction input, never the raw image or recognized OCR
    document. Every value is hidden from ``repr`` so exception/debug output
    cannot accidentally expose financial data.
    """

    kind: TransactionType = field(repr=False)
    amount_minor: int = field(repr=False)
    occurred_at: datetime | None = field(default=None, repr=False)
    description: str = field(default="", repr=False)
    needs_confirmation: bool = field(default=False, repr=False)
    category_hint: str | None = field(default=None, repr=False)
    category_explicit: bool = field(default=False, repr=False)
    account_hint: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        validate_minor(self.amount_minor)
        if self.occurred_at is not None and self.occurred_at.utcoffset() is None:
            raise ValueError("OCR candidate date must contain a timezone")
        if len(self.description) > 500:
            raise ValueError("OCR candidate description is too long")

    @classmethod
    def from_transaction_draft(cls, draft: TransactionDraft) -> OcrQueueCandidate:
        """Copy a domain draft without retaining a repr-visible reference."""

        return cls(
            kind=draft.type,
            amount_minor=draft.amount_minor,
            occurred_at=draft.occurred_at,
            description=draft.description,
            needs_confirmation=draft.needs_confirmation,
            category_hint=draft.category_hint,
            category_explicit=draft.category_explicit,
            account_hint=draft.account_hint,
        )

    def to_transaction_draft(self) -> TransactionDraft:
        return TransactionDraft(
            amount_minor=self.amount_minor,
            type=self.kind,
            occurred_at=self.occurred_at,
            category_hint=self.category_hint,
            category_explicit=self.category_explicit,
            account_hint=self.account_hint,
            description=self.description,
            needs_confirmation=self.needs_confirmation,
        )


@dataclass(frozen=True, slots=True)
class PreparedOcrDraft:
    state: str
    payload: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        if not self.state or len(self.state) > 30:
            raise ValueError("Prepared OCR draft state is invalid")
        if "ocr_batch" in self.payload:
            raise ValueError("Prepared OCR draft must not contain queue state")
        object.__setattr__(self, "payload", _channel_neutral_payload(self.payload))


@dataclass(frozen=True, slots=True)
class OcrQueueContinuation:
    expected_candidate: OcrQueueCandidate = field(repr=False)
    prepared: PreparedOcrDraft = field(repr=False)


@dataclass(frozen=True, slots=True)
class OcrQueueActionCommand:
    owner_id: UUID = field(repr=False)
    expected: DraftRef = field(repr=False)


@dataclass(frozen=True, slots=True)
class OcrQueueMutationCommand:
    owner_id: UUID = field(repr=False)
    expected: DraftRef = field(repr=False)
    continuation: OcrQueueContinuation | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ProcessOcrImageCommand:
    owner_id: UUID = field(repr=False)
    content: bytes = field(repr=False)
    mime_type: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise TypeError("OCR image owner id must be a UUID")
        if not isinstance(self.content, bytes):
            raise TypeError("OCR image content must be bytes")
        if not self.content:
            raise ValueError("OCR image content must not be empty")
        if len(self.content) > MAX_OCR_IMAGE_BYTES:
            raise ValueError("OCR image content is too large")
        if not isinstance(self.mime_type, str):
            raise TypeError("OCR image MIME type must be a string")
        if self.mime_type not in SUPPORTED_OCR_IMAGE_MIME_TYPES:
            raise ValueError("OCR image MIME type is not supported")


@dataclass(frozen=True, slots=True)
class OcrOwnerContext:
    timezone: str = field(repr=False)
    currency: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.timezone:
            raise ValueError("OCR timezone is required")
        if len(self.currency) != 3 or self.currency != self.currency.upper():
            raise ValueError("OCR currency must be an uppercase ISO-like code")


@dataclass(frozen=True, slots=True)
class OcrQueueItemSnapshot:
    position: int
    total: int
    saved: int
    skipped: int
    draft: DraftSnapshot = field(repr=False)

    def __post_init__(self) -> None:
        if not 1 <= self.position <= self.total:
            raise ValueError("OCR queue position is invalid")
        if self.saved < 0 or self.skipped < 0:
            raise ValueError("OCR queue counters must not be negative")
        if self.position != self.saved + self.skipped + 1:
            raise ValueError("OCR queue counters do not match its position")


@dataclass(frozen=True, slots=True)
class ProcessOcrImageResult:
    """Channel-neutral outcome of one image ingress attempt.

    Raw bytes and recognized text never reach this result.  A conflict returns
    the exact pre-existing draft without mutating it, while a successful result
    carries the canonical OCR queue item required for explicit review.
    """

    status: OcrImageIngressStatus
    queue_item: OcrQueueItemSnapshot | None = field(default=None, repr=False)
    active_draft: DraftSnapshot | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, OcrImageIngressStatus):
            raise TypeError("OCR image ingress status is invalid")
        if self.status is OcrImageIngressStatus.STARTED:
            if self.queue_item is None or self.active_draft is not None:
                raise ValueError("Started OCR image ingress requires one queue item")
            return
        if self.status is OcrImageIngressStatus.ACTIVE_DRAFT:
            if self.active_draft is None or self.queue_item is not None:
                raise ValueError("OCR image conflict requires one active draft")
            return
        if self.queue_item is not None or self.active_draft is not None:
            raise ValueError("Rejected OCR image ingress must not retain draft data")

    @property
    def draft(self) -> DraftSnapshot | None:
        if self.queue_item is not None:
            return self.queue_item.draft
        return self.active_draft


@dataclass(frozen=True, slots=True)
class OcrQueueMutationResult:
    status: OcrQueueStatus
    saved: int
    skipped: int
    draft: DraftSnapshot | None = field(default=None, repr=False)
    transaction: TransactionSnapshot | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.saved < 0 or self.skipped < 0:
            raise ValueError("OCR queue counters must not be negative")
        if self.status is OcrQueueStatus.ADVANCED and self.draft is None:
            raise ValueError("Advanced OCR queue result requires a draft")
        if self.status is not OcrQueueStatus.ADVANCED and self.draft is not None:
            raise ValueError("Finished OCR queue result must not contain a draft")
