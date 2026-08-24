from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid7

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from finbot.adapters.database.models import (
    Account,
    Category,
    Draft,
    ImportBatch,
    ImportRow,
    Transaction,
    User,
)
from finbot.adapters.database.repositories.draft_preparation import (
    SqlAlchemyDraftPreparationRepository,
)
from finbot.adapters.database.repositories.queries import SqlAlchemyQueryRepository
from finbot.application.bank_imports import (
    MAX_BANK_IMPORT_PAGE_SIZE,
    BankImportAccountCurrencyMismatchError,
    BankImportBatchCursor,
    BankImportBatchCursorItem,
    BankImportBatchSnapshot,
    BankImportBatchState,
    BankImportCommandRepository,
    BankImportCounts,
    BankImportDraftResult,
    BankImportEncoding,
    BankImportMutationResult,
    BankImportProfile,
    BankImportReader,
    BankImportRowCursor,
    BankImportRowCursorItem,
    BankImportRowSnapshot,
    BankImportRowState,
    CancelBankImportBatchCommand,
    LinkBankImportRowCommand,
    StageBankImportBatchCommand,
    VersionedBankImportRowCommand,
)
from finbot.application.draft_preparation import DraftPreparationState
from finbot.application.dto import DraftRef, OwnerSnapshot, TransactionSnapshot
from finbot.application.errors import (
    ActiveDraftConflictError,
    CatalogUnavailableError,
    EntityNotFoundError,
    InvalidStateError,
    ObjectVersionConflictError,
)
from finbot.application.use_cases.draft_preparation import (
    PrepareParsedDraft,
    SystemDraftPreparationClock,
)
from finbot.domain.bank_imports import reconciliation_bounds
from finbot.domain.transactions import TransactionDraft, TransactionType

_MAX_FETCH_LIMIT = MAX_BANK_IMPORT_PAGE_SIZE + 1
_MAX_CANDIDATE_FETCH = 6
_MAX_VERSION = 2**31 - 1
_ACTIONABLE_STATES = frozenset({"pending", "staged"})
_IMPORT_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {"pending_intent", "ocr_batch", "pending_rule", "rule_offer_pattern"}
)


def _next_version(current: int, *, entity: str) -> int:
    if current >= _MAX_VERSION:
        raise InvalidStateError(f"Достигнут предел версий {entity}")
    return current + 1


def _owner_snapshot(owner: User) -> OwnerSnapshot:
    return OwnerSnapshot(
        owner_id=owner.id,
        locale=owner.locale,
        timezone=owner.timezone,
        base_currency=owner.base_currency,
        default_account_id=owner.default_account_id,
        fast_mode=owner.fast_mode,
        settings_version=owner.settings_version,
    )


def _transaction_snapshot(
    transaction: Transaction,
    account_name: str,
    category_name: str,
    category_emoji: str,
) -> TransactionSnapshot:
    return TransactionSnapshot(
        transaction_id=transaction.id,
        kind=TransactionType(transaction.type),
        amount_minor=transaction.amount_minor,
        currency=transaction.currency,
        account_id=transaction.account_id,
        account_name=account_name,
        category_id=transaction.category_id,
        category_name=category_name,
        category_emoji=category_emoji,
        occurred_at=transaction.occurred_at,
        description=transaction.description,
        source=transaction.source,
        deleted_at=transaction.deleted_at,
        version=transaction.version,
    )


def _row_snapshot(
    row: ImportRow,
    *,
    transaction_id: UUID | None,
    possible_duplicate: bool,
) -> BankImportRowSnapshot:
    return BankImportRowSnapshot(
        row_id=row.id,
        batch_id=row.batch_id,
        owner_id=row.user_id,
        position=row.position,
        occurred_at=row.occurred_at,
        kind=TransactionType(row.type),
        amount_minor=row.amount_minor,
        currency=row.currency,
        description=row.description,
        state=BankImportRowState(row.status),
        has_reference=row.reference_digest is not None,
        possible_duplicate=possible_duplicate,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        draft_id=row.draft_id,
        transaction_id=transaction_id,
        resolved_at=row.resolved_at,
    )


