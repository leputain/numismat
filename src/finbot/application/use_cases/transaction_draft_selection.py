from datetime import datetime
from zoneinfo import ZoneInfo

from finbot.application.draft_conflicts import PENDING_DRAFT_INTENT_KEY
from finbot.application.draft_navigation import DraftDateChoice
from finbot.application.dto import DraftRef, DraftSnapshot, UpdateDraftCommand
from finbot.application.errors import (
    DraftRevisionConflictError,
    InvalidStateError,
)
from finbot.application.transaction_draft_selection import (
    TRANSACTION_DRAFT_SELECTION_STATES,
    TransactionDraftSelectionAction,
    TransactionDraftSelectionClock,
    TransactionDraftSelectionCommand,
    TransactionDraftSelectionOwnerQuery,
    TransactionDraftSelectionRepository,
    TransactionDraftSelectionResult,
    TransactionDraftSelectionStatus,
)
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.domain.dates import parse_local_datetime


class SystemTransactionDraftSelectionClock:
    """Timezone-aware production clock kept behind an injectable port."""

    def now(self, timezone: str) -> datetime:
        return datetime.now(ZoneInfo(timezone))


def _require_current(
    current: DraftSnapshot | None,
    expected: DraftRef,
    action: TransactionDraftSelectionAction,
) -> DraftSnapshot:
    if current is None or current.ref != expected:
        raise DraftRevisionConflictError(
            current_revision=current.revision if current is not None else None
        )
    if current.suspended:
        raise InvalidStateError("Черновик редактирования приостановлен")
    if PENDING_DRAFT_INTENT_KEY in current.payload:
        raise InvalidStateError("Сначала выберите действие для незавершённого ввода")
    if current.state != TRANSACTION_DRAFT_SELECTION_STATES[action]:
        raise InvalidStateError("Экран редактирования уже неактуален")
    return current


def _date_choice(command: TransactionDraftSelectionCommand) -> DraftDateChoice:
    if not isinstance(command.choice, DraftDateChoice):  # pragma: no cover - DTO validates this
        raise TypeError("Transaction draft date choice is invalid")
    return command.choice


class TransactionDraftSelectionUseCases:
    """Apply an exact saved-transaction choice without a second adapter session."""

    __slots__ = ("_clock", "_drafts", "_owners", "_repository")

    def __init__(
        self,
        drafts: DraftUseCases,
        owners: TransactionDraftSelectionOwnerQuery,
        repository: TransactionDraftSelectionRepository,
        clock: TransactionDraftSelectionClock | None = None,
    ) -> None:
        self._drafts = drafts
        self._owners = owners
        self._repository = repository
        self._clock = clock or SystemTransactionDraftSelectionClock()

    async def execute(
        self,
        command: TransactionDraftSelectionCommand,
    ) -> TransactionDraftSelectionResult:
        current = _require_current(
            await self._drafts.get_active(command.owner_id),
            command.expected,
            command.action,
        )
        owner = await self._owners(command.owner_id)

        if (
            command.action is TransactionDraftSelectionAction.DATE
            and _date_choice(command) is DraftDateChoice.CUSTOM
        ):
            payload = dict(current.payload)
            payload.pop("history_page", None)
            updated = await self._drafts.update(
                UpdateDraftCommand(
                    owner_id=command.owner_id,
                    expected=current.ref,
                    state="edit_date",
                    payload=payload,
                )
            )
            return TransactionDraftSelectionResult(
                action=command.action,
                status=TransactionDraftSelectionStatus.DATE_INPUT_REQUIRED,
                owner=owner,
                draft=updated,
            )

        occurred_at: datetime | None = None
        if command.action is TransactionDraftSelectionAction.DATE:
            choice = _date_choice(command)
            occurred_at = parse_local_datetime(
                choice.value,
                owner.timezone,
                now=self._clock.now(owner.timezone),
            )
        mutation = await self._repository.apply(command, occurred_at=occurred_at)
        return TransactionDraftSelectionResult(
            action=command.action,
            status=TransactionDraftSelectionStatus.TRANSACTION_UPDATED,
            owner=owner,
            transaction=mutation.transaction,
        )
