from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from finbot.adapters.telegram.executor import (
    DuplicateTelegramMutation,
    TelegramMutationExecutor,
    TelegramMutationRequest,
    TelegramMutationSession,
)
from finbot.application.draft_conflicts import (
    PENDING_DRAFT_INTENT_KEY,
    PendingQuickIntent,
    PendingRepeatIntent,
    PendingWizardIntent,
    decode_pending_draft_intent,
    encode_pending_draft_intent,
)
from finbot.application.draft_ingress import (
    BankImportDraftIngressBlockedError,
    BeginQuickDraftCommand,
    BeginRepeatDraftCommand,
    BeginWizardDraftCommand,
    DraftIngressOperation,
    DraftIngressResult,
    DraftIngressStatus,
)
from finbot.application.draft_preparation import (
    DraftPreparationState,
    PreparedDraftResult,
)
from finbot.application.dto import DraftRef
from finbot.application.errors import InvalidStateError
from finbot.application.interactions import MAX_PAGE
from finbot.application.use_cases.draft_ingress import DraftIngressUseCases


def _validate_optional_message_id(message_id: int | None) -> None:
    if isinstance(message_id, bool) or (message_id is not None and not isinstance(message_id, int)):
        raise TypeError("Telegram message id must be an integer")
    if message_id is not None and message_id <= 0:
        raise ValueError("Telegram message id must be positive")


def _validate_optional_history_page(history_page: int | None) -> None:
    if history_page is None:
        return
    if isinstance(history_page, bool) or not isinstance(history_page, int):
        raise TypeError("Transaction history page must be an integer")
    if not 0 <= history_page <= MAX_PAGE:
        raise ValueError(f"Transaction history page must be between 0 and {MAX_PAGE}")


def _decode_receipt_intent(
    value: object,
) -> PendingWizardIntent | PendingQuickIntent | PendingRepeatIntent:
    try:
        intent = decode_pending_draft_intent(value)
    except InvalidStateError:
        raise ValueError("Draft ingress receipt intent is invalid") from None
    if not isinstance(intent, (PendingWizardIntent, PendingQuickIntent, PendingRepeatIntent)):
        raise ValueError("Draft ingress receipt intent has an invalid kind")
    return intent


def _validate_receipt_result(result: DraftIngressResult) -> None:
    draft = result.draft
    if result.status is DraftIngressStatus.STARTED:
        if draft.suspended:
            raise ValueError("Started draft ingress receipt must be active")
        if result.operation is DraftIngressOperation.WIZARD:
            if draft.state != "wizard_type" or dict(draft.payload) != {"flow": "wizard"}:
                raise ValueError("Started wizard draft is not canonical")
            return
        if result.operation in {
            DraftIngressOperation.QUICK,
            DraftIngressOperation.LOCAL_AI,
        }:
            try:
                prepared = PreparedDraftResult(
                    DraftPreparationState(draft.state),
                    draft.payload,
                )
            except TypeError, ValueError:
                raise ValueError("Started quick draft is not canonical") from None
            expected_flow = (
                "quick" if result.operation is DraftIngressOperation.QUICK else "local_ai"
            )
            if prepared.payload.get("flow") != expected_flow:
                raise ValueError("Started quick draft is not canonical")
            return
        encoded: dict[str, object] = {
            "kind": "repeat",
            "state": "review",
            "payload": dict(draft.payload),
        }
        intent = _decode_receipt_intent(encoded)
        if (
            draft.state != "review"
            or not isinstance(intent, PendingRepeatIntent)
            or encode_pending_draft_intent(intent) != encoded
        ):
            raise ValueError("Started repeat draft is not canonical")
        return

    if result.status is DraftIngressStatus.BLOCKED_BANK_IMPORT:
        if (
            result.draft.payload.get("flow") != "bank_import"
            or PENDING_DRAFT_INTENT_KEY in result.draft.payload
        ):
            raise ValueError("Blocked bank import ingress receipt is invalid")
        return

    if result.operation is DraftIngressOperation.LOCAL_AI:
        raise ValueError("Local AI draft ingress must never stage a pending intent")
    pending = draft.payload.get(PENDING_DRAFT_INTENT_KEY)
    if not isinstance(pending, Mapping) or "history_page" in draft.payload:
        raise ValueError("Draft ingress conflict receipt is not channel-neutral")
    intent = _decode_receipt_intent(pending)
    expected_type = {
        DraftIngressOperation.WIZARD: PendingWizardIntent,
        DraftIngressOperation.QUICK: PendingQuickIntent,
        DraftIngressOperation.REPEAT: PendingRepeatIntent,
    }[result.operation]
    if not isinstance(intent, expected_type) or encode_pending_draft_intent(intent) != dict(
        pending
    ):
        raise ValueError("Draft ingress conflict intent is not canonical")