def _validate_import_draft_payload(
    row: ImportRow,
    expected_account_id: UUID,
    state: str,
    payload: Mapping[str, Any],
) -> None:
    try:
        occurred_at = datetime.fromisoformat(str(payload.get("occurred_at", "")))
        amount_minor = int(payload.get("amount_minor", 0))
        parsed_account_id = UUID(str(payload.get("account_id", "")))
    except TypeError, ValueError:
        raise InvalidStateError("Черновик импорта повреждён") from None
    if occurred_at.utcoffset() is None:
        raise InvalidStateError("Черновик импорта повреждён")
    if (
        state != "review"
        or payload.get("flow") != "bank_import"
        or _IMPORT_FORBIDDEN_PAYLOAD_KEYS.intersection(payload)
        or payload.get("type") != row.type
        or amount_minor != row.amount_minor
        or parsed_account_id != expected_account_id
        or payload.get("currency") != row.currency
        or occurred_at.astimezone(UTC) != row.occurred_at.astimezone(UTC)
    ):
        raise InvalidStateError("Поля банковской операции нельзя изменить до сверки")


class SqlAlchemyBankImportRepository(BankImportCommandRepository, BankImportReader):
    """Owner-locked staged imports with bounded reconciliation reads."""

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

    async def _lock_batch(self, owner_id: UUID, batch_id: UUID) -> ImportBatch:
        batch = await self._session.scalar(
            select(ImportBatch)
            .where(ImportBatch.id == batch_id, ImportBatch.user_id == owner_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if batch is None:
            raise EntityNotFoundError("Пакет импорта не найден")
        return batch

    async def _lock_row(
        self,
        owner_id: UUID,
        batch_id: UUID,
        row_id: UUID,
    ) -> ImportRow:
        row = await self._session.scalar(
            select(ImportRow)
            .where(
                ImportRow.id == row_id,
                ImportRow.batch_id == batch_id,
                ImportRow.user_id == owner_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise EntityNotFoundError("Строка импорта не найдена")
        return row

    @staticmethod
    def _require_batch_version(batch: ImportBatch, expected: int) -> None:
        if batch.version != expected:
            raise ObjectVersionConflictError(current_version=batch.version)

    @staticmethod
    def _require_row_version(row: ImportRow, expected: int) -> None:
        if row.version != expected:
            raise ObjectVersionConflictError(current_version=row.version)

    @staticmethod
    def _require_actionable(batch: ImportBatch, row: ImportRow) -> None:
        if batch.status != "open":
            raise InvalidStateError("Пакет импорта уже закрыт")
        if row.status not in _ACTIONABLE_STATES or row.draft_id is not None:
            raise InvalidStateError("Строка импорта уже обрабатывается или завершена")

    async def _counts(self, batch_ids: tuple[UUID, ...]) -> dict[UUID, BankImportCounts]:
        if not batch_ids:
            return {}
        if len(batch_ids) > _MAX_FETCH_LIMIT:
            raise ValueError("Bank import count request is too large")
        rows = await self._session.execute(
            select(
                ImportRow.batch_id,
                func.count(ImportRow.id),
                func.count(ImportRow.id).filter(ImportRow.status == "pending"),
                func.count(ImportRow.id).filter(ImportRow.status == "staged"),
                func.count(ImportRow.id).filter(ImportRow.status == "confirmed"),
                func.count(ImportRow.id).filter(ImportRow.status == "linked"),
                func.count(ImportRow.id).filter(ImportRow.status == "skipped"),
                func.count(ImportRow.id).filter(ImportRow.status == "cancelled"),
            )
            .where(ImportRow.batch_id.in_(batch_ids))
            .group_by(ImportRow.batch_id)
        )
        result = {
            batch_id: BankImportCounts(
                total=int(total),
                pending=int(pending),
                staged=int(staged),
                confirmed=int(confirmed),
                linked=int(linked),
                skipped=int(skipped),
                cancelled=int(cancelled),
            )
            for (
                batch_id,
                total,
                pending,
                staged,
                confirmed,
                linked,
                skipped,
                cancelled,
            ) in rows
        }
        if set(result) != set(batch_ids):
            raise RuntimeError("Bank import rows do not match their batches")
        return result

    @staticmethod
    def _batch_snapshot(batch: ImportBatch, counts: BankImportCounts) -> BankImportBatchSnapshot:
        if counts.total != batch.row_count:
            raise RuntimeError("Bank import batch row count is inconsistent")
        return BankImportBatchSnapshot(
            batch_id=batch.id,
            owner_id=batch.user_id,
            account_id=batch.account_id,
            profile=BankImportProfile(batch.profile),
            encoding=BankImportEncoding(batch.encoding),
            state=BankImportBatchState(batch.status),
            counts=counts,
            version=batch.version,
            created_at=batch.created_at,
            updated_at=batch.updated_at,
            completed_at=batch.completed_at,
            cancelled_at=batch.cancelled_at,
        )

    async def _snapshot_batch(self, batch: ImportBatch) -> BankImportBatchSnapshot:
        counts = await self._counts((batch.id,))
        return self._batch_snapshot(batch, counts[batch.id])

    @staticmethod
    def _duplicate_expression() -> Any:
        other_row = aliased(ImportRow)
        other_batch = aliased(ImportBatch)
        return exists(
            select(other_row.id)
            .join(other_batch, other_batch.id == other_row.batch_id)
            .where(
                other_row.user_id == ImportRow.user_id,
                other_row.id != ImportRow.id,
                other_batch.account_id == ImportBatch.account_id,
                or_(
                    other_row.fingerprint == ImportRow.fingerprint,
                    and_(
                        ImportRow.reference_digest.is_not(None),
                        other_row.reference_digest == ImportRow.reference_digest,
                    ),
                ),
            )
            .limit(1)
        ).correlate(ImportRow, ImportBatch)

    async def _snapshot_row_by_id(
        self,
        owner_id: UUID,
        batch_id: UUID,
        row_id: UUID,
    ) -> BankImportRowSnapshot:
        statement = (
            select(
                ImportRow,
                Transaction.id,
                self._duplicate_expression().label("possible_duplicate"),
            )
            .join(ImportBatch, ImportBatch.id == ImportRow.batch_id)
            .outerjoin(Transaction, Transaction.import_row_id == ImportRow.id)
            .where(
                ImportRow.id == row_id,
                ImportRow.batch_id == batch_id,
                ImportRow.user_id == owner_id,
            )
        )
        result = (await self._session.execute(statement)).one_or_none()
        if result is None:
            raise EntityNotFoundError("Строка импорта не найдена")
        row, transaction_id, possible_duplicate = result._t
        return _row_snapshot(
            row,
            transaction_id=transaction_id,
            possible_duplicate=bool(possible_duplicate),
        )

    async def _mutation_result(
        self,
        batch: ImportBatch,
        row: ImportRow,
    ) -> BankImportMutationResult:
        return BankImportMutationResult(
            batch=await self._snapshot_batch(batch),
            row=await self._snapshot_row_by_id(row.user_id, row.batch_id, row.id),
        )

    async def _advance_batch(self, batch: ImportBatch, now: datetime) -> None:
        unresolved = await self._session.scalar(
            select(func.count(ImportRow.id)).where(
                ImportRow.batch_id == batch.id,
                ImportRow.user_id == batch.user_id,
                ImportRow.status.in_(("pending", "staged")),
            )
        )
        batch.version = _next_version(batch.version, entity="пакета импорта")
        batch.updated_at = now
        if int(unresolved or 0) == 0:
            batch.status = "completed"
            batch.completed_at = now

    async def create_batch(
        self,
        command: StageBankImportBatchCommand,
    ) -> BankImportBatchSnapshot:
        await self._lock_owner(command.owner_id)
        account = await self._session.scalar(
            select(Account)
            .where(Account.id == command.account_id, Account.user_id == command.owner_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if account is None or account.archived_at is not None:
            raise CatalogUnavailableError("Счёт для импорта недоступен")
        if account.version != command.expected_account_version:
            raise ObjectVersionConflictError(current_version=account.version)
        if any(row.currency != account.currency for row in command.rows):
            raise BankImportAccountCurrencyMismatchError(
                "Валюта каждой строки импорта должна совпадать с валютой счёта"
            )
        now = datetime.now(UTC)
        batch = ImportBatch(
            id=uuid7(),
            user_id=command.owner_id,
            account_id=command.account_id,
            profile=command.profile.value,
            encoding=command.encoding.value,
            status="open",
            row_count=len(command.rows),
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._session.add(batch)
        self._session.add_all(
            [
                ImportRow(
                    id=uuid7(),
                    batch_id=batch.id,
                    user_id=command.owner_id,
                    position=row.position,
                    occurred_at=row.occurred_at.astimezone(UTC),
                    type=row.kind.value,
                    amount_minor=row.amount_minor,
                    currency=row.currency,
                    description=row.description,
                    fingerprint=row.fingerprint.value,
                    reference_digest=(
                        row.reference_digest.value if row.reference_digest is not None else None
                    ),
                    status="pending",
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                for row in command.rows
            ]
        )
        await self._session.flush()
        return await self._snapshot_batch(batch)

    async def stage_draft(
        self,
        command: VersionedBankImportRowCommand,
    ) -> BankImportDraftResult:
        owner = await self._lock_owner(command.owner_id)
        batch = await self._lock_batch(command.owner_id, command.batch.batch_id)
        row = await self._lock_row(command.owner_id, batch.id, command.row.row_id)
        self._require_batch_version(batch, command.batch.version)
        self._require_row_version(row, command.row.version)
        self._require_actionable(batch, row)
        active = await self._session.scalar(
            select(Draft)
            .where(Draft.user_id == command.owner_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if active is not None:
            raise ActiveDraftConflictError(current_revision=active.revision)
        account = await self._session.scalar(
            select(Account)
            .where(Account.id == batch.account_id, Account.user_id == command.owner_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if account is None or account.archived_at is not None or account.currency != row.currency:
            raise CatalogUnavailableError("Счёт для импорта недоступен")

        catalogs = SqlAlchemyDraftPreparationRepository(self._session)
        prepared = await PrepareParsedDraft(
            SqlAlchemyQueryRepository(self._session),
            catalogs,
            catalogs,
            SystemDraftPreparationClock(),
        ).prepare_for_owner(
            _owner_snapshot(owner),
            TransactionDraft(
                amount_minor=row.amount_minor,
                type=TransactionType(row.type),
                occurred_at=row.occurred_at,
                account_hint=account.slug,
                description=row.description,
            ),
            flow="bank_import",
        )
        if prepared.state is not DraftPreparationState.REVIEW:
            raise CatalogUnavailableError("Категория для операции импорта недоступна")
        payload = dict(prepared.payload)
        _validate_import_draft_payload(row, batch.account_id, "review", payload)
        now = datetime.now(UTC)
        draft = Draft(
            id=uuid7(),
            user_id=command.owner_id,
            state="review",
            payload=payload,
            schema_version=1,
            revision=1,
            suspended=False,
            updated_at=now,
        )
        self._session.add(draft)
        await self._session.flush()
        row.status = "staged"
        row.draft_id = draft.id
        row.version = _next_version(row.version, entity="строки импорта")
        row.updated_at = now
        batch.version = _next_version(batch.version, entity="пакета импорта")
        batch.updated_at = now
        await self._session.flush()
        result = await self._mutation_result(batch, row)
        return BankImportDraftResult(
            batch=result.batch,
            row=result.row,
            draft=DraftRef(draft.id, draft.revision),
        )

    async def link(self, command: LinkBankImportRowCommand) -> BankImportMutationResult:
        await self._lock_owner(command.owner_id)
        batch = await self._lock_batch(command.owner_id, command.batch.batch_id)
        row = await self._lock_row(command.owner_id, batch.id, command.row.row_id)
        self._require_batch_version(batch, command.batch.version)
        self._require_row_version(row, command.row.version)
        self._require_actionable(batch, row)
        transaction = await self._session.scalar(
            select(Transaction)
            .where(
                Transaction.id == command.transaction_id,
                Transaction.user_id == command.owner_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if transaction is None:
            raise EntityNotFoundError("Операция для сверки не найдена")
        if transaction.version != command.expected_transaction_version:
            raise ObjectVersionConflictError(current_version=transaction.version)
        start, end = reconciliation_bounds(row.occurred_at)
        if (
            transaction.source != "manual"
            or transaction.deleted_at is not None
            or transaction.account_id != batch.account_id
            or transaction.type != row.type
            or transaction.amount_minor != row.amount_minor
            or transaction.currency != row.currency
            or not start <= transaction.occurred_at <= end
        ):
            raise InvalidStateError("Операция больше не подходит для сверки")
        now = datetime.now(UTC)
        transaction.source = "bank_import"
        transaction.import_row_id = row.id
        transaction.version = _next_version(transaction.version, entity="операции")
        transaction.updated_at = now
        row.status = "linked"
        row.resolved_at = now
        row.version = _next_version(row.version, entity="строки импорта")
        row.updated_at = now
        await self._session.flush()
        await self._advance_batch(batch, now)
        await self._session.flush()
        return await self._mutation_result(batch, row)

    async def skip(
        self,
        command: VersionedBankImportRowCommand,
    ) -> BankImportMutationResult:
        await self._lock_owner(command.owner_id)
        batch = await self._lock_batch(command.owner_id, command.batch.batch_id)
        row = await self._lock_row(command.owner_id, batch.id, command.row.row_id)
        self._require_batch_version(batch, command.batch.version)
        self._require_row_version(row, command.row.version)
        self._require_actionable(batch, row)
        now = datetime.now(UTC)
        row.status = "skipped"
        row.resolved_at = now
        row.version = _next_version(row.version, entity="строки импорта")
        row.updated_at = now
        await self._session.flush()
        await self._advance_batch(batch, now)
        await self._session.flush()
        return await self._mutation_result(batch, row)

    async def cancel_batch(
        self,
        command: CancelBankImportBatchCommand,
    ) -> BankImportBatchSnapshot:
        await self._lock_owner(command.owner_id)
        batch = await self._lock_batch(command.owner_id, command.expected.batch_id)
        self._require_batch_version(batch, command.expected.version)
        if batch.status != "open":
            raise InvalidStateError("Пакет импорта уже закрыт")
        rows = tuple(
            await self._session.scalars(
                select(ImportRow)
                .where(
                    ImportRow.batch_id == batch.id,
                    ImportRow.user_id == command.owner_id,
                )
                .order_by(ImportRow.position, ImportRow.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if len(rows) != batch.row_count:
            raise RuntimeError("Bank import batch row count is inconsistent")
        if any(row.draft_id is not None for row in rows):
            raise InvalidStateError("Сначала закройте черновик операции импорта")
        actionable = tuple(row for row in rows if row.status in _ACTIONABLE_STATES)
        if not actionable:
            raise InvalidStateError("В пакете импорта нет строк для отмены")
        now = datetime.now(UTC)
        for row in actionable:
            row.status = "cancelled"
            row.resolved_at = now
            row.version = _next_version(row.version, entity="строки импорта")
            row.updated_at = now
        batch.status = "cancelled"
        batch.cancelled_at = now
        batch.version = _next_version(batch.version, entity="пакета импорта")
        batch.updated_at = now
        await self._session.flush()
        return await self._snapshot_batch(batch)

    async def get_batch(
        self,
        owner_id: UUID,
        batch_id: UUID,
    ) -> BankImportBatchSnapshot | None:
        batch = await self._session.scalar(
            select(ImportBatch).where(
                ImportBatch.id == batch_id,
                ImportBatch.user_id == owner_id,
            )
        )
        return await self._snapshot_batch(batch) if batch is not None else None

    async def list_batches_after(
        self,
        owner_id: UUID,
        *,
        state: BankImportBatchState | None,
        cursor: BankImportBatchCursor | None,
        limit: int,
    ) -> tuple[BankImportBatchCursorItem, ...]:
        if type(limit) is not int or not 1 <= limit <= _MAX_FETCH_LIMIT:
            raise ValueError("bank import fetch limit is invalid")
        statement = select(ImportBatch).where(ImportBatch.user_id == owner_id)
        if state is not None:
            statement = statement.where(ImportBatch.status == state.value)
        if cursor is not None:
            statement = statement.where(
                or_(
                    ImportBatch.created_at < cursor.created_at,
                    and_(
                        ImportBatch.created_at == cursor.created_at,
                        ImportBatch.id < cursor.batch_id,
                    ),
                )
            )
        batches = tuple(
            await self._session.scalars(
                statement.order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc()).limit(
                    limit
                )
            )
        )
        counts = await self._counts(tuple(batch.id for batch in batches))
        return tuple(
            BankImportBatchCursorItem(
                batch=self._batch_snapshot(batch, counts[batch.id]),
                cursor=BankImportBatchCursor(batch.created_at, batch.id),
            )
            for batch in batches
        )

    async def get_row(
        self,
        owner_id: UUID,
        batch_id: UUID,
        row_id: UUID,
    ) -> BankImportRowSnapshot | None:
        try:
            return await self._snapshot_row_by_id(owner_id, batch_id, row_id)
        except EntityNotFoundError:
            return None

    async def list_rows_after(
        self,
        owner_id: UUID,
        batch_id: UUID,
        *,
        state: BankImportRowState | None,
        cursor: BankImportRowCursor | None,
        limit: int,
    ) -> tuple[BankImportRowCursorItem, ...]:
        if type(limit) is not int or not 1 <= limit <= _MAX_FETCH_LIMIT:
            raise ValueError("bank import row fetch limit is invalid")
        statement = (
            select(
                ImportRow,
                Transaction.id,
                self._duplicate_expression().label("possible_duplicate"),
            )
            .join(ImportBatch, ImportBatch.id == ImportRow.batch_id)
            .outerjoin(Transaction, Transaction.import_row_id == ImportRow.id)
            .where(
                ImportRow.user_id == owner_id,
                ImportRow.batch_id == batch_id,
            )
        )
        if state is not None:
            statement = statement.where(ImportRow.status == state.value)
        if cursor is not None:
            statement = statement.where(
                or_(
                    ImportRow.position > cursor.position,
                    and_(
                        ImportRow.position == cursor.position,
                        ImportRow.id > cursor.row_id,
                    ),
                )
            )
        rows = await self._session.execute(
            statement.order_by(ImportRow.position, ImportRow.id).limit(limit)
        )
        return tuple(
            BankImportRowCursorItem(
                row=(
                    snapshot := _row_snapshot(
                        import_row,
                        transaction_id=transaction_id,
                        possible_duplicate=bool(possible_duplicate),
                    )
                ),
                cursor=BankImportRowCursor(snapshot.position, snapshot.row_id),
            )
            for import_row, transaction_id, possible_duplicate in rows
        )

    async def list_candidate_transactions(
        self,
        owner_id: UUID,
        row_id: UUID,
        *,
        limit: int,
    ) -> tuple[TransactionSnapshot, ...]:
        if type(limit) is not int or not 1 <= limit <= _MAX_CANDIDATE_FETCH:
            raise ValueError("bank import candidate limit is invalid")
        source = (
            await self._session.execute(
                select(ImportRow, ImportBatch.account_id)
                .join(ImportBatch, ImportBatch.id == ImportRow.batch_id)
                .where(ImportRow.id == row_id, ImportRow.user_id == owner_id)
            )
        ).one_or_none()
        if source is None:
            return ()
        row, account_id = source._t
        start, end = reconciliation_bounds(row.occurred_at)
        distance = func.abs(func.extract("epoch", Transaction.occurred_at - row.occurred_at))
        candidates = await self._session.execute(
            select(Transaction, Account.name, Category.name, Category.emoji)
            .join(Account, Account.id == Transaction.account_id)
            .join(Category, Category.id == Transaction.category_id)
            .where(
                Transaction.user_id == owner_id,
                Transaction.source == "manual",
                Transaction.deleted_at.is_(None),
                Transaction.account_id == account_id,
                Transaction.type == row.type,
                Transaction.amount_minor == row.amount_minor,
                Transaction.currency == row.currency,
                Transaction.occurred_at >= start,
                Transaction.occurred_at <= end,
            )
            .order_by(distance, Transaction.occurred_at, Transaction.id)
            .limit(limit)
        )
        return tuple(
            _transaction_snapshot(transaction, account_name, category_name, category_emoji)
            for transaction, account_name, category_name, category_emoji in candidates
        )


__all__ = ["SqlAlchemyBankImportRepository"]
