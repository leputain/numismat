from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, TypeAdapter

from finbot.adapters.database.repositories.http_idempotency import IdempotencyResultKind
from finbot.adapters.http.exchange_rates.cursor import EXCHANGE_RATE_CURSOR_LENGTH
from finbot.adapters.http.exchange_rates.service import HttpRateVersionPage
from finbot.adapters.http.mutations.service import MutationReceipt
from finbot.adapters.http.schemas.common import ApiModel
from finbot.application.dto import CurrencyTotals
from finbot.application.exchange_rates import (
    ConvertedPeriodValuation,
    RateEntrySnapshot,
    RateSourceSnapshot,
    RateVersionSnapshot,
    RateVersionSummary,
)

CurrencyCode = Annotated[str, Field(strict=True, pattern=r"^[A-Z]{3}$")]
RateDecimal = Annotated[
    str,
    Field(
        strict=True,
        min_length=1,
        max_length=19,
        pattern=r"^(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,12})?$",
    ),
]
SourceVersion = Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)]
PositiveVersion = Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]
UnsignedMinor = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=64, pattern=r"^(?:0|[1-9][0-9]{0,63})$"),
]
SignedMinor = Annotated[
    str,
    Field(
        strict=True,
        min_length=1,
        max_length=65,
        pattern=r"^(?:0|-?[1-9][0-9]{0,63})$",
    ),
]


class ManualRateEntryRequest(ApiModel):
    source_currency: CurrencyCode = Field(repr=False)
    rate: RateDecimal = Field(repr=False)


class PublishManualRateVersionRequest(ApiModel):
    target_currency: CurrencyCode = Field(repr=False)
    expected_source_version: SourceVersion = Field(repr=False)
    effective_at: str = Field(
        strict=True,
        min_length=20,
        max_length=27,
        pattern=(
            r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
            r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{1,6})?Z$"
        ),
        repr=False,
    )
    entries: tuple[ManualRateEntryRequest, ...] = Field(
        min_length=1,
        max_length=32,
        repr=False,
    )


PUBLISH_MANUAL_RATE_VERSION_ADAPTER = TypeAdapter[PublishManualRateVersionRequest](
    PublishManualRateVersionRequest
)


class RateSourceResponse(ApiModel):
    id: UUID = Field(repr=False)
    kind: Literal["manual"]
    target_currency: CurrencyCode = Field(repr=False)
    latest_version: PositiveVersion = Field(repr=False)


class RateSourcesResponse(ApiModel):
    items: tuple[RateSourceResponse, ...] = Field(max_length=32, repr=False)


class RateVersionSummaryResponse(ApiModel):
    id: UUID = Field(repr=False)
    source_id: UUID = Field(repr=False)
    target_currency: CurrencyCode = Field(repr=False)
    version: PositiveVersion = Field(repr=False)
    effective_at: datetime = Field(repr=False)
    created_at: datetime = Field(repr=False)


class RateEntryResponse(ApiModel):
    source_currency: CurrencyCode = Field(repr=False)
    target_currency: CurrencyCode = Field(repr=False)
    rate: RateDecimal = Field(repr=False)
    source_minor_digits: Literal[2]
    target_minor_digits: Literal[2]


class RateVersionResponse(RateVersionSummaryResponse):
    entries: tuple[RateEntryResponse, ...] = Field(
        min_length=1,
        max_length=32,
        repr=False,
    )


class RateVersionPageResponse(ApiModel):
    items: tuple[RateVersionSummaryResponse, ...] = Field(max_length=50, repr=False)
    next_cursor: str | None = Field(
        default=None,
        min_length=EXCHANGE_RATE_CURSOR_LENGTH,
        max_length=EXCHANGE_RATE_CURSOR_LENGTH,
        repr=False,
    )


class OriginalCurrencyTotalResponse(ApiModel):
    currency: CurrencyCode = Field(repr=False)
    income_minor: UnsignedMinor = Field(repr=False)
    expense_minor: UnsignedMinor = Field(repr=False)


