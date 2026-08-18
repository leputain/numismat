from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from finbot.adapters.database.models import Category, CategoryRule, User
from finbot.application.errors import CatalogUnavailableError
from finbot.application.rules import (
    CATEGORY_RULE_FETCH_LIMIT,
    MAX_CATEGORY_RULES_PER_OWNER_KIND,
)
from finbot.domain.category_rules import CategoryRuleSpec, normalize_rule_pattern
from finbot.domain.errors import ObjectNotFoundError
from finbot.domain.transactions import TransactionType


def _spec(rule: CategoryRule) -> CategoryRuleSpec:
    return CategoryRuleSpec(
        id=rule.id,
        category_id=rule.category_id,
        kind=TransactionType(rule.kind),
        normalized_pattern=rule.normalized_pattern,
        account_id=rule.account_id,
        version=rule.version,
        updated_at=rule.updated_at,
    )


class SqlAlchemyCategoryRuleRepository:
    """PostgreSQL implementation of the category-rule application ports."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_applicable(
        self,
        user_id: UUID,
        kind: TransactionType,
        account_id: UUID | None,
    ) -> tuple[CategoryRuleSpec, ...]:
        scopes: list[ColumnElement[bool]] = [CategoryRule.account_id.is_(None)]
        if account_id is not None:
            scopes.append(CategoryRule.account_id == account_id)
        rows = await self._session.scalars(
            select(CategoryRule)
            .join(Category, Category.id == CategoryRule.category_id)
            .where(
                CategoryRule.user_id == user_id,
                CategoryRule.kind == kind.value,
                Category.archived_at.is_(None),
                or_(*scopes),
            )
            .order_by(CategoryRule.updated_at.desc(), CategoryRule.id.desc())
            .limit(CATEGORY_RULE_FETCH_LIMIT)
        )
        rules = rows.all()
        if len(rules) > MAX_CATEGORY_RULES_PER_OWNER_KIND:
            raise CatalogUnavailableError("Набор правил категоризации превышает допустимый размер")
        return tuple(_spec(rule) for rule in rules)

    async def upsert(
        self,
        user_id: UUID,
        kind: TransactionType,
        category_id: UUID,
        normalized_pattern: str,
        account_id: UUID | None,
    ) -> CategoryRuleSpec:
        normalized = normalize_rule_pattern(normalized_pattern)
        owner = await self._session.scalar(
            select(User.id).where(User.id == user_id).with_for_update()
        )
        if owner is None:
            raise ObjectNotFoundError("Пользователь не найден")
        scope_filter = (
            CategoryRule.account_id.is_(None)
            if account_id is None
            else CategoryRule.account_id == account_id
        )
        existing = await self._session.scalar(
            select(CategoryRule.id).where(
                CategoryRule.user_id == user_id,
                CategoryRule.kind == kind.value,
                CategoryRule.normalized_pattern == normalized,
                scope_filter,
            )
        )
        if existing is None:
            at_capacity = await self._session.scalar(
                select(CategoryRule.id)
                .where(
                    CategoryRule.user_id == user_id,
                    CategoryRule.kind == kind.value,
                )
                .offset(MAX_CATEGORY_RULES_PER_OWNER_KIND - 1)
                .limit(1)
            )
            if at_capacity is not None:
                raise CatalogUnavailableError(
                    "Набор правил категоризации достиг допустимого размера"
                )
        values = {
            "user_id": user_id,
            "kind": kind.value,
            "category_id": category_id,
            "normalized_pattern": normalized,
            "pattern": normalized,
            "account_id": account_id,
        }
        statement = insert(CategoryRule).values(**values)
        if account_id is None:
            statement = statement.on_conflict_do_update(
                index_elements=(
                    CategoryRule.user_id,
                    CategoryRule.kind,
                    CategoryRule.normalized_pattern,
                ),
                index_where=CategoryRule.account_id.is_(None),
                set_={
                    "category_id": category_id,
                    "pattern": normalized,
                    "version": CategoryRule.version + 1,
                    "updated_at": func.now(),
                },
            )
        else:
            statement = statement.on_conflict_do_update(
                index_elements=(
                    CategoryRule.user_id,
                    CategoryRule.account_id,
                    CategoryRule.kind,
                    CategoryRule.normalized_pattern,
                ),
                index_where=CategoryRule.account_id.is_not(None),
                set_={
                    "category_id": category_id,
                    "pattern": normalized,
                    "version": CategoryRule.version + 1,
                    "updated_at": func.now(),
                },
            )
        rule_id = (await self._session.execute(statement.returning(CategoryRule.id))).scalar_one()
        rule = await self._session.scalar(
            select(CategoryRule)
            .where(CategoryRule.id == rule_id)
            .execution_options(populate_existing=True)
        )
        if rule is None:  # pragma: no cover - RETURNING guarantees this
            raise RuntimeError("Сохранённое правило не найдено")
        return _spec(rule)
