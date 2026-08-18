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
from finbot.application.draft_conflicts import PENDING_DRAFT_INTENT_KEY
from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    DraftRef,
    DraftSnapshot,
    OwnerSnapshot,
)
from finbot.application.errors import (
    DraftRevisionConflictError,
    EntityNotFoundError,
    InvalidStateError,
)
from finbot.application.settings_text_input import (
    SETTINGS_TEXT_INPUT_STATES,
    SettingsTextInputNotApplicableError,
    SettingsTextInputTarget,
)
from finbot.domain.transactions import TransactionType

_PRESENTATION_KEYS = frozenset(
    {
        "presentation_ref",
        "telegram_chat_id",
        "telegram_message_id",
        "ui_message_id",
        "history_page",
        "pending_history_page",
    }
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


def _owner_snapshot(owner: User) -> OwnerSnapshot:
    return OwnerSnapshot(
        owner_id=owner.id,
        locale=owner.locale,
        timezone=owner.timezone,
        base_currency=owner.base_currency,
        default_account_id=owner.default_account_id,
        fast_mode=owner.fast_mode,
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


def _payload_uuid(payload: Mapping[str, object], key: str) -> UUID:
    try:
        return UUID(str(payload[key]))
    except KeyError, ValueError:
        raise InvalidStateError("Черновик ввода настроек повреждён") from None


class SqlAlchemySettingsTextInputRepository:
    """Owner-serialized exact settings draft and catalog target adapter."""

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

    async def _locked_draft(self, owner_id: UUID) -> Draft | None:
        return cast(
            Draft | None,
            await self._session.scalar(
                select(Draft)
                .where(Draft.user_id == owner_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ),
        )

    @staticmethod
    def _require_applicable(draft: Draft | None) -> Draft:
        if draft is None or draft.suspended or draft.state not in SETTINGS_TEXT_INPUT_STATES:
            raise SettingsTextInputNotApplicableError("Нет активного черновика ввода настроек")
        if PENDING_DRAFT_INTENT_KEY in draft.payload:
            raise InvalidStateError("Сначала выберите действие для незавершённого ввода")
        return draft

    @staticmethod
    def _require_exact(draft: Draft | None, expected: DraftRef) -> Draft:
        if draft is None or draft.id != expected.draft_id or draft.revision != expected.revision:
            raise DraftRevisionConflictError(
                current_revision=draft.revision if draft is not None else None
            )
        return draft

    async def lock_active(self, owner_id: UUID) -> DraftSnapshot:
        await self._lock_owner(owner_id)
        return _draft_snapshot(self._require_applicable(await self._locked_draft(owner_id)))

    async def presentation_message_id(
        self,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
    ) -> int | None:
        owner = await self._lock_owner(owner_id)
        draft = self._require_exact(await self._locked_draft(owner_id), expected)
        self._require_applicable(draft)
        projection = (
            await self._session.execute(
                select(
                    TelegramDraftPresentation.chat_id,
                    TelegramDraftPresentation.message_id,
                )
                .where(
                    TelegramDraftPresentation.draft_id == draft.id,
                    TelegramDraftPresentation.rendered_revision == draft.revision,
                )
                .with_for_update()
            )
        ).one_or_none()
        if projection is not None:
            projected_chat_id, projected_message_id = projection._t
            return int(projected_message_id) if projected_chat_id == chat_id else None

        if owner.telegram_chat_id not in {None, chat_id}:
            return None
        legacy = draft.payload.get("ui_message_id", draft.presentation_ref)
        try:
            message_id = int(str(legacy)) if legacy is not None else None
        except ValueError:
            return None
        return message_id if message_id is not None and message_id > 0 else None

    async def lock_target(
        self,
        owner_id: UUID,
        expected: DraftRef,
    ) -> SettingsTextInputTarget:
        owner = await self._lock_owner(owner_id)
        draft = self._require_exact(await self._locked_draft(owner_id), expected)
        self._require_applicable(draft)
        snapshot = _draft_snapshot(draft)
        account: AccountSnapshot | None = None
        category: CategorySnapshot | None = None

        if draft.state == "settings_account_rename":
            account_id = _payload_uuid(snapshot.payload, "account_id")
            account_row = cast(
                Account | None,
                await self._session.scalar(
                    select(Account)
                    .where(
                        Account.id == account_id,
                        Account.user_id == owner_id,
                        Account.archived_at.is_(None),
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ),
            )
            account = _account_snapshot(account_row) if account_row is not None else None
        elif draft.state == "settings_category_rename":
            category_id = _payload_uuid(snapshot.payload, "category_id")
            category_row = cast(
                Category | None,
                await self._session.scalar(
                    select(Category)
                    .where(
                        Category.id == category_id,
                        Category.user_id == owner_id,
                        Category.archived_at.is_(None),
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ),
            )
            category = _category_snapshot(category_row) if category_row is not None else None

        try:
            return SettingsTextInputTarget(
                owner=_owner_snapshot(owner),
                draft=snapshot,
                account=account,
                category=category,
            )
        except KeyError, TypeError, ValueError:
            raise InvalidStateError("Черновик ввода настроек повреждён") from None

    async def get_account(
        self,
        owner_id: UUID,
        account_id: UUID,
    ) -> AccountSnapshot | None:
        account = cast(
            Account | None,
            await self._session.scalar(
                select(Account)
                .where(
                    Account.id == account_id,
                    Account.user_id == owner_id,
                    Account.archived_at.is_(None),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ),
        )
        return _account_snapshot(account) if account is not None else None

    async def get_category(
        self,
        owner_id: UUID,
        category_id: UUID,
    ) -> CategorySnapshot | None:
        category = cast(
            Category | None,
            await self._session.scalar(
                select(Category)
                .where(
                    Category.id == category_id,
                    Category.user_id == owner_id,
                    Category.archived_at.is_(None),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ),
        )
        return _category_snapshot(category) if category is not None else None
