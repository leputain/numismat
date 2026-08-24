from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from finbot.application.draft_composition import (
    BeginComposeDraftCommand,
    ComposedDraftInput,
    PrepareComposedDraftCommand,
)
from finbot.application.draft_conflicts import (
    PENDING_DRAFT_INTENT_KEY,
    PendingComposeIntent,
    PendingQuickIntent,
    PendingRepeatIntent,
    PendingWizardIntent,
    encode_pending_draft_intent,
)
from finbot.application.draft_ingress import (
    QUICK_DRAFT_INGRESS_CONFLICT_STATES,
    BankImportDraftIngressBlockedError,
    BeginQuickDraftCommand,
    BeginRepeatDraftCommand,
    BeginWizardDraftCommand,
    DraftIngressClock,
    DraftIngressComposePreparer,
    DraftIngressOperation,
    DraftIngressOwnerQuery,
    DraftIngressQuickPreparer,
    DraftIngressRepeatPreparer,
    DraftIngressResult,
    DraftIngressStatus,
    QuickDraftIngressNotApplicableError,
)
from finbot.application.draft_preparation import (
    PreparedDraftResult,
    PrepareQuickDraftCommand,
)
from finbot.application.dto import (
    CreateDraftCommand,
    DraftSnapshot,
    OwnerSnapshot,
    PrepareRepeatDraftCommand,
    UpdateDraftCommand,
)
from finbot.application.errors import (
    ActiveDraftConflictError,
    ApplicationValidationError,
    InvalidStateError,
)
from finbot.application.use_cases.draft_preparation import SystemDraftPreparationClock
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.domain.recurrence import resolve_local_occurrence


