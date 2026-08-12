from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import (
    Account,
    AuditEvent,
    Category,
    Draft,
    Transaction,
)
from finbot.application.services.catalogs import catalog_slug
from finbot.domain.categories import CATEGORY_ALIASES, EXPENSE_CATEGORIES, INCOME_CATEGORIES
from finbot.domain.errors import (
    ObjectNotFoundError,
    StaleObjectError,
    UnknownAccountError,
    UnknownCategoryError,
)
from finbot.domain.transactions import TransactionDraft


def _snapshot(transaction: Transaction) -> dict[str, object]:
    return {
        "amount_minor": transaction.amount_minor,
        "category_id": str(transaction.category_id),
        "account_id": str(transaction.account_id),
        "currency": transaction.currency,
        "occurred_at": transaction.occurred_at.isoformat(),
        "description": transaction.description,
        "deleted_at": transaction.deleted_at.isoformat() if transaction.deleted_at else None,
    }


async def _audit(
    session: AsyncSession,
    user_id: UUID,
    transaction_id: UUID,
    action: str,
    data: dict[str, object] | None = None,
) -> None:
    session.add(
        AuditEvent(
            user_id=user_id,
            transaction_id=transaction_id,
            action=action,
            data=data or {},
        )
    )


async def resolve_category(
    session: AsyncSession,
    user_id: UUID,
    kind: str,
    hint: str | None,
) -> Category:
    names = INCOME_CATEGORIES if kind == "income" else EXPENSE_CATEGORIES
    requested = hint or (names[0] if kind == "income" else names[-1])
    requested = CATEGORY_ALIASES.get(requested.casefold(), requested)
    slug = catalog_slug(requested)
    category = await session.scalar(
        select(Category).where(
            Category.user_id == user_id,
            Category.kind == kind,
            Category.slug == slug,
            Category.archived_at.is_(None),
        )
    )
    if category is None:
        category = await session.scalar(
            select(Category).where(
                Category.user_id == user_id,
                Category.kind == kind,
                func.lower(Category.name) == requested.casefold(),
                Category.archived_at.is_(None),
            )
        )
    if category is None:
        raise UnknownCategoryError(requested)
    return category


async def resolve_account(
    session: AsyncSession,
    user_id: UUID,
    hint: str | None,
    default_account_id: UUID | None = None,
) -> Account:
    if hint:
        normalized = hint.casefold().strip()
        account = await session.scalar(
            select(Account).where(
                Account.user_id == user_id,
                or_(
                    func.lower(Account.slug) == normalized,
                    Account.slug == catalog_slug(hint),
                    func.lower(Account.name) == normalized,
                ),
                Account.archived_at.is_(None),
            )
        )
        if account is None:
            raise UnknownAccountError(hint)
        return account
    if default_account_id is not None:
        account = cast(
            Account | None,
            await session.scalar(
                select(Account).where(
                    Account.id == default_account_id,
                    Account.user_id == user_id,
                    Account.archived_at.is_(None),
                )
            ),
        )
        if account is not None:
            return account
    account = cast(
        Account | None,
        await session.scalar(
            select(Account)
            .where(Account.user_id == user_id, Account.archived_at.is_(None))
            .order_by(Account.name)
            .limit(1)
        ),
    )
    if account is None:
        raise UnknownAccountError("основной")
    return account


async def save_transaction(
    session: AsyncSession,
    user_id: UUID,
    draft: TransactionDraft,
    currency: str = "RUB",
    update_id: int | None = None,
    default_account_id: UUID | None = None,
) -> Transaction:
    category = await resolve_category(session, user_id, draft.type.value, draft.category_hint)
    account = await resolve_account(session, user_id, draft.account_hint, default_account_id)
    item = Transaction(
        user_id=user_id,
        type=draft.type.value,
        amount_minor=draft.amount_minor,
        currency=account.currency or currency,
        account_id=account.id,
        category_id=category.id,
        occurred_at=(draft.occurred_at or datetime.now(UTC)).astimezone(UTC),
        description=draft.description[:500],
        source="manual",
        telegram_update_id=update_id,
    )
    session.add(item)
    await session.flush()
    await _audit(session, user_id, item.id, "create")
    await session.flush()
    return item


async def _locked_transaction(
    session: AsyncSession,
    user_id: UUID,
    transaction_id: UUID,
) -> Transaction:
    transaction = await session.scalar(
        select(Transaction)
        .where(Transaction.id == transaction_id, Transaction.user_id == user_id)
        .with_for_update()
    )
    if transaction is None:
        raise ObjectNotFoundError("Операция не найдена")
    return transaction


