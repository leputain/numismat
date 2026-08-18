from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from finbot.domain.recurrence import RecurringTransactionDefinition
from finbot.domain.transactions import TransactionType

MAX_RECURRING_PAGE_SIZE = 50
MAX_ACTIVE_RECURRING_SCHEDULES_PER_OWNER = 200
MAX_RETAINED_RECURRING_SCHEDULES_PER_OWNER = 400
MAX_PENDING_RECURRING_INSTANCES_PER_OWNER = 32
MAX_DUE_SCHEDULES_PER_TICK = 32
MAX_STAGE_OWNERS_PER_TICK = 16


class RecurringScheduleState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    PAUSED_ERROR = "paused_error"
    COMPLETED = "completed"
    DELETED = "deleted"


class RecurringFailureCode(StrEnum):
    ACCOUNT_UNAVAILABLE = "account_unavailable"
    CATEGORY_UNAVAILABLE = "category_unavailable"
    CURRENCY_MISMATCH = "currency_mismatch"
    SCHEDULE_INVALID = "schedule_invalid"


class RecurringInstanceStatus(StrEnum):
    PENDING = "pending"
    GENERATED = "generated"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class RecurringInstanceOutcome(StrEnum):
    PENDING = "pending"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    AWAITING_REVIEW = "awaiting_review"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"


@dataclass(frozen=True, slots=True, repr=False)
class RecurringScheduleRef:
    schedule_id: UUID = field(repr=False)
    version: int = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.schedule_id, UUID):
            raise ValueError("Recurring schedule id is invalid")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or not 1 <= self.version <= 2**31 - 1
        ):
            raise ValueError("Recurring schedule version must be positive")


@dataclass(frozen=True, slots=True, repr=False)
class RecurringScheduleSnapshot:
    schedule_id: UUID = field(repr=False)
    owner_id: UUID = field(repr=False)
    definition: RecurringTransactionDefinition = field(repr=False)
    next_occurrence_index: int = field(repr=False)
    created_at: datetime = field(repr=False)
    updated_at: datetime = field(repr=False)
    next_due_local: datetime | None = field(default=None, repr=False)
    next_due_at: datetime | None = field(default=None, repr=False)
    paused_at: datetime | None = field(default=None, repr=False)
    pause_reason: RecurringFailureCode | None = None
    deleted_at: datetime | None = field(default=None, repr=False)
    version: int = field(default=1, repr=False)

    def __post_init__(self) -> None:
        RecurringScheduleRef(self.schedule_id, self.version)
        if not isinstance(self.owner_id, UUID):
            raise ValueError("Recurring schedule owner id is invalid")
        if not isinstance(self.definition, RecurringTransactionDefinition):
            raise ValueError("Recurring schedule definition is invalid")
        if (
            isinstance(self.next_occurrence_index, bool)
            or not isinstance(self.next_occurrence_index, int)
            or self.next_occurrence_index < 0
        ):
            raise ValueError("Recurring occurrence index must be non-negative")
        if (self.next_due_local is None) is not (self.next_due_at is None):
            raise ValueError("Recurring due timestamps must be both set or both absent")
        if self.next_due_local is not None and self.next_due_local.tzinfo is not None:
            raise ValueError("Recurring nominal due timestamp must be timezone-naive")
        for value in (
            self.next_due_at,
            self.paused_at,
            self.deleted_at,
            self.created_at,
            self.updated_at,
        ):
            if value is not None and (not isinstance(value, datetime) or value.utcoffset() is None):
                raise ValueError("Recurring schedule timestamps must contain a timezone")
        if self.pause_reason is not None and self.paused_at is None:
            raise ValueError("Recurring failure pause requires a pause timestamp")

    @property
    def ref(self) -> RecurringScheduleRef:
        return RecurringScheduleRef(self.schedule_id, self.version)

    @property
    def state(self) -> RecurringScheduleState:
        if self.deleted_at is not None:
            return RecurringScheduleState.DELETED
        if self.paused_at is not None:
            return (
                RecurringScheduleState.PAUSED_ERROR
                if self.pause_reason is not None
                else RecurringScheduleState.PAUSED
            )
        if self.next_due_at is None:
            return RecurringScheduleState.COMPLETED
        return RecurringScheduleState.ACTIVE


