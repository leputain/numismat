from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import Account, Category, User
from finbot.application.services.onboarding import (
    DEFAULT_ACCOUNT_NAME,
    DEFAULT_ACCOUNT_SLUG,
    INITIAL_CATEGORIES,
    normalize_currency_code,
)


class OwnerChatBindingError(RuntimeError):
    """The verified Telegram actor does not match the immutable private-chat binding."""


# A database-scoped constant avoids putting a Telegram identifier in the advisory-lock
# statement while still serializing first-use setup across workers and processes. The
# xact lock is deliberately held until caller commit.
_ONBOARDING_ADVISORY_LOCK = 0x46494E424F54


async def _lock_onboarding(session: AsyncSession) -> None:
    await session.scalar(select(func.pg_advisory_xact_lock(_ONBOARDING_ADVISORY_LOCK)))


async def _create_default_account(
    session: AsyncSession,
    user_id: UUID,
    currency: str,
) -> Account:
    account = Account(
        user_id=user_id,
        name=DEFAULT_ACCOUNT_NAME,
        slug=DEFAULT_ACCOUNT_SLUG,
        type="card",
        currency=currency,
    )
    session.add(account)
    await session.flush()
    return account


async def _repair_missing_default(
    session: AsyncSession,
    user: User,
    currency: str,
) -> None:
    if user.default_account_id is not None:
        return

    account = await session.scalar(
        select(Account)
        .where(Account.user_id == user.id, Account.archived_at.is_(None))
        .order_by(Account.created_at, Account.id)
        .limit(1)
        .execution_options(populate_existing=True)
    )
    if account is None:
        account = await session.scalar(
            select(Account)
            .where(
                Account.user_id == user.id,
                Account.slug == DEFAULT_ACCOUNT_SLUG,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if account is None:
            account = await _create_default_account(session, user.id, currency)
        else:
            account.archived_at = None
            account.version += 1
    user.default_account_id = account.id


async def ensure_owner_user(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    telegram_chat_id: int,
    locale: str,
    timezone: str,
    currency: str,
) -> User:
    """Load or atomically initialize the owner and their complete seed catalog."""

    if (
        type(telegram_user_id) is not int
        or type(telegram_chat_id) is not int
        or telegram_user_id <= 0
        or telegram_user_id != telegram_chat_id
    ):
        raise OwnerChatBindingError("Telegram private-chat binding is invalid")
    normalized_currency = normalize_currency_code(currency)
    await _lock_onboarding(session)
    user = await session.scalar(
        select(User)
        .where(User.telegram_user_id == telegram_user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None:
        user = User(
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            locale=locale,
            timezone=timezone,
            base_currency=normalized_currency,
            fast_mode=False,
        )
        session.add(user)
        await session.flush()

        account = await _create_default_account(session, user.id, normalized_currency)
        session.add_all(
            Category(
                user_id=user.id,
                kind=category.kind,
                name=category.name,
                slug=category.slug,
                emoji=category.emoji,
            )
            for category in INITIAL_CATEGORIES
        )
        await session.flush()
        user.default_account_id = account.id
        return user

    if user.telegram_chat_id is None:
        user.telegram_chat_id = telegram_chat_id
    elif user.telegram_chat_id != telegram_chat_id:
        raise OwnerChatBindingError("Telegram private-chat binding is immutable")
    await _repair_missing_default(session, user, normalized_currency)
    return user
