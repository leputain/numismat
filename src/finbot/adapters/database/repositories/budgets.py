from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid7

from sqlalchemy import and_, func, literal, or_, select, union_all
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import Budget, Category, Transaction, User
from finbot.application.budgets import (
    MAX_BUDGET_PAGE_SIZE,
    BudgetCommandRepository,
    BudgetCursor,
    BudgetCursorItem,
    BudgetSnapshot,
    BudgetSpendWindow,
    CreateBudgetCommand,
    ReplaceBudgetCommand,
    VersionedBudgetCommand,
)
from finbot.application.dto import OwnerSnapshot
from finbot.application.errors import (
    ApplicationValidationError,
    BudgetOverlapError,
    CatalogUnavailableError,
    EntityNotFoundError,
    InvalidStateError,
    ObjectVersionConflictError,
)
from finbot.domain.budgets import BudgetDefinition

_MAX_FETCH_LIMIT = MAX_BUDGET_PAGE_SIZE + 1
_MAX_VERSION = 2**31 - 1


def _snapshot(budget: Budget) -> BudgetSnapshot:
    return BudgetSnapshot(
        budget_id=budget.id,
        owner_id=budget.user_id,
        definition=BudgetDefinition(
            name=budget.name,
            limit_minor=budget.limit_minor,
            currency=budget.currency,
            category_id=budget.category_id,
            starts_on=budget.starts_on,
            ends_on=budget.ends_on,
            timezone=budget.timezone,
        ),
        version=budget.version,
        created_at=budget.created_at,
        updated_at=budget.updated_at,
        deleted_at=budget.deleted_at,
    )


def _next_version(current: int) -> int:
    if current >= _MAX_VERSION:
        raise InvalidStateError("Достигнут предел версий бюджета")
    return current + 1