@dataclass(frozen=True, slots=True, repr=False)
class RecurringInstanceRef:
    instance_id: UUID = field(repr=False)
    version: int = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.instance_id, UUID):
            raise ValueError("Recurring instance id is invalid")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or not 1 <= self.version <= 2**31 - 1
        ):
            raise ValueError("Recurring instance version must be positive")


@dataclass(frozen=True, slots=True, repr=False)
class RecurringInstanceSnapshot:
    instance_id: UUID = field(repr=False)
    schedule_id: UUID = field(repr=False)
    owner_id: UUID = field(repr=False)
    occurrence_index: int = field(repr=False)
    nominal_local: datetime = field(repr=False)
    scheduled_for: datetime = field(repr=False)
    timezone: str = field(repr=False)
    dst_adjusted: bool
    kind: TransactionType = field(repr=False)
    amount_minor: int = field(repr=False)
    currency: str = field(repr=False)
    account_id: UUID = field(repr=False)
    category_id: UUID = field(repr=False)
    description: str = field(repr=False)
    status: RecurringInstanceStatus
    next_attempt_at: datetime = field(repr=False)
    created_at: datetime = field(repr=False)
    updated_at: datetime = field(repr=False)
    draft_id: UUID | None = field(default=None, repr=False)
    transaction_id: UUID | None = field(default=None, repr=False)
    attempt_count: int = 0
    failure_code: RecurringFailureCode | None = None
    generated_at: datetime | None = field(default=None, repr=False)
    skipped_at: datetime | None = field(default=None, repr=False)
    version: int = field(default=1, repr=False)

    def __post_init__(self) -> None:
        RecurringInstanceRef(self.instance_id, self.version)
        if not all(
            isinstance(value, UUID)
            for value in (self.schedule_id, self.owner_id, self.account_id, self.category_id)
        ):
            raise ValueError("Recurring instance ownership references are invalid")
        if (
            isinstance(self.occurrence_index, bool)
            or not isinstance(self.occurrence_index, int)
            or self.occurrence_index < 0
        ):
            raise ValueError("Recurring occurrence index must be non-negative")
        if not isinstance(self.nominal_local, datetime) or self.nominal_local.tzinfo is not None:
            raise ValueError("Recurring nominal timestamp must be timezone-naive")
        for value in (
            self.scheduled_for,
            self.next_attempt_at,
            self.generated_at,
            self.skipped_at,
            self.created_at,
            self.updated_at,
        ):
            if value is not None and (not isinstance(value, datetime) or value.utcoffset() is None):
                raise ValueError("Recurring instance timestamps must contain a timezone")
        if type(self.dst_adjusted) is not bool:
            raise ValueError("Recurring DST flag must be boolean")
        if not isinstance(self.kind, TransactionType):
            raise ValueError("Recurring transaction type is invalid")
        if (
            isinstance(self.amount_minor, bool)
            or not isinstance(self.amount_minor, int)
            or not 1 <= self.amount_minor <= 2**63 - 1
        ):
            raise ValueError("Recurring amount is invalid")
        if (
            not isinstance(self.currency, str)
            or len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isalpha()
            or self.currency != self.currency.upper()
        ):
            raise ValueError("Recurring currency is invalid")
        if not isinstance(self.status, RecurringInstanceStatus):
            raise ValueError("Recurring instance status is invalid")
        if (
            isinstance(self.attempt_count, bool)
            or not isinstance(self.attempt_count, int)
            or not 0 <= self.attempt_count <= 32_767
        ):
            raise ValueError("Recurring attempt count is invalid")
        if not isinstance(self.description, str) or len(self.description) > 500:
            raise ValueError("Recurring description is invalid")
        if not isinstance(self.timezone, str) or not 1 <= len(self.timezone) <= 64:
            raise ValueError("Recurring timezone is invalid")
        for result_id in (self.draft_id, self.transaction_id):
            if result_id is not None and not isinstance(result_id, UUID):
                raise ValueError("Recurring result reference is invalid")
        if self.draft_id is not None and self.transaction_id is not None:
            raise ValueError("Recurring instance has conflicting result references")
        if self.status is RecurringInstanceStatus.PENDING:
            if any((self.generated_at, self.skipped_at, self.failure_code)):
                raise ValueError("Pending recurring instance contains terminal state")
            if self.draft_id is not None or self.transaction_id is not None:
                raise ValueError("Pending recurring instance contains a result reference")
        elif self.status is RecurringInstanceStatus.GENERATED:
            if (
                self.generated_at is None
                or self.skipped_at is not None
                or self.failure_code is not None
            ):
                raise ValueError("Generated recurring instance is inconsistent")
            if self.draft_id is None and self.transaction_id is None:
                # Once the generated draft is cancelled its FK is cleared and the
                # derived public outcome becomes dismissed.  This is intentional.
                pass
        elif self.status is RecurringInstanceStatus.BLOCKED:
            if (
                self.failure_code is None
                or self.generated_at is not None
                or self.skipped_at is not None
            ):
                raise ValueError("Blocked recurring instance is inconsistent")
            if self.draft_id is not None or self.transaction_id is not None:
                raise ValueError("Blocked recurring instance contains a result reference")
        elif self.skipped_at is None or self.generated_at is not None:
            raise ValueError("Skipped recurring instance is inconsistent")
        elif self.draft_id is not None or self.transaction_id is not None:
            raise ValueError("Skipped recurring instance contains a result reference")

    @property
    def ref(self) -> RecurringInstanceRef:
        return RecurringInstanceRef(self.instance_id, self.version)

    @property
    def outcome(self) -> RecurringInstanceOutcome:
        if self.transaction_id is not None:
            return RecurringInstanceOutcome.CONFIRMED
        if self.status is RecurringInstanceStatus.PENDING:
            return RecurringInstanceOutcome.PENDING
        if self.status is RecurringInstanceStatus.BLOCKED:
            return RecurringInstanceOutcome.BLOCKED
        if self.status is RecurringInstanceStatus.SKIPPED:
            return RecurringInstanceOutcome.SKIPPED
        if self.draft_id is not None:
            return RecurringInstanceOutcome.AWAITING_REVIEW
        return RecurringInstanceOutcome.DISMISSED