@dataclass(frozen=True, slots=True)
class TelegramDraftIngressContext:
    request: TelegramMutationRequest = field(repr=False)
    message_id: int | None = field(default=None, repr=False)
    history_page: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.request, TelegramMutationRequest):
            raise TypeError("Telegram mutation request is invalid")
        _validate_optional_message_id(self.message_id)
        _validate_optional_history_page(self.history_page)


@dataclass(frozen=True, slots=True)
class DraftIngressReceiptSnapshot:
    result: DraftIngressResult = field(repr=False)
    message_id: int | None = field(default=None, repr=False)
    history_page: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.result, DraftIngressResult):
            raise TypeError("Draft ingress result is invalid")
        _validate_optional_message_id(self.message_id)
        _validate_optional_history_page(self.history_page)
        _validate_receipt_result(self.result)

    @property
    def draft_ref(self) -> DraftRef:
        return self.result.draft.ref


@dataclass(frozen=True, slots=True)
class DraftIngressSessionUseCases:
    ingress: DraftIngressUseCases = field(repr=False)


class DraftIngressUseCaseFactory(Protocol):
    def __call__(
        self,
        session: TelegramMutationSession,
        /,
    ) -> DraftIngressSessionUseCases: ...


type DraftIngressReceiptEnqueuer = Callable[
    [TelegramMutationSession, TelegramMutationRequest, DraftIngressReceiptSnapshot],
    Awaitable[None],
]


class DraftIngressController:
    """Start or stage one typed draft intent in the Telegram update UoW."""

    __slots__ = ("_enqueue_receipt", "_executor", "_use_cases")

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        use_case_factory: DraftIngressUseCaseFactory,
        enqueue_receipt: DraftIngressReceiptEnqueuer,
    ) -> None:
        self._executor = executor
        self._use_cases = use_case_factory
        self._enqueue_receipt = enqueue_receipt

    async def _execute(
        self,
        context: TelegramDraftIngressContext,
        operation: DraftIngressOperation,
        invoke: Callable[[DraftIngressUseCases, UUID], Awaitable[DraftIngressResult]],
    ) -> DraftIngressReceiptSnapshot | None:
        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> DraftIngressResult:
            try:
                return await invoke(self._use_cases(session).ingress, owner_id)
            except BankImportDraftIngressBlockedError as exc:
                return DraftIngressResult(
                    operation,
                    DraftIngressStatus.BLOCKED_BANK_IMPORT,
                    exc.owner,
                    exc.draft,
                )

        def build_receipt(result: DraftIngressResult) -> DraftIngressReceiptSnapshot:
            return DraftIngressReceiptSnapshot(
                result,
                context.message_id,
                context.history_page,
            )

        execution = await self._executor.execute(
            context.request,
            mutate=mutate,
            build_receipt=build_receipt,
            enqueue_receipt=self._enqueue_receipt,
        )
        if isinstance(execution, DuplicateTelegramMutation):
            return None
        return execution.receipt

    async def begin_wizard(
        self,
        context: TelegramDraftIngressContext,
    ) -> DraftIngressReceiptSnapshot | None:
        async def invoke(use_cases: DraftIngressUseCases, owner_id: UUID) -> DraftIngressResult:
            return await use_cases.begin_wizard(BeginWizardDraftCommand(owner_id))

        return await self._execute(context, DraftIngressOperation.WIZARD, invoke)

    async def begin_quick(
        self,
        context: TelegramDraftIngressContext,
        text: str,
    ) -> DraftIngressReceiptSnapshot | None:
        # Reject malformed or oversized text before opening a database UoW.
        PendingQuickIntent(text)

        async def invoke(use_cases: DraftIngressUseCases, owner_id: UUID) -> DraftIngressResult:
            return await use_cases.begin_quick(BeginQuickDraftCommand(owner_id, text))

        return await self._execute(context, DraftIngressOperation.QUICK, invoke)

    async def begin_repeat(
        self,
        context: TelegramDraftIngressContext,
        transaction_id: UUID,
        expected_version: int,
    ) -> DraftIngressReceiptSnapshot | None:
        if not isinstance(transaction_id, UUID):
            raise TypeError("Transaction id must be a UUID")
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 1
        ):
            raise ValueError("Transaction version must be positive")

        async def invoke(use_cases: DraftIngressUseCases, owner_id: UUID) -> DraftIngressResult:
            return await use_cases.begin_repeat(
                BeginRepeatDraftCommand(owner_id, transaction_id, expected_version)
            )

        return await self._execute(context, DraftIngressOperation.REPEAT, invoke)