class DraftIngressUseCases:
    """Start a draft or stage one typed replacement intent without overwriting work."""

    __slots__ = (
        "_clock",
        "_compose_drafts",
        "_drafts",
        "_owners",
        "_quick_drafts",
        "_transactions",
    )

    def __init__(
        self,
        drafts: DraftUseCases,
        transactions: DraftIngressRepeatPreparer,
        owners: DraftIngressOwnerQuery,
        clock: DraftIngressClock | None = None,
        *,
        quick_drafts: DraftIngressQuickPreparer | None = None,
        compose_drafts: DraftIngressComposePreparer | None = None,
    ) -> None:
        self._drafts = drafts
        self._transactions = transactions
        self._owners = owners
        self._clock = clock or SystemDraftPreparationClock()
        self._quick_drafts = quick_drafts
        self._compose_drafts = compose_drafts

    @staticmethod
    def _pending_payload(
        current_payload: Mapping[str, Any],
        encoded: dict[str, object],
    ) -> dict[str, object]:
        payload = dict(current_payload)
        if PENDING_DRAFT_INTENT_KEY in payload:
            raise InvalidStateError("Сначала выберите действие для незавершённого ввода")
        # History position belongs to a Telegram receipt, not to a shared
        # application draft.  Stop propagating the rolling legacy key when
        # this exact draft revision is touched.
        payload.pop("history_page", None)
        payload[PENDING_DRAFT_INTENT_KEY] = encoded
        return payload

    @staticmethod
    def _ensure_can_stage(current: DraftSnapshot | None, owner: OwnerSnapshot) -> None:
        if current is not None and current.payload.get("flow") == "bank_import":
            raise BankImportDraftIngressBlockedError(owner, current)
        if current is not None and PENDING_DRAFT_INTENT_KEY in current.payload:
            raise InvalidStateError("Сначала выберите действие для незавершённого ввода")

    @staticmethod
    def _ensure_quick_can_stage(current: DraftSnapshot) -> None:
        if current.suspended or current.state in QUICK_DRAFT_INGRESS_CONFLICT_STATES:
            return
        raise QuickDraftIngressNotApplicableError("Текст обрабатывается другим активным сценарием")

    @staticmethod
    def _require_owner(owner_id: UUID, expected_owner_id: UUID) -> None:
        if owner_id != expected_owner_id:
            raise ApplicationValidationError("Контекст владельца повреждён")

    async def _start_or_stage(
        self,
        *,
        owner_id: UUID,
        owner: OwnerSnapshot,
        current: DraftSnapshot | None,
        state: str,
        payload: Mapping[str, Any],
        pending_intent: dict[str, object],
        quick_policy: bool = False,
    ) -> tuple[DraftIngressStatus, DraftSnapshot]:
        if current is None:
            try:
                created = await self._drafts.create(CreateDraftCommand(owner_id, state, payload))
            except ActiveDraftConflictError:
                # ``create_if_absent`` is the durable absent-row race guard.
                # A concurrent winner is staged against by exact revision;
                # if it changed again, the subsequent CAS fails closed.
                current = await self._drafts.get_active(owner_id)
                if current is None:
                    raise
            else:
                return DraftIngressStatus.STARTED, created

        self._ensure_can_stage(current, owner)
        if quick_policy:
            self._ensure_quick_can_stage(current)
        updated = await self._drafts.update(
            UpdateDraftCommand(
                owner_id,
                current.ref,
                current.state,
                self._pending_payload(current.payload, pending_intent),
            )
        )
        return DraftIngressStatus.CONFLICT, updated

    async def begin_wizard(self, command: BeginWizardDraftCommand) -> DraftIngressResult:
        owner = await self._owners(command.owner_id)
        self._require_owner(owner.owner_id, command.owner_id)
        current = await self._drafts.get_active(command.owner_id)
        status, draft = await self._start_or_stage(
            owner_id=command.owner_id,
            owner=owner,
            current=current,
            state="wizard_type",
            payload={"flow": "wizard"},
            pending_intent=encode_pending_draft_intent(PendingWizardIntent()),
        )
        return DraftIngressResult(
            DraftIngressOperation.WIZARD,
            status,
            owner,
            draft,
        )

    async def begin_quick(self, command: BeginQuickDraftCommand) -> DraftIngressResult:
        owner = await self._owners(command.owner_id)
        self._require_owner(owner.owner_id, command.owner_id)
        current = await self._drafts.get_active(command.owner_id)
        self._ensure_can_stage(current, owner)
        pending_intent = encode_pending_draft_intent(PendingQuickIntent(command.text))

        if current is not None:
            self._ensure_quick_can_stage(current)
            status, draft = await self._start_or_stage(
                owner_id=command.owner_id,
                owner=owner,
                current=current,
                state=current.state,
                payload=current.payload,
                pending_intent=pending_intent,
                quick_policy=True,
            )
        else:
            if self._quick_drafts is None:
                raise RuntimeError("Quick draft ingress is not configured")
            prepared = await self._quick_drafts.execute(
                PrepareQuickDraftCommand(command.owner_id, command.text)
            )
            if (
                not isinstance(prepared, PreparedDraftResult)
                or prepared.payload.get("flow") != "quick"
            ):
                raise ApplicationValidationError("Быстрый черновик повреждён")
            status, draft = await self._start_or_stage(
                owner_id=command.owner_id,
                owner=owner,
                current=None,
                state=prepared.state.value,
                payload=prepared.payload,
                pending_intent=pending_intent,
                quick_policy=True,
            )

        return DraftIngressResult(
            DraftIngressOperation.QUICK,
            status,
            owner,
            draft,
        )

    def _compose_values(
        self,
        command: BeginComposeDraftCommand,
        owner: OwnerSnapshot,
    ) -> ComposedDraftInput:
        try:
            timezone = ZoneInfo(owner.timezone)
            local_now = self._clock.now(owner.timezone)
        except ZoneInfoNotFoundError:
            raise ApplicationValidationError("Часовой пояс владельца не поддерживается") from None
        if not isinstance(local_now, datetime) or local_now.utcoffset() is None:
            raise ApplicationValidationError("Текущее время должно содержать часовой пояс")
        local_now = local_now.astimezone(timezone)
        occurred_at = local_now
        if command.spec.occurred_on is not None:
            day = command.spec.occurred_on
            nominal = datetime(
                day.year,
                day.month,
                day.day,
                local_now.hour,
                local_now.minute,
                local_now.second,
                local_now.microsecond,
            )
            try:
                resolved, _adjusted = resolve_local_occurrence(nominal, owner.timezone)
            except ValueError:
                raise ApplicationValidationError("Дата операции недоступна") from None
            occurred_at = resolved.astimezone(timezone)
        return ComposedDraftInput(
            kind=command.spec.kind,
            amount_minor=command.spec.amount_minor,
            occurred_at=occurred_at,
            account=command.spec.account,
            category=command.spec.category,
            description=command.spec.description,
        )

    async def begin_compose(self, command: BeginComposeDraftCommand) -> DraftIngressResult:
        owner = await self._owners(command.owner_id)
        self._require_owner(owner.owner_id, command.owner_id)
        current = await self._drafts.get_active(command.owner_id)
        self._ensure_can_stage(current, owner)
        values = self._compose_values(command, owner)
        pending_intent = encode_pending_draft_intent(PendingComposeIntent(values))
        state: str

        if current is None:
            if self._compose_drafts is None:
                raise RuntimeError("Compose draft ingress is not configured")
            prepared = await self._compose_drafts.execute(
                PrepareComposedDraftCommand(command.owner_id, values)
            )
            if (
                not isinstance(prepared, PreparedDraftResult)
                or prepared.state.value != "review"
                or prepared.payload.get("flow") != "quick"
            ):
                raise ApplicationValidationError("Составной черновик повреждён")
            state = prepared.state.value
            payload = prepared.payload
        else:
            state = current.state
            payload = current.payload

        status, draft = await self._start_or_stage(
            owner_id=command.owner_id,
            owner=owner,
            current=current,
            state=state,
            payload=payload,
            pending_intent=pending_intent,
        )
        return DraftIngressResult(DraftIngressOperation.COMPOSE, status, owner, draft)

    async def begin_repeat(self, command: BeginRepeatDraftCommand) -> DraftIngressResult:
        owner = await self._owners(command.owner_id)
        self._require_owner(owner.owner_id, command.owner_id)
        current = await self._drafts.get_active(command.owner_id)
        self._ensure_can_stage(current, owner)
        occurred_at = self._clock.now(owner.timezone)
        prepared = await self._transactions.prepare_repeat(
            PrepareRepeatDraftCommand(
                command.owner_id,
                command.transaction_id,
                command.expected_version,
                occurred_at,
            )
        )
        if (
            prepared.source_transaction_id != command.transaction_id
            or prepared.source_version != command.expected_version
            or prepared.transaction.occurred_at != occurred_at
        ):
            raise ApplicationValidationError("Повтор операции повреждён")
        status, draft = await self._start_or_stage(
            owner_id=command.owner_id,
            owner=owner,
            current=current,
            state="review",
            payload=prepared.to_payload(),
            pending_intent=encode_pending_draft_intent(PendingRepeatIntent.from_prepared(prepared)),
        )
        return DraftIngressResult(
            DraftIngressOperation.REPEAT,
            status,
            owner,
            draft,
        )