async def soft_delete_transaction(
    session: AsyncSession,
    user_id: UUID,
    transaction_id: UUID,
    version: int,
) -> Transaction:
    transaction = await _locked_transaction(session, user_id, transaction_id)
    if transaction.version != version:
        raise StaleObjectError("Операция уже была изменена. Обновите историю")
    if transaction.deleted_at is not None:
        raise StaleObjectError("Операция уже удалена")
    transaction.deleted_at = datetime.now(UTC)
    transaction.version += 1
    await _audit(session, user_id, transaction.id, "delete")
    await session.flush()
    return transaction


async def restore_transaction(
    session: AsyncSession,
    user_id: UUID,
    transaction_id: UUID,
    version: int,
) -> Transaction:
    transaction = await _locked_transaction(session, user_id, transaction_id)
    if transaction.version != version:
        raise StaleObjectError("Операция уже была изменена. Обновите историю")
    if transaction.deleted_at is None:
        raise StaleObjectError("Операция уже восстановлена")
    transaction.deleted_at = None
    transaction.version += 1
    await _audit(session, user_id, transaction.id, "restore")
    await session.flush()
    return transaction


async def edit_transaction(
    session: AsyncSession,
    user_id: UUID,
    transaction_id: UUID,
    version: int,
    *,
    amount_minor: int | None = None,
    category_id: UUID | None = None,
    account_id: UUID | None = None,
    occurred_at: datetime | None = None,
    description: str | None = None,
) -> Transaction:
    transaction = await _locked_transaction(session, user_id, transaction_id)
    if transaction.deleted_at is not None:
        raise StaleObjectError("Удалённую операцию сначала нужно восстановить")
    if transaction.version != version:
        raise StaleObjectError("Операция уже была изменена. Откройте её заново")
    old = _snapshot(transaction)
    if amount_minor is not None:
        if amount_minor <= 0:
            raise ValueError("Сумма должна быть больше нуля")
        transaction.amount_minor = amount_minor
    if category_id is not None:
        category = await session.scalar(
            select(Category).where(
                Category.id == category_id,
                Category.user_id == user_id,
                Category.kind == transaction.type,
                Category.archived_at.is_(None),
            )
        )
        if category is None:
            raise UnknownCategoryError("выбранная")
        transaction.category_id = category.id
    if account_id is not None:
        account = await session.scalar(
            select(Account).where(
                Account.id == account_id,
                Account.user_id == user_id,
                Account.archived_at.is_(None),
            )
        )
        if account is None:
            raise UnknownAccountError("выбранный")
        transaction.account_id = account.id
        transaction.currency = account.currency
    if occurred_at is not None:
        transaction.occurred_at = occurred_at.astimezone(UTC)
    if description is not None:
        transaction.description = description.strip()[:500]
    transaction.version += 1
    await _audit(session, user_id, transaction.id, "update", {"old": old})
    await session.flush()
    return transaction


@dataclass(frozen=True, slots=True)
class UndoResult:
    action: str
    transaction: Transaction | None


async def undo_last_action(session: AsyncSession, user_id: UUID) -> UndoResult | None:
    event = await session.scalar(
        select(AuditEvent)
        .where(AuditEvent.user_id == user_id, AuditEvent.undone_at.is_(None))
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(1)
        .with_for_update()
    )
    if event is None:
        # Never guess which legacy operation should be undone. Only an explicit,
        # reversible audit event authorizes a mutation.
        return None
    transaction = None
    if event.transaction_id is not None:
        transaction = await _locked_transaction(session, user_id, event.transaction_id)
    if transaction is not None:
        if event.action == "create":
            transaction.deleted_at = datetime.now(UTC)
        elif event.action == "delete":
            transaction.deleted_at = None
        elif event.action == "restore":
            transaction.deleted_at = datetime.now(UTC)
        elif event.action == "update":
            old_raw = event.data.get("old")
            if isinstance(old_raw, dict):
                transaction.amount_minor = int(str(old_raw["amount_minor"]))
                transaction.category_id = UUID(str(old_raw["category_id"]))
                transaction.account_id = UUID(str(old_raw["account_id"]))
                transaction.currency = str(old_raw["currency"])
                transaction.occurred_at = datetime.fromisoformat(str(old_raw["occurred_at"]))
                transaction.description = str(old_raw["description"])
                deleted_at = old_raw.get("deleted_at")
                transaction.deleted_at = (
                    datetime.fromisoformat(str(deleted_at)) if deleted_at else None
                )
        transaction.version += 1
    event.undone_at = datetime.now(UTC)
    await session.flush()
    return UndoResult(event.action, transaction)


async def undo_last(session: AsyncSession, user_id: UUID) -> bool:
    """Compatibility wrapper for older callers."""
    return await undo_last_action(session, user_id) is not None


