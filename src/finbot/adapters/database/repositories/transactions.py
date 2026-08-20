from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.category_rules import SqlAlchemyCategoryRuleRepository
from finbot.adapters.database.models import (
    Account,
    Category,
    Draft,
    ImportBatch,
    ImportRow,
    RecurringInstance,
    Transaction,
    User,
)
from finbot.adapters.database.queries.transactions import get_transaction_details
from finbot.adapters.database.services.transactions import (
    edit_transaction,
    restore_transaction,
    save_transaction,
    soft_delete_transaction,
)
from finbot.application.draft_conflicts import PENDING_DRAFT_INTENT_KEY
from finbot.application.dto import (
    ConfirmTransactionDraftCommand,
    EditTransactionCommand,
    PreparedTransactionDraft,
    PrepareRepeatDraftCommand,
    ReviewedTransactionInput,
    TransactionMutationResult,
    TransactionSnapshot,
    VersionedTransactionCommand,
)
from finbot.application.errors import (
    ApplicationValidationError,
    CatalogUnavailableError,
    DraftRevisionConflictError,
    EntityNotFoundError,
    InvalidStateError,
    ObjectVersionConflictError,
    ReviewRequiredError,
)
from finbot.application.rules import validate_staged_rule
from finbot.domain.errors import (
    ObjectNotFoundError,
    StaleObjectError,
    UnknownAccountError,
    UnknownCategoryError,
)
from finbot.domain.transactions import TransactionDraft, TransactionType

_REVIEW_STATES = frozenset({"review", "quick_confirm", "wizard_confirm"})
_SPECIALIZED_REVIEW_KEYS = frozenset({"ocr_batch", PENDING_DRAFT_INTENT_KEY})
_IMPORT_FORBIDDEN_REVIEW_KEYS = frozenset({"pending_rule", "rule_offer_pattern"})
_MAX_VERSION = 2**31 - 1


def _next_recurring_version(current: int) -> int:
    if current >= _MAX_VERSION:
        raise RuntimeError("recurring instance version exhausted")
    return current + 1


def _next_import_version(current: int) -> int:
    if current >= _MAX_VERSION:
        raise InvalidStateError("Достигнут предел версий импорта")
    return current + 1


def _validated_pending_rule(payload: Mapping[str, Any]) -> tuple[str, str] | None:
    """Revalidate untrusted staged learning metadata against final review values."""

    pending = payload.get("pending_rule")
    if not isinstance(pending, Mapping):
        return None
    pattern = str(pending.get("pattern", "")).strip()
    scope = str(pending.get("scope", ""))
    if pattern != str(payload.get("rule_offer_pattern", "")).strip():
        return None
    if str(pending.get("account_id", "")) != str(payload.get("account_id", "")):
        return None
    if str(pending.get("category_id", "")) != str(payload.get("category_id", "")):
        return None
    return validate_staged_rule(
        pattern,
        scope,
        str(payload.get("description", "")),
        flow=str(payload.get("flow", "")),
        category_explicit=bool(payload.get("category_explicit")),
    )