class ConvertedPeriodResponse(ApiModel):
    version: RateVersionSummaryResponse = Field(repr=False)
    original_totals: tuple[OriginalCurrencyTotalResponse, ...] = Field(
        max_length=32,
        repr=False,
    )
    target_currency: CurrencyCode = Field(repr=False)
    income_minor: UnsignedMinor = Field(repr=False)
    expense_minor: UnsignedMinor = Field(repr=False)
    net_minor: SignedMinor = Field(repr=False)


class RateVersionMutationResultResponse(ApiModel):
    kind: Literal["exchange_rate_version"]
    rate_version_id: UUID = Field(repr=False)
    version: PositiveVersion = Field(repr=False)


class RateVersionMutationResponse(ApiModel):
    result: RateVersionMutationResultResponse = Field(repr=False)


def source_response(value: RateSourceSnapshot) -> RateSourceResponse:
    return RateSourceResponse(
        id=value.source_id,
        kind=value.kind.value,
        target_currency=value.target_currency,
        latest_version=value.latest_version,
    )


def sources_response(values: tuple[RateSourceSnapshot, ...]) -> RateSourcesResponse:
    return RateSourcesResponse(items=tuple(source_response(value) for value in values))


def version_summary_response(value: RateVersionSummary) -> RateVersionSummaryResponse:
    return RateVersionSummaryResponse(
        id=value.rate_version_id,
        source_id=value.source_id,
        target_currency=value.target_currency,
        version=value.version,
        effective_at=value.effective_at,
        created_at=value.created_at,
    )


def entry_response(value: RateEntrySnapshot) -> RateEntryResponse:
    entry = value.entry
    return RateEntryResponse(
        source_currency=entry.source_currency,
        target_currency=entry.target_currency,
        rate=entry.value.canonical,
        source_minor_digits=2,
        target_minor_digits=2,
    )


def version_response(value: RateVersionSnapshot) -> RateVersionResponse:
    summary = version_summary_response(value.summary)
    return RateVersionResponse(
        **summary.model_dump(),
        entries=tuple(entry_response(entry) for entry in value.entries),
    )


def version_page_response(value: HttpRateVersionPage) -> RateVersionPageResponse:
    return RateVersionPageResponse(
        items=tuple(version_summary_response(item) for item in value.page.items),
        next_cursor=value.next_cursor,
    )


def _original_total(value: CurrencyTotals) -> OriginalCurrencyTotalResponse:
    return OriginalCurrencyTotalResponse(
        currency=value.currency,
        income_minor=str(value.income_minor),
        expense_minor=str(value.expense_minor),
    )


def converted_period_response(value: ConvertedPeriodValuation) -> ConvertedPeriodResponse:
    return ConvertedPeriodResponse(
        version=version_summary_response(value.version),
        original_totals=tuple(_original_total(total) for total in value.original_totals),
        target_currency=value.version.target_currency,
        income_minor=str(value.income_minor),
        expense_minor=str(value.expense_minor),
        net_minor=str(value.net_minor),
    )


def version_mutation_response(value: MutationReceipt) -> RateVersionMutationResponse:
    if (
        value.kind is not IdempotencyResultKind.EXCHANGE_RATE_VERSION
        or value.result_id is None
        or value.revision is None
    ):
        raise ValueError("exchange-rate mutation receipt has no version reference")
    return RateVersionMutationResponse(
        result=RateVersionMutationResultResponse(
            kind="exchange_rate_version",
            rate_version_id=value.result_id,
            version=value.revision,
        )
    )


__all__ = [
    "PUBLISH_MANUAL_RATE_VERSION_ADAPTER",
    "ConvertedPeriodResponse",
    "PublishManualRateVersionRequest",
    "RateSourcesResponse",
    "RateVersionMutationResponse",
    "RateVersionPageResponse",
    "RateVersionResponse",
    "converted_period_response",
    "sources_response",
    "version_mutation_response",
    "version_page_response",
    "version_response",
]