@dataclass(frozen=True, slots=True, repr=False)
class RecurringScheduleCursor:
    created_at: datetime = field(repr=False)
    schedule_id: UUID = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.created_at, datetime) or self.created_at.utcoffset() is None:
            raise ValueError("Recurring schedule cursor timestamp is invalid")
        if not isinstance(self.schedule_id, UUID):
            raise ValueError("Recurring schedule cursor id is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class RecurringScheduleCursorItem:
    schedule: RecurringScheduleSnapshot = field(repr=False)
    cursor: RecurringScheduleCursor = field(repr=False)

    def __post_init__(self) -> None:
        if self.schedule.schedule_id != self.cursor.schedule_id:
            raise ValueError("Recurring schedule cursor does not match its item")
        if self.schedule.created_at != self.cursor.created_at:
            raise ValueError("Recurring schedule cursor timestamp does not match its item")


@dataclass(frozen=True, slots=True, repr=False)
class RecurringSchedulePageSnapshot:
    items: tuple[RecurringScheduleSnapshot, ...] = field(repr=False)
    next_cursor: RecurringScheduleCursor | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if len(self.items) > MAX_RECURRING_PAGE_SIZE:
            raise ValueError("Recurring schedule page is too large")
        if self.next_cursor is not None and not self.items:
            raise ValueError("Empty recurring schedule page cannot contain a cursor")


@dataclass(frozen=True, slots=True, repr=False)
class RecurringInstanceCursor:
    scheduled_for: datetime = field(repr=False)
    instance_id: UUID = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.scheduled_for, datetime) or self.scheduled_for.utcoffset() is None:
            raise ValueError("Recurring instance cursor timestamp is invalid")
        if not isinstance(self.instance_id, UUID):
            raise ValueError("Recurring instance cursor id is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class RecurringInstanceCursorItem:
    instance: RecurringInstanceSnapshot = field(repr=False)
    cursor: RecurringInstanceCursor = field(repr=False)

    def __post_init__(self) -> None:
        if self.instance.instance_id != self.cursor.instance_id:
            raise ValueError("Recurring instance cursor does not match its item")
        if self.instance.scheduled_for != self.cursor.scheduled_for:
            raise ValueError("Recurring instance cursor timestamp does not match its item")


@dataclass(frozen=True, slots=True, repr=False)
class RecurringInstancePageSnapshot:
    items: tuple[RecurringInstanceSnapshot, ...] = field(repr=False)
    next_cursor: RecurringInstanceCursor | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if len(self.items) > MAX_RECURRING_PAGE_SIZE:
            raise ValueError("Recurring instance page is too large")
        if self.next_cursor is not None and not self.items:
            raise ValueError("Empty recurring instance page cannot contain a cursor")


@dataclass(frozen=True, slots=True, repr=False)
class CreateRecurringScheduleCommand:
    owner_id: UUID = field(repr=False)
    definition: RecurringTransactionDefinition = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise ValueError("Recurring schedule owner id is invalid")
        if not isinstance(self.definition, RecurringTransactionDefinition):
            raise ValueError("Recurring schedule definition is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class ReplaceRecurringScheduleCommand:
    owner_id: UUID = field(repr=False)
    expected: RecurringScheduleRef = field(repr=False)
    definition: RecurringTransactionDefinition = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise ValueError("Recurring schedule owner id is invalid")
        if not isinstance(self.expected, RecurringScheduleRef):
            raise ValueError("Recurring schedule reference is invalid")
        if not isinstance(self.definition, RecurringTransactionDefinition):
            raise ValueError("Recurring schedule definition is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class VersionedRecurringScheduleCommand:
    owner_id: UUID = field(repr=False)
    expected: RecurringScheduleRef = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise ValueError("Recurring schedule owner id is invalid")
        if not isinstance(self.expected, RecurringScheduleRef):
            raise ValueError("Recurring schedule reference is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class VersionedRecurringInstanceCommand:
    owner_id: UUID = field(repr=False)
    expected: RecurringInstanceRef = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID):
            raise ValueError("Recurring instance owner id is invalid")
        if not isinstance(self.expected, RecurringInstanceRef):
            raise ValueError("Recurring instance reference is invalid")


class RecurringCommandRepository(Protocol):
    """Owner-scoped commands; the enclosing channel owns commit and rollback."""

    async def create(
        self,
        command: CreateRecurringScheduleCommand,
    ) -> RecurringScheduleSnapshot: ...

    async def replace(
        self,
        command: ReplaceRecurringScheduleCommand,
    ) -> RecurringScheduleSnapshot: ...

    async def pause(
        self,
        command: VersionedRecurringScheduleCommand,
    ) -> RecurringScheduleSnapshot: ...

    async def resume(
        self,
        command: VersionedRecurringScheduleCommand,
    ) -> RecurringScheduleSnapshot: ...

    async def delete(
        self,
        command: VersionedRecurringScheduleCommand,
    ) -> RecurringScheduleSnapshot: ...

    async def restore(
        self,
        command: VersionedRecurringScheduleCommand,
    ) -> RecurringScheduleSnapshot: ...

    async def skip_instance(
        self,
        command: VersionedRecurringInstanceCommand,
    ) -> RecurringInstanceSnapshot: ...

    async def retry_instance(
        self,
        command: VersionedRecurringInstanceCommand,
    ) -> RecurringInstanceSnapshot: ...


class RecurringReader(Protocol):
    async def get_schedule(
        self,
        owner_id: UUID,
        schedule_id: UUID,
    ) -> RecurringScheduleSnapshot | None: ...

    async def list_schedules_after(
        self,
        owner_id: UUID,
        *,
        deleted: bool,
        cursor: RecurringScheduleCursor | None,
        limit: int,
    ) -> tuple[RecurringScheduleCursorItem, ...]: ...

    async def get_instance(
        self,
        owner_id: UUID,
        instance_id: UUID,
    ) -> RecurringInstanceSnapshot | None: ...

    async def list_instances_after(
        self,
        owner_id: UUID,
        schedule_id: UUID,
        *,
        cursor: RecurringInstanceCursor | None,
        limit: int,
    ) -> tuple[RecurringInstanceCursorItem, ...]: ...


class RecurringRepository(RecurringCommandRepository, RecurringReader, Protocol):
    """Combined owner-scoped port used inside mutation transactions."""
