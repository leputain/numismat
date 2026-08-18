from collections.abc import Mapping
from copy import deepcopy
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import (
    Account,
    Category,
    Draft,
    TelegramDraftPresentation,
    User,
)
from finbot.adapters.database.services.transactions import (
    create_or_get_account,
    create_or_get_category,
    resolve_account,
)
from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    DraftRef,
    DraftSnapshot,
)
from finbot.application.errors import (
    CatalogUnavailableError,
    DraftRevisionConflictError,
    EntityNotFoundError,
    InvalidStateError,
)
from finbot.application.finance_draft_text_input import (
    FinanceDraftTextInputNotApplicableError,
)
from finbot.domain.errors import UnknownAccountError
from finbot.domain.transactions import TransactionType

_PRESENTATION_KEYS = frozenset(
    {"presentation_ref", "telegram_chat_id", "telegram_message_id", "ui_message_id"}
)


def _application_payload(payload: Mapping[str, object]) -> dict[str, object]:
    return deepcopy({key: value for key, value in payload.items() if key not in _PRESENTATION_KEYS})


def _draft_snapshot(draft: Draft) -> DraftSnapshot:
    return DraftSnapshot(
        draft_id=draft.id,
        state=draft.state,
        payload=_application_payload(draft.payload),
        schema_version=draft.schema_version,
        revision=draft.revision,
        suspended=draft.suspended,
        updated_at=draft.updated_at,
    )


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


class _OwnerLock:
    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_owner(self, owner_id: UUID) -> User:
        owner = await self._session.scalar(
            select(User)
            .where(User.id == owner_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if owner is None:
            raise EntityNotFoundError("Владелец не найден")
        return owner


class SqlAlchemyFinanceDraftTextInputCatalogRepository(_OwnerLock):
    """Create-or-get finance catalogs without owning commit or rollback."""

    async def create_or_get_category(
        self,
        owner_id: UUID,
        name: str,
        kind: TransactionType,
    ) -> CategorySnapshot:
        await self.lock_owner(owner_id)
        try:
            category = await create_or_get_category(
                self._session,
                owner_id,
                name,
                kind.value,
            )
        except ValueError:
            raise CatalogUnavailableError("Категория с таким названием недоступна") from None
        return _category_snapshot(category)

    async def create_or_get_account(
        self,
        owner_id: UUID,
        name: str,
        currency: str,
    ) -> AccountSnapshot:
        await self.lock_owner(owner_id)
        try:
            account = await create_or_get_account(
                self._session,
                owner_id,
                name,
                currency,
            )
        except ValueError:
            raise CatalogUnavailableError("Счёт с таким названием недоступен") from None
        return _account_snapshot(account)

    async def resolve_account(
        self,
        owner_id: UUID,
        hint: str | None,
        default_account_id: UUID | None,
    ) -> AccountSnapshot | None:
        owner = await self.lock_owner(owner_id)
        try:
            account = await resolve_account(
                self._session,
                owner_id,
                hint,
                owner.default_account_id,
            )
        except UnknownAccountError:
            return None
        locked = cast(
            Account | None,
            await self._session.scalar(
                select(Account)
                .where(Account.id == account.id, Account.user_id == owner_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ),
        )
        if locked is None or locked.archived_at is not None:
            return None
        return _account_snapshot(locked)


class SqlAlchemyFinanceDraftTextTargetRepository(_OwnerLock):
    """Lock the current input draft and read its existing Telegram projection."""

    async def lock_active(self, owner_id: UUID) -> DraftSnapshot:
        await self.lock_owner(owner_id)
        draft = cast(
            Draft | None,
            await self._session.scalar(
                select(Draft)
                .where(Draft.user_id == owner_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ),
        )
        if draft is None:
            raise FinanceDraftTextInputNotApplicableError(
                "Нет активного черновика для текстового ввода"
            )
        if draft.suspended:
            raise FinanceDraftTextInputNotApplicableError("Черновик приостановлен")
        return _draft_snapshot(draft)

    async def presentation_message_id(
        self,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
    ) -> int | None:
        await self.lock_owner(owner_id)
        draft = cast(
            Draft | None,
            await self._session.scalar(
                select(Draft)
                .where(Draft.user_id == owner_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ),
        )
        if draft is None or draft.id != expected.draft_id or draft.revision != expected.revision:
            raise DraftRevisionConflictError(
                current_revision=draft.revision if draft is not None else None
            )
        if draft.suspended:
            raise InvalidStateError("Черновик приостановлен")

        projection = (
            await self._session.execute(
                select(
                    TelegramDraftPresentation.chat_id,
                    TelegramDraftPresentation.message_id,
                ).where(
                    TelegramDraftPresentation.draft_id == draft.id,
                    TelegramDraftPresentation.rendered_revision == draft.revision,
                )
            )
        ).one_or_none()
        if projection is not None:
            projected_chat_id, message_id = projection._t
            return int(message_id) if projected_chat_id == chat_id else None

        legacy = draft.payload.get("ui_message_id", draft.presentation_ref)
        try:
            legacy_message_id = int(str(legacy)) if legacy is not None else None
        except ValueError:
            return None
        return (
            legacy_message_id if legacy_message_id is not None and legacy_message_id > 0 else None
        )
