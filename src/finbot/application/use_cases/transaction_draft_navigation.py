from collections.abc import Mapping
from typing import Any
from uuid import UUID

from finbot.application.draft_conflicts import PENDING_DRAFT_INTENT_KEY
from finbot.application.dto import DraftRef, DraftSnapshot, UpdateDraftCommand
from finbot.application.errors import (
    ApplicationValidationError,
    DraftRevisionConflictError,
    InvalidStateError,
    ObjectVersionConflictError,
)
from finbot.application.transaction_draft_navigation import (
    TRANSACTION_DRAFT_NAVIGATION_ALLOWED_STATES,
    TRANSACTION_DRAFT_NAVIGATION_TARGET_STATES,
    TransactionDraftNavigationAccountQuery,
    TransactionDraftNavigationAction,
    TransactionDraftNavigationCategoryQuery,
    TransactionDraftNavigationChoices,
    TransactionDraftNavigationCommand,
    TransactionDraftNavigationOwnerQuery,
    TransactionDraftNavigationResult,
    TransactionDraftNavigationTransactionQuery,
)
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.domain.transactions import TransactionType

_EDIT_ACTIONS = frozenset(
    {
        TransactionDraftNavigationAction.EDIT_TYPE,
        TransactionDraftNavigationAction.EDIT_AMOUNT,
        TransactionDraftNavigationAction.EDIT_CATEGORY,
        TransactionDraftNavigationAction.EDIT_ACCOUNT,
        TransactionDraftNavigationAction.EDIT_DATE,
        TransactionDraftNavigationAction.EDIT_DESCRIPTION,
    }
)


def _require_current(current: DraftSnapshot | None, expected: DraftRef) -> DraftSnapshot:
    if current is None or current.ref != expected:
        raise DraftRevisionConflictError(
            current_revision=current.revision if current is not None else None
        )
    if current.suspended:
        raise InvalidStateError("Черновик приостановлен")
    if PENDING_DRAFT_INTENT_KEY in current.payload:
        raise InvalidStateError("Сначала выберите действие для незавершённого ввода")
    return current


def _required_transaction_id(payload: Mapping[str, Any]) -> UUID:
    try:
        return UUID(str(payload["transaction_id"]))
    except KeyError, ValueError:
        raise ApplicationValidationError("Черновик редактирования повреждён") from None


def _required_positive_version(payload: Mapping[str, Any]) -> int:
    raw = payload.get("version")
    if isinstance(raw, bool):
        raise ApplicationValidationError("Черновик редактирования повреждён")
    try:
        version = int(str(raw))
    except ValueError:
        raise ApplicationValidationError("Черновик редактирования повреждён") from None
    if version < 1:
        raise ApplicationValidationError("Черновик редактирования повреждён")
    return version


class TransactionDraftNavigationUseCases:
    """Move an exact saved-transaction edit draft without mutating the transaction."""

    __slots__ = ("_accounts", "_categories", "_drafts", "_owners", "_transactions")

    def __init__(
        self,
        drafts: DraftUseCases,
        owners: TransactionDraftNavigationOwnerQuery,
        transactions: TransactionDraftNavigationTransactionQuery,
        accounts: TransactionDraftNavigationAccountQuery,
        categories: TransactionDraftNavigationCategoryQuery,
    ) -> None:
        self._drafts = drafts
        self._owners = owners
        self._transactions = transactions
        self._accounts = accounts
        self._categories = categories

    async def execute(
        self,
        command: TransactionDraftNavigationCommand,
    ) -> TransactionDraftNavigationResult:
        current = _require_current(
            await self._drafts.get_active(command.owner_id),
            command.expected,
        )
        if current.state not in TRANSACTION_DRAFT_NAVIGATION_ALLOWED_STATES[command.action]:
            raise InvalidStateError("Экран редактирования уже неактуален")

        transaction_id = _required_transaction_id(current.payload)
        transaction = await self._transactions(command.owner_id, transaction_id)
        if transaction.transaction_id != transaction_id:
            raise ApplicationValidationError("Черновик редактирования повреждён")
        if transaction.deleted_at is not None:
            raise InvalidStateError("Операция недоступна")

        version = transaction.version
        if command.action in _EDIT_ACTIONS:
            expected_version = _required_positive_version(current.payload)
            if transaction.version != expected_version:
                raise ObjectVersionConflictError(current_version=transaction.version)

        payload: dict[str, object] = {
            "transaction_id": str(transaction.transaction_id),
            "version": version,
        }
        choices = await self._choices(command, transaction.kind)
        owner = await self._owners(command.owner_id)
        updated = await self._drafts.update(
            UpdateDraftCommand(
                command.owner_id,
                current.ref,
                TRANSACTION_DRAFT_NAVIGATION_TARGET_STATES[command.action],
                payload,
            )
        )
        return TransactionDraftNavigationResult(
            action=command.action,
            owner=owner,
            draft=updated,
            transaction=transaction,
            choices=choices,
        )

    async def _choices(
        self,
        command: TransactionDraftNavigationCommand,
        kind: TransactionType,
    ) -> TransactionDraftNavigationChoices:
        if command.action is TransactionDraftNavigationAction.EDIT_ACCOUNT:
            return TransactionDraftNavigationChoices(
                accounts=tuple(await self._accounts(command.owner_id))
            )
        if command.action is TransactionDraftNavigationAction.EDIT_CATEGORY:
            return TransactionDraftNavigationChoices(
                categories=tuple(await self._categories(command.owner_id, kind=kind))
            )
        return TransactionDraftNavigationChoices()
