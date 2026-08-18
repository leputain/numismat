from finbot.application.draft_conflicts import PENDING_DRAFT_INTENT_KEY
from finbot.application.dto import (
    ConfirmTransactionDraftCommand,
    DraftSnapshot,
    EditTransactionCommand,
    PrepareRepeatDraftCommand,
    TransactionMutationResult,
    VersionedTransactionCommand,
)
from finbot.application.errors import (
    ApplicationValidationError,
    DraftRevisionConflictError,
    InvalidStateError,
    ReviewRequiredError,
)
from finbot.application.ports import DraftRepository, TransactionCommandRepository

_REVIEW_STATES = frozenset({"review", "quick_confirm", "wizard_confirm"})
_SPECIALIZED_REVIEW_KEYS = frozenset({"ocr_batch", PENDING_DRAFT_INTENT_KEY})


class TransactionUseCases:
    """Framework-neutral reviewed transaction lifecycle.

    Both repositories must participate in the same adapter-owned unit of work.
    The database adapter repeats the draft checks under locks, so this initial
    read is only an early typed-error gate and never the authority for saving.
    """

    __slots__ = ("_commands", "_drafts")

    def __init__(
        self,
        commands: TransactionCommandRepository,
        drafts: DraftRepository,
    ) -> None:
        self._commands = commands
        self._drafts = drafts

    async def confirm(self, command: ConfirmTransactionDraftCommand) -> TransactionMutationResult:
        active = await self._drafts.get_active(command.owner_id)
        if active is None or active.ref != command.expected:
            raise DraftRevisionConflictError(
                current_revision=active.revision if active is not None else None
            )
        if active.suspended or active.state not in _REVIEW_STATES:
            raise ReviewRequiredError("Сначала проверьте черновик операции")
        if _SPECIALIZED_REVIEW_KEYS.intersection(active.payload):
            raise InvalidStateError("Черновик требует специализированной обработки")
        return await self._commands.confirm_reviewed_draft(command)

    async def edit(self, command: EditTransactionCommand) -> TransactionMutationResult:
        if not command.has_changes:
            raise ApplicationValidationError("Не указаны изменения операции")
        return await self._commands.edit(command)

    async def delete(self, command: VersionedTransactionCommand) -> TransactionMutationResult:
        return await self._commands.delete(command)

    async def restore(self, command: VersionedTransactionCommand) -> TransactionMutationResult:
        return await self._commands.restore(command)

    async def prepare_repeat(self, command: PrepareRepeatDraftCommand) -> DraftSnapshot:
        prepared = await self._commands.prepare_repeat(command)
        return await self._drafts.create_if_absent(
            command.owner_id,
            "quick_confirm",
            prepared.to_payload(),
        )
