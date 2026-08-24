from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, TypeAdapter

from finbot.adapters.database.repositories.http_idempotency import IdempotencyResultKind
from finbot.adapters.http.finance.request import CANONICAL_UUID_PATTERN
from finbot.adapters.http.mutations.service import MutationReceipt
from finbot.adapters.http.schemas.common import ApiModel
from finbot.application.draft_views import PublicDraftFlow, PublicDraftState, PublicDraftView

PositiveRevision = Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]
CanonicalUuidString = Annotated[
    str,
    Field(
        strict=True,
        min_length=36,
        max_length=36,
        pattern=CANONICAL_UUID_PATTERN,
    ),
]
PositiveMinorString = Annotated[
    str,
    Field(
        min_length=1,
        max_length=19,
        pattern=r"^[1-9][0-9]{0,18}$",
    ),
]
DecimalAmountString = Annotated[
    str,
    Field(
        strict=True,
        min_length=1,
        max_length=20,
        pattern=r"^(?:0|[1-9][0-9]{0,16})(?:\.[0-9]{1,2})?$",
    ),
]
LocalDateString = Annotated[
    str,
    Field(
        strict=True,
        min_length=10,
        max_length=10,
        pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
    ),
]


class MutationRequestModel(ApiModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class EmptyMutationRequest(MutationRequestModel):
    pass


class QuickDraftRequest(MutationRequestModel):
    text: str = Field(
        strict=True,
        min_length=1,
        max_length=4096,
        pattern=r"\S",
        repr=False,
    )


class RevisionMutationRequest(MutationRequestModel):
    revision: PositiveRevision = Field(repr=False)


class VersionMutationRequest(MutationRequestModel):
    version: PositiveRevision = Field(repr=False)


class TimezoneSettingsRequest(MutationRequestModel):
    timezone: str = Field(strict=True, min_length=1, max_length=64, repr=False)
    version: PositiveRevision = Field(repr=False)


class ExistingSelectionRequest(MutationRequestModel):
    kind: Literal["existing"]
    id: CanonicalUuidString = Field(repr=False)
    version: PositiveRevision = Field(repr=False)


class ComposeDraftRequest(MutationRequestModel):
    type: Literal["income", "expense"]
    amount: DecimalAmountString = Field(repr=False)
    account: ExistingSelectionRequest | None = Field(default=None, repr=False)
    category: ExistingSelectionRequest | None = Field(default=None, repr=False)
    occurred_on: LocalDateString | None = Field(default=None, repr=False)
    description: str = Field(default="", strict=True, max_length=500, repr=False)


class CustomSelectionRequest(MutationRequestModel):
    kind: Literal["custom"]


type CatalogSelectionRequest = Annotated[
    ExistingSelectionRequest | CustomSelectionRequest,
    Field(discriminator="kind"),
]


class InputTextPatchRequest(RevisionMutationRequest):
    action: Literal["input_text"]
    text: str = Field(strict=True, max_length=4096, repr=False)


class NavigationPatchRequest(RevisionMutationRequest):
    action: Literal[
        "edit_type",
        "edit_amount",
        "edit_category",
        "edit_account",
        "edit_date",
        "edit_description",
        "skip_description",
        "back",
    ]


class SelectTypePatchRequest(RevisionMutationRequest):
    action: Literal["select_type"]
    value: Literal["income", "expense"]


class SelectEditTypePatchRequest(RevisionMutationRequest):
    action: Literal["select_edit_type"]
    value: Literal["income", "expense"]
    category: ExistingSelectionRequest = Field(repr=False)


class SelectCatalogPatchRequest(RevisionMutationRequest):
    action: Literal["select_category", "select_account"]
    selection: CatalogSelectionRequest = Field(repr=False)


class SelectDatePatchRequest(RevisionMutationRequest):
    action: Literal["select_date"]
    value: Literal["today", "yesterday", "custom"]


class SetRulePatchRequest(RevisionMutationRequest):
    action: Literal["set_rule"]
    value: Literal["global", "account", "remove"]


type DraftPatchRequest = Annotated[
    InputTextPatchRequest
    | NavigationPatchRequest
    | SelectTypePatchRequest
    | SelectEditTypePatchRequest
    | SelectCatalogPatchRequest
    | SelectDatePatchRequest
    | SetRulePatchRequest,
    Field(discriminator="action"),
]

EMPTY_MUTATION_ADAPTER = TypeAdapter(EmptyMutationRequest)
QUICK_DRAFT_ADAPTER = TypeAdapter(QuickDraftRequest)
COMPOSE_DRAFT_ADAPTER = TypeAdapter(ComposeDraftRequest)
REVISION_MUTATION_ADAPTER = TypeAdapter(RevisionMutationRequest)
VERSION_MUTATION_ADAPTER = TypeAdapter(VersionMutationRequest)
TIMEZONE_SETTINGS_ADAPTER = TypeAdapter(TimezoneSettingsRequest)
DRAFT_PATCH_ADAPTER: TypeAdapter[DraftPatchRequest] = TypeAdapter(DraftPatchRequest)


class DraftCatalogResponse(ApiModel):
    id: UUID = Field(repr=False)
    name: str = Field(min_length=1, max_length=100, repr=False)
    emoji: str | None = Field(default=None, max_length=16, repr=False)


class DraftTransactionResponse(ApiModel):
    type: Literal["income", "expense"] | None = Field(default=None, repr=False)
    amount_minor: PositiveMinorString | None = Field(default=None, repr=False)
    currency: str | None = Field(
        default=None,
        min_length=3,
        max_length=3,
        pattern=r"^[A-Z]{3}$",
        repr=False,
    )
    account: DraftCatalogResponse | None = Field(default=None, repr=False)
    category: DraftCatalogResponse | None = Field(default=None, repr=False)
    occurred_at: datetime | None = Field(default=None, repr=False)
    description: str | None = Field(default=None, max_length=500, repr=False)


class DraftEditTargetResponse(ApiModel):
    transaction_id: UUID = Field(repr=False)
    version: PositiveRevision = Field(repr=False)


class DraftConflictResponse(ApiModel):
    pending_kind: Literal["wizard", "quick", "compose", "repeat", "edit"]


class DraftRuleResponse(ApiModel):
    offered: bool
    selected_scope: Literal["global", "account"] | None = None


class DraftResponse(ApiModel):
    id: UUID = Field(repr=False)
    revision: PositiveRevision = Field(repr=False)
    state: PublicDraftState
    suspended: bool
    supported: bool
    flow: PublicDraftFlow
    transaction: DraftTransactionResponse | None = Field(default=None, repr=False)
    edit_target: DraftEditTargetResponse | None = Field(default=None, repr=False)
    conflict: DraftConflictResponse | None = Field(default=None, repr=False)
    rule: DraftRuleResponse | None = Field(default=None, repr=False)


class ActiveDraftResponse(ApiModel):
    draft: DraftResponse | None = Field(repr=False)


class DraftMutationResultResponse(ApiModel):
    kind: Literal["draft"] = "draft"
    draft_id: UUID = Field(repr=False)
    revision: PositiveRevision = Field(repr=False)


class TransactionMutationResultResponse(ApiModel):
    kind: Literal["transaction"] = "transaction"
    transaction_id: UUID = Field(repr=False)
    version: PositiveRevision = Field(repr=False)


type MutationResultResponse = Annotated[
    DraftMutationResultResponse | TransactionMutationResultResponse,
    Field(discriminator="kind"),
]


class MutationResponse(ApiModel):
    result: MutationResultResponse = Field(repr=False)


def _catalog_response(value: object) -> DraftCatalogResponse:
    from finbot.application.draft_views import DraftCatalogView

    if not isinstance(value, DraftCatalogView):
        raise TypeError("draft catalog projection is invalid")
    return DraftCatalogResponse(id=value.entity_id, name=value.name, emoji=value.emoji)


def draft_response(value: PublicDraftView) -> DraftResponse:
    transaction = None
    if value.transaction is not None:
        transaction = DraftTransactionResponse(
            type=value.transaction.kind.value if value.transaction.kind is not None else None,
            amount_minor=(
                str(value.transaction.amount_minor)
                if value.transaction.amount_minor is not None
                else None
            ),
            currency=value.transaction.currency,
            account=(
                _catalog_response(value.transaction.account)
                if value.transaction.account is not None
                else None
            ),
            category=(
                _catalog_response(value.transaction.category)
                if value.transaction.category is not None
                else None
            ),
            occurred_at=value.transaction.occurred_at,
            description=value.transaction.description,
        )
    return DraftResponse(
        id=value.draft_id,
        revision=value.revision,
        state=value.state,
        suspended=value.suspended,
        supported=value.state.value != "unsupported",
        flow=value.flow,
        transaction=transaction,
        edit_target=(
            DraftEditTargetResponse(
                transaction_id=value.edit_target.transaction_id,
                version=value.edit_target.version,
            )
            if value.edit_target is not None
            else None
        ),
        conflict=(
            DraftConflictResponse(pending_kind=value.conflict.pending_kind.value)
            if value.conflict is not None
            else None
        ),
        rule=(
            DraftRuleResponse(
                offered=value.rule.offered,
                selected_scope=value.rule.selection,
            )
            if value.rule is not None
            else None
        ),
    )


def mutation_response(value: MutationReceipt) -> MutationResponse:
    if value.result_id is None or value.revision is None:
        raise ValueError("empty mutation receipt has no response body")
    if value.kind is IdempotencyResultKind.DRAFT:
        result: MutationResultResponse = DraftMutationResultResponse(
            draft_id=value.result_id,
            revision=value.revision,
        )
    elif value.kind is IdempotencyResultKind.TRANSACTION:
        result = TransactionMutationResultResponse(
            transaction_id=value.result_id,
            version=value.revision,
        )
    else:
        raise ValueError("unsupported mutation response kind")
    return MutationResponse(result=result)
