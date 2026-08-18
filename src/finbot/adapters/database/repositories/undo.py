from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import Account, AuditEvent, Category, Transaction, User
from finbot.adapters.database.queries.transactions import get_transaction_details
from finbot.application.dto import TransactionSnapshot
from finbot.application.errors import EntityNotFoundError, InvalidStateError
from finbot.application.undo import UndoAction, UndoActionResult
from finbot.domain.transactions import TransactionType


@dataclass(frozen=True, slots=True)
class _UpdateSnapshot:
    transaction_type: str | None
    amount_minor: int
    category_id: UUID
    account_id: UUID
    currency: str
    occurred_at: datetime
    description: str
    deleted_at: datetime | None


def _aware_datetime(value: object, *, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        raise InvalidStateError(f"Audit {field_name} is invalid") from None
    if parsed.utcoffset() is None:
        raise InvalidStateError(f"Audit {field_name} must contain a timezone")
    return parsed


def _update_snapshot(data: Mapping[str, object]) -> _UpdateSnapshot:
    old = data.get("old")
    if not isinstance(old, Mapping):
        raise InvalidStateError("Audit update snapshot is missing")
    try:
        amount_minor = int(str(old["amount_minor"]))
        category_id = UUID(str(old["category_id"]))
        account_id = UUID(str(old["account_id"]))
        currency = old["currency"]
        description = old["description"]
        occurred_raw = old["occurred_at"]
    except KeyError, TypeError, ValueError:
        raise InvalidStateError("Audit update snapshot is invalid") from None
    transaction_type = old.get("type")
    if transaction_type is not None and transaction_type not in {"expense", "income"}:
        raise InvalidStateError("Audit transaction type is invalid")
    if amount_minor <= 0:
        raise InvalidStateError("Audit amount must be positive")
    if (
        type(currency) is not str
        or len(currency) != 3
        or not currency.isascii()
        or not currency.isalpha()
        or not currency.isupper()
    ):
        raise InvalidStateError("Audit currency is invalid")
    if type(description) is not str or len(description) > 500:
        raise InvalidStateError("Audit description is invalid")
    occurred_at = _aware_datetime(occurred_raw, field_name="date")
    deleted_raw = old.get("deleted_at")
    deleted_at = (
        _aware_datetime(deleted_raw, field_name="deleted date") if deleted_raw is not None else None
    )
    return _UpdateSnapshot(
        transaction_type=transaction_type,
        amount_minor=amount_minor,
        category_id=category_id,
        account_id=account_id,
        currency=currency,
        occurred_at=occurred_at,
        description=description,
        deleted_at=deleted_at,
    )


class SqlAlchemyUndoRepository:
    """Owner-serialized reversal of the latest explicit transaction audit event."""

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

    async def _latest_event(self, owner_id: UUID) -> AuditEvent | None:
        return cast(
            AuditEvent | None,
            await self._session.scalar(
                select(AuditEvent)
                .where(AuditEvent.user_id == owner_id, AuditEvent.undone_at.is_(None))
                .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
                .limit(1)
                .with_for_update()
                .execution_options(populate_existing=True)
            ),
        )

    async def _locked_transaction(
        self,
        owner_id: UUID,
        transaction_id: UUID,
    ) -> Transaction:
        transaction = await self._session.scalar(
            select(Transaction)
            .where(Transaction.id == transaction_id, Transaction.user_id == owner_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if transaction is None:
            raise EntityNotFoundError("Операция не найдена")
        return transaction

    async def _validate_update_catalogs(
        self,
        owner_id: UUID,
        transaction_type: str,
        snapshot: _UpdateSnapshot,
    ) -> None:
        account_id = await self._session.scalar(
            select(Account.id).where(
                Account.id == snapshot.account_id,
                Account.user_id == owner_id,
            )
        )
        category_id = await self._session.scalar(
            select(Category.id).where(
                Category.id == snapshot.category_id,
                Category.user_id == owner_id,
                Category.kind == (snapshot.transaction_type or transaction_type),
            )
        )
        if account_id is None or category_id is None:
            raise InvalidStateError("Audit catalog snapshot is unavailable")

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

    async def undo_last(self, owner_id: UUID) -> UndoActionResult | None:
        await self._lock_owner(owner_id)
        event = await self._latest_event(owner_id)
        if event is None:
            # A legacy transaction row is not evidence of reversible user intent.
            return None
        try:
            action = UndoAction(event.action)
        except ValueError:
            raise InvalidStateError("Последнее действие нельзя отменить") from None
        if event.transaction_id is None:
            raise InvalidStateError("Audit event has no transaction target")

        transaction = await self._locked_transaction(owner_id, event.transaction_id)
        now = datetime.now(UTC)
        if action is UndoAction.CREATE:
            transaction.deleted_at = now
        elif action is UndoAction.DELETE:
            transaction.deleted_at = None
        elif action is UndoAction.RESTORE:
            transaction.deleted_at = now
        else:
            previous = _update_snapshot(event.data)
            await self._validate_update_catalogs(owner_id, transaction.type, previous)
            if previous.transaction_type is not None:
                transaction.type = previous.transaction_type
            transaction.amount_minor = previous.amount_minor
            transaction.category_id = previous.category_id
            transaction.account_id = previous.account_id
            transaction.currency = previous.currency
            transaction.occurred_at = previous.occurred_at
            transaction.description = previous.description
            transaction.deleted_at = previous.deleted_at

        transaction.version += 1
        event.undone_at = now
        await self._session.flush()
        return UndoActionResult(
            action=action,
            transaction=await self._snapshot(owner_id, transaction.id),
        )
