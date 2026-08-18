from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.category_rules import SqlAlchemyCategoryRuleRepository
from finbot.adapters.database.models import Account, Category
from finbot.adapters.database.services.transactions import resolve_account, resolve_category
from finbot.application.dto import AccountSnapshot, CategorySnapshot
from finbot.domain.category_rules import CategoryRuleSpec
from finbot.domain.errors import UnknownAccountError, UnknownCategoryError
from finbot.domain.transactions import TransactionType


def _account_snapshot(account: Account) -> AccountSnapshot:
    return AccountSnapshot(
        account_id=account.id,
        name=account.name,
        account_type=account.type,
        currency=account.currency,
        archived_at=account.archived_at,
        version=account.version,
    )


def _category_snapshot(category: Category) -> CategorySnapshot:
    return CategorySnapshot(
        category_id=category.id,
        kind=TransactionType(category.kind),
        name=category.name,
        emoji=category.emoji,
        archived_at=category.archived_at,
        version=category.version,
    )


class SqlAlchemyDraftPreparationRepository:
    """Read-only catalog/rule adapter for application draft preparation."""

    __slots__ = ("_category_rules", "_session")

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._category_rules = SqlAlchemyCategoryRuleRepository(session)

    async def resolve_account(
        self,
        owner_id: UUID,
        hint: str | None,
        default_account_id: UUID | None,
    ) -> AccountSnapshot | None:
        try:
            account = await resolve_account(
                self._session,
                owner_id,
                hint,
                default_account_id,
            )
        except UnknownAccountError:
            return None
        return _account_snapshot(account)

    async def resolve_category(
        self,
        owner_id: UUID,
        kind: TransactionType,
        hint: str | None,
    ) -> CategorySnapshot | None:
        try:
            category = await resolve_category(self._session, owner_id, kind.value, hint)
        except UnknownCategoryError:
            return None
        return _category_snapshot(category)

    async def get_category(
        self,
        owner_id: UUID,
        kind: TransactionType,
        category_id: UUID,
    ) -> CategorySnapshot | None:
        category = cast(
            Category | None,
            await self._session.scalar(
                select(Category).where(
                    Category.id == category_id,
                    Category.user_id == owner_id,
                    Category.kind == kind.value,
                    Category.archived_at.is_(None),
                )
            ),
        )
        return _category_snapshot(category) if category is not None else None

    async def list_applicable(
        self,
        user_id: UUID,
        kind: TransactionType,
        account_id: UUID | None,
    ) -> tuple[CategoryRuleSpec, ...]:
        return await self._category_rules.list_applicable(user_id, kind, account_id)
