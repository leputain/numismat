from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from finbot.application.draft_conflicts import PENDING_DRAFT_INTENT_KEY
from finbot.application.dto import EditTransactionCommand, UpdateDraftCommand
from finbot.application.errors import ApplicationValidationError, InvalidStateError
from finbot.application.transaction_edit_text_input import (
    TransactionEditTextInputClock,
    TransactionEditTextInputCommand,
    TransactionEditTextInputError,
    TransactionEditTextInputField,
    TransactionEditTextInputResult,
    TransactionEditTextInputStatus,
    TransactionEditTextTarget,
    TransactionEditTextTargetRepository,
)
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.application.use_cases.transactions import TransactionUseCases
from finbot.domain.dates import parse_local_datetime
from finbot.domain.money import MoneyError, parse_minor


class SystemTransactionEditTextInputClock:
    def now(self, timezone: str) -> datetime:
        return datetime.now(ZoneInfo(timezone))


class TransactionEditTextInputUseCase:
    """Apply one exact transaction edit without owning commit or delivery."""

    __slots__ = ("_clock", "_drafts", "_targets", "_transactions")

    def __init__(
        self,
        targets: TransactionEditTextTargetRepository,
        transactions: TransactionUseCases,
        drafts: DraftUseCases,
        clock: TransactionEditTextInputClock | None = None,
    ) -> None:
        self._targets = targets
        self._transactions = transactions
        self._drafts = drafts
        self._clock = clock or SystemTransactionEditTextInputClock()

    async def execute(
        self,
        command: TransactionEditTextInputCommand,
    ) -> TransactionEditTextInputResult:
        target = await self._targets.lock_target(command.owner_id, command.expected)
        if PENDING_DRAFT_INTENT_KEY in target.draft.payload:
            raise InvalidStateError("Сначала выберите действие для незавершённого ввода")
        if target.owner.owner_id != command.owner_id or target.draft.ref != command.expected:
            raise ApplicationValidationError("Контекст редактирования операции повреждён")

        edit, retry_error = self._parse(command, target)
        if retry_error is not None:
            retry_draft = await self._drafts.update(
                UpdateDraftCommand(
                    owner_id=command.owner_id,
                    expected=command.expected,
                    state=target.draft.state,
                    payload=target.draft.payload,
                )
            )
            return TransactionEditTextInputResult(
                status=TransactionEditTextInputStatus.RETRY,
                edited_field=self._field(target),
                owner=target.owner,
                transaction=target.transaction,
                draft=retry_draft,
                retry_error=retry_error,
            )

        if edit is None:  # pragma: no cover - the typed retry branch returned above
            raise RuntimeError("Transaction edit parser produced no command")
        mutation = await self._transactions.edit(edit)
        await self._drafts.cancel(command.owner_id, command.expected)
        return TransactionEditTextInputResult(
            status=TransactionEditTextInputStatus.UPDATED,
            edited_field=self._field(target),
            owner=target.owner,
            transaction=mutation.transaction,
        )

    @staticmethod
    def _field(target: TransactionEditTextTarget) -> TransactionEditTextInputField:
        return {
            "edit_amount": TransactionEditTextInputField.AMOUNT,
            "edit_date": TransactionEditTextInputField.DATE,
            "edit_description": TransactionEditTextInputField.DESCRIPTION,
        }[target.draft.state]

    def _parse(
        self,
        command: TransactionEditTextInputCommand,
        target: TransactionEditTextTarget,
    ) -> tuple[EditTransactionCommand | None, TransactionEditTextInputError | None]:
        if target.draft.state == "edit_amount":
            try:
                amount_minor = parse_minor(command.text)
            except MoneyError:
                return None, TransactionEditTextInputError.INVALID_AMOUNT
            return EditTransactionCommand(
                owner_id=command.owner_id,
                transaction_id=target.transaction.transaction_id,
                expected_version=target.transaction.version,
                amount_minor=amount_minor,
            ), None

        if target.draft.state == "edit_date":
            try:
                now = self._clock.now(target.owner.timezone)
                if now.utcoffset() is None:
                    raise ApplicationValidationError("Текущее время должно содержать часовой пояс")
                occurred_at = parse_local_datetime(
                    command.text,
                    target.owner.timezone,
                    now=now,
                )
            except ZoneInfoNotFoundError:
                raise ApplicationValidationError(
                    "Часовой пояс владельца не поддерживается"
                ) from None
            except ValueError:
                return None, TransactionEditTextInputError.INVALID_DATE
            return EditTransactionCommand(
                owner_id=command.owner_id,
                transaction_id=target.transaction.transaction_id,
                expected_version=target.transaction.version,
                occurred_at=occurred_at,
            ), None

        description = command.text.strip()
        if len(description) > 500:
            return None, TransactionEditTextInputError.INVALID_DESCRIPTION
        return EditTransactionCommand(
            owner_id=command.owner_id,
            transaction_id=target.transaction.transaction_id,
            expected_version=target.transaction.version,
            description="" if description == "-" else description,
        ), None