class SqlAlchemyTransactionCommandRepository:
    """Owner-scoped transaction commands inside an externally owned unit of work.

    Every path takes the owner row first. Catalog archive commands use the same
    lock order, making confirm/edit versus archive outcomes linearizable rather
    than dependent on a stale pre-check.
    """

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _lock_owner(self, owner_id: UUID) -> User:
        owner = await self._session.scalar(
            select(User)
            .where(User.id == owner_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if owner is None:
            raise EntityNotFoundError("Владелец не найден")
        return owner

    async def _locked_plain_review_draft(
        self,
        command: ConfirmTransactionDraftCommand,
    ) -> Draft:
        draft = await self._session.scalar(
            select(Draft)
            .where(Draft.user_id == command.owner_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            draft is None
            or draft.id != command.expected.draft_id
            or (draft.revision != command.expected.revision)
        ):
            raise DraftRevisionConflictError(
                current_revision=draft.revision if draft is not None else None
            )
        if draft.suspended or draft.state not in _REVIEW_STATES:
            raise ReviewRequiredError("Сначала проверьте черновик операции")
        if _SPECIALIZED_REVIEW_KEYS.intersection(draft.payload):
            raise InvalidStateError("Черновик требует специализированной обработки")
        return draft

    async def _locked_review_draft(
        self,
        command: ConfirmTransactionDraftCommand,
    ) -> tuple[Draft, ReviewedTransactionInput]:
        draft = await self._locked_plain_review_draft(command)
        try:
            reviewed = ReviewedTransactionInput.from_payload(draft.payload)
        except TypeError, ValueError:
            raise ReviewRequiredError("Черновик операции неполон") from None
        return draft, reviewed

    async def _active_catalogs(
        self,
        owner_id: UUID,
        reviewed: ReviewedTransactionInput,
    ) -> tuple[Account, Category]:
        account = await self._session.scalar(
            select(Account)
            .where(
                Account.id == reviewed.account_id,
                Account.user_id == owner_id,
                Account.archived_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        category = await self._session.scalar(
            select(Category)
            .where(
                Category.id == reviewed.category_id,
                Category.user_id == owner_id,
                Category.kind == reviewed.kind.value,
                Category.archived_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if account is None or category is None:
            raise CatalogUnavailableError("Счёт или категория недоступны")
        return account, category

    async def _snapshot(self, owner_id: UUID, transaction_id: UUID) -> TransactionSnapshot:
        details = await get_transaction_details(self._session, owner_id, transaction_id)
        if details is None:
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

    @staticmethod
    def _result(snapshot: TransactionSnapshot, state: str) -> TransactionMutationResult:
        return TransactionMutationResult(
            entity_id=snapshot.transaction_id,
            version=snapshot.version,
            resulting_state=state,
            transaction=snapshot,
        )

    async def _raise_stale(
        self,
        owner_id: UUID,
        transaction_id: UUID,
        expected_version: int,
    ) -> NoReturn:
        row = (
            await self._session.execute(
                select(Transaction.version, Transaction.deleted_at).where(
                    Transaction.id == transaction_id,
                    Transaction.user_id == owner_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise EntityNotFoundError("Операция не найдена")
        current_version, _deleted_at = row._t
        if current_version == expected_version:
            raise InvalidStateError("Операция находится в несовместимом состоянии")
        raise ObjectVersionConflictError(current_version=current_version)

    async def confirm_reviewed_draft(
        self,
        command: ConfirmTransactionDraftCommand,
    ) -> TransactionMutationResult:
        owner = await self._lock_owner(command.owner_id)
        draft, reviewed = await self._locked_review_draft(command)
        account, category = await self._active_catalogs(command.owner_id, reviewed)
        recurring_instance = await self._session.scalar(
            select(RecurringInstance)
            .where(
                RecurringInstance.user_id == command.owner_id,
                RecurringInstance.draft_id == draft.id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if recurring_instance is not None and recurring_instance.status != "generated":
            raise InvalidStateError("Экземпляр расписания находится в несовместимом состоянии")
        import_row = await self._session.scalar(
            select(ImportRow)
            .where(
                ImportRow.user_id == command.owner_id,
                ImportRow.draft_id == draft.id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if recurring_instance is not None and import_row is not None:
            raise InvalidStateError("Черновик содержит конфликтующие источники")
        import_batch: ImportBatch | None = None
        if import_row is not None:
            if _IMPORT_FORBIDDEN_REVIEW_KEYS.intersection(draft.payload):
                raise InvalidStateError("Черновик импорта не может создавать правила")
            import_batch = await self._session.scalar(
                select(ImportBatch)
                .where(
                    ImportBatch.id == import_row.batch_id,
                    ImportBatch.user_id == command.owner_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                import_batch is None
                or import_batch.status != "open"
                or import_row.status != "staged"
                or import_row.draft_id != draft.id
            ):
                raise InvalidStateError("Источник черновика импорта повреждён")
            if (
                import_batch.account_id != reviewed.account_id
                or import_row.type != reviewed.kind.value
                or import_row.amount_minor != reviewed.amount_minor
                or import_row.occurred_at.astimezone(UTC) != reviewed.occurred_at.astimezone(UTC)
                or import_row.currency != account.currency
                or draft.payload.get("currency") != import_row.currency
            ):
                raise InvalidStateError("Поля банковской операции нельзя изменить до сверки")

        pending_rule = _validated_pending_rule(draft.payload)
        if pending_rule is not None:
            pattern, scope = pending_rule
            await SqlAlchemyCategoryRuleRepository(self._session).upsert(
                command.owner_id,
                reviewed.kind,
                reviewed.category_id,
                pattern,
                reviewed.account_id if scope == "account" else None,
            )
        try:
            transaction = await save_transaction(
                self._session,
                command.owner_id,
                TransactionDraft(
                    amount_minor=reviewed.amount_minor,
                    type=reviewed.kind,
                    occurred_at=reviewed.occurred_at,
                    category_hint=category.slug,
                    category_explicit=True,
                    account_hint=account.slug,
                    description=reviewed.description,
                ),
                currency=owner.base_currency,
                default_account_id=owner.default_account_id,
                source=(
                    "recurring"
                    if recurring_instance is not None
                    else "bank_import"
                    if import_row is not None
                    else "manual"
                ),
                recurring_instance_id=(
                    recurring_instance.id if recurring_instance is not None else None
                ),
                import_row_id=import_row.id if import_row is not None else None,
            )
        except UnknownAccountError, UnknownCategoryError:
            raise CatalogUnavailableError("Счёт или категория недоступны") from None
        if recurring_instance is not None:
            recurring_instance.draft_id = None
            recurring_instance.version = _next_recurring_version(recurring_instance.version)
            recurring_instance.updated_at = datetime.now(UTC)
        if import_row is not None and import_batch is not None:
            now = datetime.now(UTC)
            import_row.status = "confirmed"
            import_row.draft_id = None
            import_row.resolved_at = now
            import_row.version = _next_import_version(import_row.version)
            import_row.updated_at = now
            await self._session.flush()
            unresolved = await self._session.scalar(
                select(func.count(ImportRow.id)).where(
                    ImportRow.batch_id == import_batch.id,
                    ImportRow.user_id == command.owner_id,
                    ImportRow.status.in_(("pending", "staged")),
                )
            )
            import_batch.version = _next_import_version(import_batch.version)
            import_batch.updated_at = now
            if int(unresolved or 0) == 0:
                import_batch.status = "completed"
                import_batch.completed_at = now
        await self._session.delete(draft)
        await self._session.flush()
        return self._result(
            await self._snapshot(command.owner_id, transaction.id),
            "confirmed",
        )

    async def edit(self, command: EditTransactionCommand) -> TransactionMutationResult:
        await self._lock_owner(command.owner_id)
        try:
            transaction = await edit_transaction(
                self._session,
                command.owner_id,
                command.transaction_id,
                command.expected_version,
                amount_minor=command.amount_minor,
                category_id=command.category_id,
                account_id=command.account_id,
                occurred_at=command.occurred_at,
                description=command.description,
            )
        except ObjectNotFoundError:
            raise EntityNotFoundError("Операция не найдена") from None
        except StaleObjectError:
            await self._raise_stale(
                command.owner_id,
                command.transaction_id,
                command.expected_version,
            )
        except UnknownAccountError, UnknownCategoryError:
            raise CatalogUnavailableError("Счёт или категория недоступны") from None
        except ValueError:
            raise ApplicationValidationError("Изменения операции некорректны") from None
        return self._result(
            await self._snapshot(command.owner_id, transaction.id),
            "updated",
        )

    async def delete(
        self,
        command: VersionedTransactionCommand,
    ) -> TransactionMutationResult:
        await self._lock_owner(command.owner_id)
        try:
            transaction = await soft_delete_transaction(
                self._session,
                command.owner_id,
                command.transaction_id,
                command.expected_version,
            )
        except ObjectNotFoundError:
            raise EntityNotFoundError("Операция не найдена") from None
        except StaleObjectError:
            await self._raise_stale(
                command.owner_id,
                command.transaction_id,
                command.expected_version,
            )
        return self._result(
            await self._snapshot(command.owner_id, transaction.id),
            "deleted",
        )

    async def restore(
        self,
        command: VersionedTransactionCommand,
    ) -> TransactionMutationResult:
        await self._lock_owner(command.owner_id)
        try:
            transaction = await restore_transaction(
                self._session,
                command.owner_id,
                command.transaction_id,
                command.expected_version,
            )
        except ObjectNotFoundError:
            raise EntityNotFoundError("Операция не найдена") from None
        except StaleObjectError:
            await self._raise_stale(
                command.owner_id,
                command.transaction_id,
                command.expected_version,
            )
        return self._result(
            await self._snapshot(command.owner_id, transaction.id),
            "active",
        )

    async def prepare_repeat(
        self,
        command: PrepareRepeatDraftCommand,
    ) -> PreparedTransactionDraft:
        await self._lock_owner(command.owner_id)
        row = (
            await self._session.execute(
                select(Transaction, Account, Category)
                .join(Account, Transaction.account_id == Account.id)
                .join(Category, Transaction.category_id == Category.id)
                .where(
                    Transaction.id == command.transaction_id,
                    Transaction.user_id == command.owner_id,
                )
                .with_for_update(of=Transaction)
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if row is None:
            raise EntityNotFoundError("Операция не найдена")
        transaction, account, category = row._t
        if transaction.version != command.expected_version:
            raise ObjectVersionConflictError(current_version=transaction.version)
        if transaction.deleted_at is not None:
            raise InvalidStateError("Удалённую операцию нельзя повторить")
        if account.archived_at is not None or category.archived_at is not None:
            raise CatalogUnavailableError("Счёт или категория недоступны")
        return PreparedTransactionDraft(
            source_transaction_id=transaction.id,
            source_version=transaction.version,
            transaction=ReviewedTransactionInput(
                kind=TransactionType(transaction.type),
                amount_minor=transaction.amount_minor,
                account_id=transaction.account_id,
                category_id=transaction.category_id,
                occurred_at=command.occurred_at,
                description=transaction.description,
            ),
            currency=transaction.currency,
            account_name=account.name,
            category_name=category.name,
            category_emoji=category.emoji,
        )
