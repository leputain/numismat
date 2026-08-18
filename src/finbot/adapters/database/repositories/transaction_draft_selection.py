from datetime import datetime
from typing import NoReturn, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import (
    Account,
    Category,
    Draft,
    ImportRow,
    Transaction,
    User,
)
from finbot.adapters.database.queries.transactions import get_transaction_details
from finbot.adapters.database.services.transactions import edit_transaction
from finbot.application.draft_conflicts import PENDING_DRAFT_INTENT_KEY
from finbot.application.draft_navigation import DraftCatalogRef, DraftDateChoice
from finbot.application.dto import TransactionMutationResult, TransactionSnapshot
from finbot.application.errors import (
    ApplicationValidationError,
    CatalogUnavailableError,
    DraftRevisionConflictError,
    EntityNotFoundError,
    InvalidStateError,
    ObjectVersionConflictError,
)
from finbot.application.transaction_draft_selection import (
    TRANSACTION_DRAFT_SELECTION_STATES,
    TransactionDraftSelectionAction,
    TransactionDraftSelectionCommand,
    TransactionTypeSelection,
)
from finbot.domain.errors import (
    ObjectNotFoundError,
    StaleObjectError,
    UnknownAccountError,
    UnknownCategoryError,
)
from finbot.domain.transactions import TransactionType


def _required_uuid(payload: dict[str, object], key: str) -> UUID:
    try:
        return UUID(str(payload[key]))
    except KeyError, ValueError:
        raise ApplicationValidationError("Черновик редактирования повреждён") from None


def _required_version(payload: dict[str, object]) -> int:
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


def _catalog_ref(command: TransactionDraftSelectionCommand) -> DraftCatalogRef:
    if not isinstance(command.choice, DraftCatalogRef):  # pragma: no cover - DTO validates this
        raise TypeError("Transaction draft catalog choice is invalid")
    return command.choice


def _type_selection(command: TransactionDraftSelectionCommand) -> TransactionTypeSelection:
    if not isinstance(command.choice, TransactionTypeSelection):  # pragma: no cover - DTO validates
        raise TypeError("Transaction type choice is invalid")
    return command.choice