async def get_draft(
    session: AsyncSession,
    user_id: UUID,
    *,
    for_update: bool = False,
) -> Draft | None:
    query = select(Draft).where(Draft.user_id == user_id)
    if for_update:
        query = query.with_for_update()
    return await session.scalar(query)  # type: ignore[no-any-return]


async def put_draft(
    session: AsyncSession,
    user_id: UUID,
    state: str,
    payload: dict[str, object],
    *,
    expected_revision: int | None = None,
    expected_draft_id: UUID | None = None,
    new_flow: bool = False,
) -> Draft:
    """Create or atomically update the owner's single active draft.

    Existing rows are locked before mutation. ``expected_revision`` rejects a
    stale callback, while ``new_flow`` replaces the row so a previous flow's
    callbacks cannot accidentally target the new draft id.
    """
    draft = await get_draft(session, user_id, for_update=True)
    if expected_revision is not None or expected_draft_id is not None:
        if (
            draft is None
            or (expected_revision is not None and draft.revision != expected_revision)
            or (expected_draft_id is not None and draft.id != expected_draft_id)
        ):
            raise StaleObjectError("Черновик уже был изменён. Откройте актуальную форму")
    if draft is not None and new_flow:
        await session.delete(draft)
        # Flush the deletion before inserting the replacement because user_id
        # has a unique constraint. A new ORM instance also avoids mutating a
        # persistent object's identity key in SQLAlchemy's identity map.
        await session.flush()
        draft = None
    if draft is None:
        draft = Draft(user_id=user_id, state=state, payload=dict(payload))
        session.add(draft)
    else:
        draft.state = state
        # JSONB values are not mutable-tracked.  Keep the ORM value detached from
        # the caller so a later in-place change cannot be silently lost after flush.
        draft.payload = dict(payload)
        draft.revision += 1
    await session.flush()
    return draft


async def start_draft(
    session: AsyncSession,
    user_id: UUID,
    state: str,
    payload: dict[str, object],
) -> Draft:
    """Start a distinct flow and invalidate identifiers from any older flow."""
    return await put_draft(session, user_id, state, payload, new_flow=True)


async def set_draft_suspended(
    session: AsyncSession,
    user_id: UUID,
    *,
    suspended: bool,
) -> Draft | None:
    """Suspend or resume an active flow and invalidate its old controls."""
    draft = await get_draft(session, user_id, for_update=True)
    if draft is not None and draft.suspended != suspended:
        draft.suspended = suspended
        draft.revision += 1
        await session.flush()
    return draft


async def clear_draft(
    session: AsyncSession,
    user_id: UUID,
    *,
    expected_revision: int | None = None,
    expected_draft_id: UUID | None = None,
) -> None:
    draft = await get_draft(session, user_id, for_update=True)
    if (expected_revision is not None or expected_draft_id is not None) and (
        draft is None
        or (expected_revision is not None and draft.revision != expected_revision)
        or (expected_draft_id is not None and draft.id != expected_draft_id)
    ):
        raise StaleObjectError("Черновик уже был изменён. Откройте актуальную форму")
    if draft is not None:
        await session.delete(draft)
        await session.flush()


async def create_or_get_category(
    session: AsyncSession, user_id: UUID, name: str, kind: str
) -> Category:
    clean = name.strip()
    if not clean:
        raise ValueError("Название категории не может быть пустым")
    slug = catalog_slug(clean)
    category = await session.scalar(
        select(Category).where(
            Category.user_id == user_id,
            Category.kind == kind,
            Category.slug == slug,
        )
    )
    if category is not None and category.archived_at is not None:
        raise ValueError("Категория с таким названием находится в архиве")
    if category is None:
        category = Category(user_id=user_id, kind=kind, name=clean.title(), slug=slug)
        session.add(category)
        await session.flush()
    return category


async def create_or_get_account(
    session: AsyncSession,
    user_id: UUID,
    name: str,
    currency: str = "RUB",
) -> Account:
    clean = name.strip()
    if not clean:
        raise ValueError("Название счёта не может быть пустым")
    slug = catalog_slug(clean)
    account = await session.scalar(
        select(Account).where(
            Account.user_id == user_id,
            Account.slug == slug,
        )
    )
    if account is not None and account.archived_at is not None:
        raise ValueError("Счёт с таким названием находится в архиве")
    if account is None:
        account = Account(
            user_id=user_id,
            name=clean.title(),
            slug=slug,
            type="other",
            currency=currency,
        )
        session.add(account)
        await session.flush()
    return account


async def edit_amount(
    session: AsyncSession,
    user_id: UUID,
    transaction_id: UUID,
    amount_minor: int,
    version: int,
) -> bool:
    try:
        await edit_transaction(
            session,
            user_id,
            transaction_id,
            version,
            amount_minor=amount_minor,
        )
    except ObjectNotFoundError, StaleObjectError:
        return False
    return True
