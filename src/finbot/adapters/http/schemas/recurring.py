from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, TypeAdapter

from finbot.adapters.database.repositories.http_idempotency import IdempotencyResultKind
from finbot.adapters.http.mutations.service import MutationReceipt
from finbot.adapters.http.recurring.cursor import RECURRING_CURSOR_LENGTH
from finbot.adapters.http.recurring.service import (
    HttpRecurringInstancePage,
    HttpRecurringSchedulePage,
)
from finbot.adapters.http.schemas.common import ApiModel
from finbot.application.recurring import (
    RecurringInstanceSnapshot,
    RecurringScheduleSnapshot,
)

RecurringName = Annotated[str, Field(strict=True, min_length=1, max_length=60)]
PositiveMinor = Annotated[
    str,
    Field(strict=True, pattern=r"^[1-9][0-9]{0,18}$", min_length=1, max_length=19),
]
CurrencyCode = Annotated[str, Field(strict=True, pattern=r"^[A-Z]{3}$")]
PositiveVersion = Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]
RecurrenceInterval = Annotated[int, Field(strict=True, ge=1, le=365)]
LocalMinute = Annotated[
    str,
    Field(strict=True, pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"),
]


class CreateRecurringScheduleRequest(ApiModel):
    name: RecurringName = Field(repr=False)
    kind: Literal["expense", "income"]
    amount_minor: PositiveMinor = Field(repr=False)
    currency: CurrencyCode = Field(repr=False)
    account_id: UUID = Field(repr=False)
    category_id: UUID = Field(repr=False)
    cadence: Literal["daily", "weekly", "monthly"]
    interval: RecurrenceInterval
    anchor_date: date = Field(repr=False)
    local_time: LocalMinute = Field(repr=False)
    ends_on: date | None = Field(default=None, repr=False)
    description: str = Field(default="", strict=True, max_length=500, repr=False)


class ReplaceRecurringScheduleRequest(CreateRecurringScheduleRequest):
    version: PositiveVersion = Field(repr=False)


class VersionedRecurringRequest(ApiModel):
    version: PositiveVersion = Field(repr=False)


CREATE_RECURRING_SCHEDULE_ADAPTER = TypeAdapter[CreateRecurringScheduleRequest](
    CreateRecurringScheduleRequest
)
REPLACE_RECURRING_SCHEDULE_ADAPTER = TypeAdapter[ReplaceRecurringScheduleRequest](
    ReplaceRecurringScheduleRequest
)
VERSIONED_RECURRING_ADAPTER = TypeAdapter[VersionedRecurringRequest](VersionedRecurringRequest)


class RecurringScheduleResponse(ApiModel):
    id: UUID = Field(repr=False)
    name: str = Field(min_length=1, max_length=60, repr=False)
    kind: Literal["expense", "income"]
    amount_minor: PositiveMinor = Field(repr=False)
    currency: CurrencyCode = Field(repr=False)
    account_id: UUID = Field(repr=False)
    category_id: UUID = Field(repr=False)
    cadence: Literal["daily", "weekly", "monthly"]
    interval: int = Field(ge=1, le=365)
    anchor_date: date = Field(repr=False)
    local_time: LocalMinute = Field(repr=False)
    timezone: str = Field(min_length=1, max_length=64, repr=False)
    ends_on: date | None = Field(repr=False)
    description: str = Field(max_length=500, repr=False)
    next_occurrence_index: int = Field(ge=0, repr=False)
    next_due_local: datetime | None = Field(repr=False)
    next_due_at: datetime | None = Field(repr=False)
    state: Literal["active", "paused", "paused_error", "completed", "deleted"]
    pause_reason: (
        Literal[
            "account_unavailable",
            "category_unavailable",
            "currency_mismatch",
            "schedule_invalid",
        ]
        | None
    )
    version: PositiveVersion = Field(repr=False)
    deleted_at: datetime | None = Field(repr=False)
    created_at: datetime = Field(repr=False)
    updated_at: datetime = Field(repr=False)


class RecurringSchedulePageResponse(ApiModel):
    items: tuple[RecurringScheduleResponse, ...] = Field(max_length=50, repr=False)
    next_cursor: str | None = Field(
        default=None,
        min_length=RECURRING_CURSOR_LENGTH,
        max_length=RECURRING_CURSOR_LENGTH,
        repr=False,
    )


class RecurringInstanceResponse(ApiModel):
    id: UUID = Field(repr=False)
    schedule_id: UUID = Field(repr=False)
    occurrence_index: int = Field(ge=0, repr=False)
    nominal_local: datetime = Field(repr=False)
    scheduled_for: datetime = Field(repr=False)
    timezone: str = Field(min_length=1, max_length=64, repr=False)
    dst_adjusted: bool
    kind: Literal["expense", "income"]
    amount_minor: PositiveMinor = Field(repr=False)
    currency: CurrencyCode = Field(repr=False)
    account_id: UUID = Field(repr=False)
    category_id: UUID = Field(repr=False)
    description: str = Field(max_length=500, repr=False)
    status: Literal["pending", "generated", "blocked", "skipped"]
    outcome: Literal[
        "pending",
        "blocked",
        "skipped",
        "awaiting_review",
        "confirmed",
        "dismissed",
    ]
    next_attempt_at: datetime = Field(repr=False)
    draft_id: UUID | None = Field(repr=False)
    transaction_id: UUID | None = Field(repr=False)
    attempt_count: int = Field(ge=0, le=32_767, repr=False)
    failure_code: (
        Literal[
            "account_unavailable",
            "category_unavailable",
            "currency_mismatch",
            "schedule_invalid",
        ]
        | None
    )
    generated_at: datetime | None = Field(repr=False)
    skipped_at: datetime | None = Field(repr=False)
    version: PositiveVersion = Field(repr=False)
    created_at: datetime = Field(repr=False)
    updated_at: datetime = Field(repr=False)


class RecurringInstancePageResponse(ApiModel):
    items: tuple[RecurringInstanceResponse, ...] = Field(max_length=50, repr=False)
    next_cursor: str | None = Field(
        default=None,
        min_length=RECURRING_CURSOR_LENGTH,
        max_length=RECURRING_CURSOR_LENGTH,
        repr=False,
    )


class RecurringScheduleMutationResultResponse(ApiModel):
    kind: Literal["recurring_schedule"]
    schedule_id: UUID = Field(repr=False)
    version: PositiveVersion = Field(repr=False)


class RecurringScheduleMutationResponse(ApiModel):
    result: RecurringScheduleMutationResultResponse = Field(repr=False)


class RecurringInstanceMutationResultResponse(ApiModel):
    kind: Literal["recurring_instance"]
    instance_id: UUID = Field(repr=False)
    version: PositiveVersion = Field(repr=False)


class RecurringInstanceMutationResponse(ApiModel):
    result: RecurringInstanceMutationResultResponse = Field(repr=False)


def schedule_response(value: RecurringScheduleSnapshot) -> RecurringScheduleResponse:
    definition = value.definition
    rule = definition.recurrence
    return RecurringScheduleResponse(
        id=value.schedule_id,
        name=definition.name,
        kind=definition.kind.value,
        amount_minor=str(definition.amount_minor),
        currency=definition.currency,
        account_id=definition.account_id,
        category_id=definition.category_id,
        cadence=rule.cadence.value,
        interval=rule.interval,
        anchor_date=rule.anchor_date,
        local_time=rule.local_time.isoformat(timespec="minutes"),
        timezone=rule.timezone,
        ends_on=rule.ends_on,
        description=definition.description,
        next_occurrence_index=value.next_occurrence_index,
        next_due_local=value.next_due_local,
        next_due_at=value.next_due_at,
        state=value.state.value,
        pause_reason=value.pause_reason.value if value.pause_reason is not None else None,
        version=value.version,
        deleted_at=value.deleted_at,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def schedule_page_response(value: HttpRecurringSchedulePage) -> RecurringSchedulePageResponse:
    return RecurringSchedulePageResponse(
        items=tuple(schedule_response(item) for item in value.page.items),
        next_cursor=value.next_cursor,
    )


def instance_response(value: RecurringInstanceSnapshot) -> RecurringInstanceResponse:
    return RecurringInstanceResponse(
        id=value.instance_id,
        schedule_id=value.schedule_id,
        occurrence_index=value.occurrence_index,
        nominal_local=value.nominal_local,
        scheduled_for=value.scheduled_for,
        timezone=value.timezone,
        dst_adjusted=value.dst_adjusted,
        kind=value.kind.value,
        amount_minor=str(value.amount_minor),
        currency=value.currency,
        account_id=value.account_id,
        category_id=value.category_id,
        description=value.description,
        status=value.status.value,
        outcome=value.outcome.value,
        next_attempt_at=value.next_attempt_at,
        draft_id=value.draft_id,
        transaction_id=value.transaction_id,
        attempt_count=value.attempt_count,
        failure_code=value.failure_code.value if value.failure_code is not None else None,
        generated_at=value.generated_at,
        skipped_at=value.skipped_at,
        version=value.version,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def instance_page_response(value: HttpRecurringInstancePage) -> RecurringInstancePageResponse:
    return RecurringInstancePageResponse(
        items=tuple(instance_response(item) for item in value.page.items),
        next_cursor=value.next_cursor,
    )


def schedule_mutation_response(
    value: MutationReceipt,
) -> RecurringScheduleMutationResponse:
    if (
        value.kind is not IdempotencyResultKind.RECURRING_SCHEDULE
        or value.result_id is None
        or value.revision is None
    ):
        raise ValueError("recurring schedule receipt has no schedule reference")
    return RecurringScheduleMutationResponse(
        result=RecurringScheduleMutationResultResponse(
            kind="recurring_schedule",
            schedule_id=value.result_id,
            version=value.revision,
        )
    )


def instance_mutation_response(
    value: MutationReceipt,
) -> RecurringInstanceMutationResponse:
    if (
        value.kind is not IdempotencyResultKind.RECURRING_INSTANCE
        or value.result_id is None
        or value.revision is None
    ):
        raise ValueError("recurring instance receipt has no instance reference")
    return RecurringInstanceMutationResponse(
        result=RecurringInstanceMutationResultResponse(
            kind="recurring_instance",
            instance_id=value.result_id,
            version=value.revision,
        )
    )


__all__ = [
    "CREATE_RECURRING_SCHEDULE_ADAPTER",
    "REPLACE_RECURRING_SCHEDULE_ADAPTER",
    "VERSIONED_RECURRING_ADAPTER",
    "RecurringInstanceMutationResponse",
    "RecurringInstancePageResponse",
    "RecurringInstanceResponse",
    "RecurringScheduleMutationResponse",
    "RecurringSchedulePageResponse",
    "RecurringScheduleResponse",
    "instance_mutation_response",
    "instance_page_response",
    "instance_response",
    "schedule_mutation_response",
    "schedule_page_response",
    "schedule_response",
]
