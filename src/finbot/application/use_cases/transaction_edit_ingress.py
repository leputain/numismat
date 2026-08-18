from finbot.application.draft_conflicts import (
    PENDING_DRAFT_INTENT_KEY,
    PendingEditIntent,
    encode_pending_draft_intent,
)
from finbot.application.draft_ingress import BankImportDraftIngressBlockedError
from finbot.application.dto import CreateDraftCommand, UpdateDraftCommand
from finbot.application.errors import ApplicationValidationError, InvalidStateError
from finbot.application.transaction_edit_ingress import (
    BeginTransactionEditCommand,
    BeginTransactionEditResult,
    TransactionEditIngressStatus,
    TransactionEditOwnerReader,
    TransactionEditTargetReader,
)
from finbot.application.use_cases.drafts import DraftUseCases


class BeginTransactionEdit:
    """Open editing or stage a bounded edit intent without replacing a draft."""

    __slots__ = ("_drafts", "_owners", "_transactions")

    def __init__(
        self,
        drafts: DraftUseCases,
        owners: TransactionEditOwnerReader,
        transactions: TransactionEditTargetReader,
    ) -> None:
        self._drafts = drafts
        self._owners = owners
        self._transactions = transactions

    async def execute(self, command: BeginTransactionEditCommand) -> BeginTransactionEditResult:
        transaction = await self._transactions(
            command.owner_id,
            command.transaction_id,
            command.expected_version,
        )
        if (
            transaction.transaction_id != command.transaction_id
            or transaction.version != command.expected_version
        ):
            raise ApplicationValidationError("Операция редактирования повреждена")

        active = await self._drafts.get_active(command.owner_id)
        if active is None:
            draft = await self._drafts.create(
                CreateDraftCommand(
                    owner_id=command.owner_id,
                    state="edit_menu",
                    payload={
                        "transaction_id": str(transaction.transaction_id),
                        "version": transaction.version,
                    },
                )
            )
            status = TransactionEditIngressStatus.DRAFT_CREATED
        else:
            if active.payload.get("flow") == "bank_import":
                owner = await self._owners(command.owner_id)
                raise BankImportDraftIngressBlockedError(owner, active, transaction)
            payload = dict(active.payload)
            if PENDING_DRAFT_INTENT_KEY in payload:
                raise InvalidStateError("Сначала завершите выбор для незавершённого ввода")
            # History location belongs to the Telegram receipt/callback, not to
            # the channel-neutral draft.  Drop rolling legacy metadata while
            # touching this exact revision instead of propagating it further.
            payload.pop("history_page", None)
            payload[PENDING_DRAFT_INTENT_KEY] = encode_pending_draft_intent(
                PendingEditIntent(transaction.transaction_id, transaction.version)
            )
            draft = await self._drafts.update(
                UpdateDraftCommand(
                    owner_id=command.owner_id,
                    expected=active.ref,
                    state=active.state,
                    payload=payload,
                )
            )
            status = TransactionEditIngressStatus.CONFLICT_STAGED

        owner = await self._owners(command.owner_id)
        return BeginTransactionEditResult(status, owner, transaction, draft)