class SqlAlchemyBudgetRepository(BudgetCommandRepository):
    """Owner-locked budget writes and bounded owner-scoped progress reads."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _lock_owner(self, owner_id: UUID) -> User:
        owner = await self._session.scalar(
            select(User).where(User.id == owner_id).with_for_update()
        )
        if owner is None:
            raise EntityNotFoundError("Владелец не найден")
        return owner

    async def _lock_budget(self, owner_id: UUID, budget_id: UUID) -> Budget:
        budget = await self._session.scalar(
            select(Budget)
            .where(Budget.id == budget_id, Budget.user_id == owner_id)
            .with_for_update()
        )
        if budget is None:
            raise EntityNotFoundError("Бюджет не найден")
        return budget

    async def _require_active_expense_category(
        self,
        owner_id: UUID,
        category_id: UUID | None,
    ) -> None:
        if category_id is None:
            return
        category = await self._session.scalar(
            select(Category)
            .where(
                Category.id == category_id,
                Category.user_id == owner_id,
                Category.kind == "expense",
            )
            .with_for_update(read=True)
        )
        if category is None or category.archived_at is not None:
            raise CatalogUnavailableError("Категория расходов недоступна")

    async def _ensure_no_overlap(
        self,
        owner_id: UUID,
        definition: BudgetDefinition,
        *,
        exclude_id: UUID | None = None,
    ) -> None:
        category_filter = (
            Budget.category_id.is_(None)
            if definition.category_id is None
            else Budget.category_id == definition.category_id
        )
        statement = select(Budget.id).where(
            Budget.user_id == owner_id,
            Budget.deleted_at.is_(None),
            Budget.currency == definition.currency,
            category_filter,
            Budget.starts_on <= definition.ends_on,
            Budget.ends_on >= definition.starts_on,
        )
        if exclude_id is not None:
            statement = statement.where(Budget.id != exclude_id)
        if await self._session.scalar(statement.limit(1)) is not None:
            raise BudgetOverlapError("Период бюджета пересекается с существующим")

    async def create(self, command: CreateBudgetCommand) -> BudgetSnapshot:
        owner = await self._lock_owner(command.owner_id)
        if command.definition.timezone != owner.timezone:
            raise ApplicationValidationError("Часовой пояс бюджета должен совпадать с владельцем")
        await self._require_active_expense_category(
            command.owner_id,
            command.definition.category_id,
        )
        await self._ensure_no_overlap(command.owner_id, command.definition)
        now = datetime.now(UTC)
        budget = Budget(
            id=uuid7(),
            user_id=command.owner_id,
            name=command.definition.name,
            limit_minor=command.definition.limit_minor,
            currency=command.definition.currency,
            category_id=command.definition.category_id,
            category_kind="expense",
            starts_on=command.definition.starts_on,
            ends_on=command.definition.ends_on,
            timezone=command.definition.timezone,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._session.add(budget)
        await self._session.flush()
        return _snapshot(budget)

    async def replace(self, command: ReplaceBudgetCommand) -> BudgetSnapshot:
        await self._lock_owner(command.owner_id)
        budget = await self._lock_budget(command.owner_id, command.expected.budget_id)
        if budget.version != command.expected.version:
            raise ObjectVersionConflictError(current_version=budget.version)
        if budget.deleted_at is not None:
            raise InvalidStateError("Удалённый бюджет нельзя изменить")
        if command.definition.timezone != budget.timezone:
            raise ApplicationValidationError("Часовой пояс существующего бюджета неизменяем")
        await self._require_active_expense_category(
            command.owner_id,
            command.definition.category_id,
        )
        await self._ensure_no_overlap(
            command.owner_id,
            command.definition,
            exclude_id=budget.id,
        )
        budget.name = command.definition.name
        budget.limit_minor = command.definition.limit_minor
        budget.currency = command.definition.currency
        budget.category_id = command.definition.category_id
        budget.starts_on = command.definition.starts_on
        budget.ends_on = command.definition.ends_on
        budget.version = _next_version(budget.version)
        budget.updated_at = datetime.now(UTC)
        await self._session.flush()
        return _snapshot(budget)

    async def delete(self, command: VersionedBudgetCommand) -> BudgetSnapshot:
        await self._lock_owner(command.owner_id)
        budget = await self._lock_budget(command.owner_id, command.expected.budget_id)
        if budget.version != command.expected.version:
            raise ObjectVersionConflictError(current_version=budget.version)
        if budget.deleted_at is not None:
            raise InvalidStateError("Бюджет уже удалён")
        now = datetime.now(UTC)
        budget.deleted_at = now
        budget.updated_at = now
        budget.version = _next_version(budget.version)
        await self._session.flush()
        return _snapshot(budget)

    async def restore(self, command: VersionedBudgetCommand) -> BudgetSnapshot:
        await self._lock_owner(command.owner_id)
        budget = await self._lock_budget(command.owner_id, command.expected.budget_id)
        if budget.version != command.expected.version:
            raise ObjectVersionConflictError(current_version=budget.version)
        if budget.deleted_at is None:
            raise InvalidStateError("Бюджет не удалён")
        definition = _snapshot(budget).definition
        await self._require_active_expense_category(command.owner_id, definition.category_id)
        await self._ensure_no_overlap(command.owner_id, definition, exclude_id=budget.id)
        budget.deleted_at = None
        budget.updated_at = datetime.now(UTC)
        budget.version = _next_version(budget.version)
        await self._session.flush()
        return _snapshot(budget)

    async def get(self, owner_id: UUID, budget_id: UUID) -> BudgetSnapshot | None:
        budget = await self._session.scalar(
            select(Budget).where(Budget.id == budget_id, Budget.user_id == owner_id)
        )
        return _snapshot(budget) if budget is not None else None

    async def get_owner(self, owner_id: UUID) -> OwnerSnapshot | None:
        row = (
            await self._session.execute(
                select(
                    User.id,
                    User.locale,
                    User.timezone,
                    User.base_currency,
                    User.default_account_id,
                    User.fast_mode,
                ).where(User.id == owner_id)
            )
        ).one_or_none()
        if row is None:
            return None
        user_id, locale, timezone, base_currency, default_account_id, fast_mode = row._t
        return OwnerSnapshot(
            owner_id=user_id,
            locale=locale,
            timezone=timezone,
            base_currency=base_currency,
            default_account_id=default_account_id,
            fast_mode=fast_mode,
        )

    async def list_after(
        self,
        owner_id: UUID,
        *,
        window_start: date,
        window_end: date,
        deleted: bool,
        cursor: BudgetCursor | None,
        limit: int,
    ) -> tuple[BudgetCursorItem, ...]:
        if type(limit) is not int or not 1 <= limit <= _MAX_FETCH_LIMIT:
            raise ValueError("budget fetch limit is invalid")
        if type(deleted) is not bool:
            raise ValueError("budget deletion selector is invalid")
        deleted_filter = Budget.deleted_at.is_not(None) if deleted else Budget.deleted_at.is_(None)
        statement = select(Budget).where(
            Budget.user_id == owner_id,
            deleted_filter,
            Budget.starts_on <= window_end,
            Budget.ends_on >= window_start,
        )
        if cursor is not None:
            statement = statement.where(
                or_(
                    Budget.starts_on < cursor.starts_on,
                    and_(
                        Budget.starts_on == cursor.starts_on,
                        Budget.id < cursor.budget_id,
                    ),
                )
            )
        rows = await self._session.scalars(
            statement.order_by(Budget.starts_on.desc(), Budget.id.desc()).limit(limit)
        )
        return tuple(
            BudgetCursorItem(
                budget=_snapshot(budget),
                cursor=BudgetCursor(starts_on=budget.starts_on, budget_id=budget.id),
            )
            for budget in rows
        )

    async def spent_minor_for_budgets(
        self,
        owner_id: UUID,
        windows: tuple[BudgetSpendWindow, ...],
    ) -> dict[UUID, int]:
        if len(windows) > MAX_BUDGET_PAGE_SIZE:
            raise ValueError("budget aggregate request is too large")
        if len({window.budget_id for window in windows}) != len(windows):
            raise ValueError("budget aggregate request contains duplicate ids")
        if not windows:
            return {}

        statements = []
        for window in windows:
            filters = [
                Transaction.user_id == owner_id,
                Transaction.type == "expense",
                Transaction.deleted_at.is_(None),
                Transaction.currency == window.currency,
                Transaction.occurred_at >= window.start,
                Transaction.occurred_at < window.end,
            ]
            if window.category_id is not None:
                filters.append(Transaction.category_id == window.category_id)
            statements.append(
                select(
                    literal(window.budget_id, type_=PGUUID(as_uuid=True)).label("budget_id"),
                    func.coalesce(func.sum(Transaction.amount_minor), 0).label("spent_minor"),
                ).where(*filters)
            )

        aggregate = statements[0] if len(statements) == 1 else union_all(*statements)
        rows = await self._session.execute(aggregate)
        result = {budget_id: int(spent_minor) for budget_id, spent_minor in rows}
        if set(result) != {window.budget_id for window in windows} or any(
            amount < 0 for amount in result.values()
        ):
            raise RuntimeError("budget aggregate query returned invalid rows")
        return result


__all__ = ["SqlAlchemyBudgetRepository"]
