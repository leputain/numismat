from uuid import UUID

from finbot.application.draft_conflicts import (
    PENDING_DRAFT_INTENT_KEY,
    DraftConflictReplacementPreparer,
    DraftConflictReplacementTargets,
    DraftConflictRepository,
    DraftConflictResult,
    PendingDraftIntent,
    PendingEditIntent,
    PendingQuickIntent,
    PendingRepeatIntent,
    PendingWizardIntent,
    PreparedDraftConflictReplacement,
    QuickDraftConflictPreparer,
    ResolveDraftConflictCommand,
    decode_pending_draft_intent,
)
from finbot.application.draft_preparation import PrepareQuickDraftCommand
from finbot.application.dto import (
    DraftConflictResolution,
    DraftSnapshot,
    PreparedTransactionDraft,
    PrepareRepeatDraftCommand,
)
from finbot.application.errors import DraftRevisionConflictError, InvalidStateError

_MISSING = object()


def _require_expected(
    current: DraftSnapshot | None,
    command: ResolveDraftConflictCommand,
) -> DraftSnapshot:
    if current is None or current.ref != command.expected:
        raise DraftRevisionConflictError(
            current_revision=current.revision if current is not None else None
        )
    return current


def _repeat_matches_authority(
    intent: PendingRepeatIntent,
    prepared: PreparedTransactionDraft,
) -> bool:
    return (
        intent.source_transaction_id == prepared.source_transaction_id
        and intent.source_version == prepared.source_version
        and intent.transaction == prepared.transaction
        and intent.currency == prepared.currency
        and intent.account_name == prepared.account_name
        and intent.category_name == prepared.category_name
        and intent.category_emoji == prepared.category_emoji
    )


class PrepareDraftConflictReplacement:
    """Turn a typed pending intent into an authoritative replacement draft."""

    __slots__ = ("_quick_drafts", "_targets")

    def __init__(
        self,
        quick_drafts: QuickDraftConflictPreparer,
        targets: DraftConflictReplacementTargets,
    ) -> None:
        self._quick_drafts = quick_drafts
        self._targets = targets

    async def prepare(
        self,
        owner_id: UUID,
        intent: PendingDraftIntent,
    ) -> PreparedDraftConflictReplacement:
        if not isinstance(owner_id, UUID):
            raise TypeError("Draft owner id must be a UUID")
        if isinstance(intent, PendingWizardIntent):
            return PreparedDraftConflictReplacement("wizard_type", {"flow": "wizard"})
        if isinstance(intent, PendingQuickIntent):
            quick_prepared = await self._quick_drafts.execute(
                PrepareQuickDraftCommand(owner_id, intent.text)
            )
            return PreparedDraftConflictReplacement(
                quick_prepared.state.value,
                quick_prepared.payload,
            )
        if isinstance(intent, PendingRepeatIntent):
            if intent.source_transaction_id is None or intent.source_version is None:
                raise InvalidStateError("Новое действие устарело")
            repeated = await self._targets.prepare_repeat(
                PrepareRepeatDraftCommand(
                    owner_id,
                    intent.source_transaction_id,
                    intent.source_version,
                    intent.transaction.occurred_at,
                )
            )
            if not _repeat_matches_authority(intent, repeated):
                raise InvalidStateError("Новое действие устарело")
            return PreparedDraftConflictReplacement("review", repeated.to_payload())
        if isinstance(intent, PendingEditIntent):
            await self._targets.validate_edit(
                owner_id,
                intent.transaction_id,
                intent.expected_version,
            )
            return PreparedDraftConflictReplacement(
                "edit_menu",
                {
                    "transaction_id": str(intent.transaction_id),
                    "version": intent.expected_version,
                },
            )
        raise TypeError("Pending draft intent is invalid")


class ResolveDraftConflict:
    """Resolve one exact draft after taking the durable owner/draft locks."""

    __slots__ = ("_replacements", "_repository")

    def __init__(
        self,
        repository: DraftConflictRepository,
        replacements: DraftConflictReplacementPreparer | None = None,
    ) -> None:
        self._repository = repository
        self._replacements = replacements

    async def execute(self, command: ResolveDraftConflictCommand) -> DraftConflictResult:
        current = _require_expected(
            await self._repository.lock_active(command.owner_id, command.expected),
            command,
        )
        payload = dict(current.payload)
        pending = payload.pop(PENDING_DRAFT_INTENT_KEY, _MISSING)
        intent: PendingDraftIntent | None = None
        if pending is not _MISSING:
            intent = decode_pending_draft_intent(pending)

        if command.resolution is DraftConflictResolution.REPLACE:
            if intent is None:
                raise InvalidStateError("Новое действие уже отменено")
            if self._replacements is None:
                raise InvalidStateError("Безопасная замена черновика пока недоступна")
            replacement = await self._replacements.prepare(command.owner_id, intent)
            updated = await self._repository.replace(
                command.owner_id,
                command.expected,
                replacement.state,
                replacement.payload,
            )
            return DraftConflictResult(command.resolution, updated)

        updated = await self._repository.continue_existing(
            command.owner_id,
            command.expected,
            current.state,
            payload,
        )
        return DraftConflictResult(command.resolution, updated)