class SqlAlchemyTransactionDraftSelectionRepository:
    """Linearizable saved-transaction choice inside an external unit of work.

    Lock order is owner -> exact draft -> transaction -> selected catalog. All
    competing catalog mutations take the owner row first, so version and active
    checks cannot race with rename/archive after they have been validated.
    """

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _lock_owner(self, owner_id: UUID) -> None:
        owner = await self._session.scalar(
            select(User.id)
            .where(User.id == owner_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if owner is None:
            raise EntityNotFoundError("Владелец не найден")

    async def _lock_draft(self, command: TransactionDraftSelectionCommand) -> Draft:
        draft = cast(
            Draft | None,
            await self._session.scalar(
                select(Draft)
                .where(Draft.user_id == command.owner_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ),
        )
        if (
            draft is None
            or draft.id != command.expected.draft_id
            or draft.revision != command.expected.revision
        ):
            raise DraftRevisionConflictError(
                current_revision=draft.revision if draft is not None else None
            )
        if draft.suspended or draft.state != TRANSACTION_DRAFT_SELECTION_STATES[command.action]:
            raise InvalidStateError("Экран редактирования уже неактуален")
        if PENDING_DRAFT_INTENT_KEY in draft.payload:
            raise InvalidStateError("Сначала выберите действие для незавершённого ввода")
        import_row_id = await self._session.scalar(
            select(ImportRow.id)
            .where(
                ImportRow.user_id == command.owner_id,
                ImportRow.draft_id == draft.id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if import_row_id is not None:
            raise InvalidStateError(
                "Черновик банковского импорта нельзя изменять через обычный редактор"
            )
        return draft

    async def _lock_transaction(
        self,
        owner_id: UUID,
        transaction_id: UUID,
        expected_version: int,
    ) -> Transaction:
        transaction = cast(
            Transaction | None,
            await self._session.scalar(
                select(Transaction)
                .where(
                    Transaction.id == transaction_id,
                    Transaction.user_id == owner_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ),
        )
        if transaction is None:
            raise EntityNotFoundError("Операция не найдена")
        if transaction.version != expected_version:
            raise ObjectVersionConflictError(current_version=transaction.version)
        if transaction.deleted_at is not None:
            raise InvalidStateError("Удалённую операцию сначала нужно восстановить")
        return transaction

    async def _lock_category(
        self,
        owner_id: UUID,
        kind: str,
        reference: DraftCatalogRef,
    ) -> Category:
        category = cast(
            Category | None,
            await self._session.scalar(
                select(Category)
                .where(Category.id == reference.entity_id, Category.user_id == owner_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ),
        )
        if category is None or category.kind != kind:
            raise CatalogUnavailableError("Категория больше недоступна")
        if category.version != reference.version:
            raise ObjectVersionConflictError(current_version=category.version)
        if category.archived_at is not None:
            raise CatalogUnavailableError("Категория больше недоступна")
        return category

    async def _lock_account(
        self,
        owner_id: UUID,
        reference: DraftCatalogRef,
    ) -> Account:
        account = cast(
            Account | None,
            await self._session.scalar(
                select(Account)
                .where(Account.id == reference.entity_id, Account.user_id == owner_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ),
        )
        if account is None:
            raise CatalogUnavailableError("Счёт больше недоступен")
        if account.version != reference.version:
            raise ObjectVersionConflictError(current_version=account.version)
        if account.archived_at is not None:
            raise CatalogUnavailableError("Счёт больше недоступен")
        return account

    async def _snapshot(self, owner_id: UUID, transaction_id: UUID) -> TransactionSnapshot:
        details = await get_transaction_details(self._session, owner_id, transaction_id)
        if details is None:  # pragma: no cover - the locked row was just updated
            raise EntityNotFoundError("Операция не найдена")
        return TransactionSnapshot(
            transaction_id=details.id,
            kind=TransactionType(details.type),
            amount_minor=details.amount_minor,
            currency=details.currency,
            account_id=details.account_id,
            account_name=details.account_name,
            category_id=details.category_id,
            category_name=details.category_name,
            category_emoji=details.category_emoji,
            occurred_at=details.occurred_at,
            description=details.description,
            source=details.source,
            deleted_at=details.deleted_at,
            version=details.version,
        )

    async def _raise_stale(
        self,
        owner_id: UUID,
        transaction_id: UUID,
        expected_version: int,
    ) -> NoReturn:
        current = await self._session.scalar(
            select(Transaction.version).where(
                Transaction.id == transaction_id,
                Transaction.user_id == owner_id,
            )
        )
        if current is None:
            raise EntityNotFoundError("Операция не найдена")
        if current == expected_version:
            raise InvalidStateError("Операция находится в несовместимом состоянии")
        raise ObjectVersionConflictError(current_version=current)

    async def apply(
        self,
        command: TransactionDraftSelectionCommand,
        *,
        occurred_at: datetime | None = None,
    ) -> TransactionMutationResult:
        await self._lock_owner(command.owner_id)
        draft = await self._lock_draft(command)
        payload = dict(draft.payload)
        transaction_id = _required_uuid(payload, "transaction_id")
        expected_version = _required_version(payload)
        transaction = await self._lock_transaction(
            command.owner_id,
            transaction_id,
            expected_version,
        )

        transaction_type: str | None = None
        category_id: UUID | None = None
        account_id: UUID | None = None
        if command.action is TransactionDraftSelectionAction.TYPE:
            if occurred_at is not None:
                raise ApplicationValidationError("Выбор типа не должен изменять дату")
            selection = _type_selection(command)
            category = await self._lock_category(
                command.owner_id,
                selection.kind.value,
                selection.category,
            )
            transaction_type = selection.kind.value
            category_id = category.id
        elif command.action is TransactionDraftSelectionAction.CATEGORY:
            if occurred_at is not None:
                raise ApplicationValidationError("Выбор категории не должен изменять дату")
            category = await self._lock_category(
                command.owner_id,
                transaction.type,
                _catalog_ref(command),
            )
            category_id = category.id
        elif command.action is TransactionDraftSelectionAction.ACCOUNT:
            if occurred_at is not None:
                raise ApplicationValidationError("Выбор счёта не должен изменять дату")
            account = await self._lock_account(command.owner_id, _catalog_ref(command))
            account_id = account.id
        else:
            if command.choice is DraftDateChoice.CUSTOM or occurred_at is None:
                raise InvalidStateError("Дата операции требует отдельного ввода")
            if occurred_at.utcoffset() is None:
                raise ApplicationValidationError("Дата операции должна содержать часовой пояс")

        try:
            updated = await edit_transaction(
                self._session,
                command.owner_id,
                transaction_id,
                expected_version,
                transaction_type=transaction_type,
                category_id=category_id,
                account_id=account_id,
                occurred_at=occurred_at,
            )
        except ObjectNotFoundError:
            raise EntityNotFoundError("Операция не найдена") from None
        except StaleObjectError:
            await self._raise_stale(command.owner_id, transaction_id, expected_version)
        except UnknownAccountError, UnknownCategoryError:
            raise CatalogUnavailableError("Счёт или категория недоступны") from None
        except ValueError:
            raise ApplicationValidationError("Изменения операции некорректны") from None

        await self._session.delete(draft)
        await self._session.flush()
        snapshot = await self._snapshot(command.owner_id, updated.id)
        return TransactionMutationResult(
            entity_id=snapshot.transaction_id,
            version=snapshot.version,
            resulting_state="updated",
            transaction=snapshot,
        )
