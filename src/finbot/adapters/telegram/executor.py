from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finbot.adapters.database.services.onboarding import ensure_owner_user
from finbot.adapters.database.services.updates import claim_update

type TelegramMutationSession = AsyncSession


@dataclass(frozen=True, slots=True)
class TelegramMutationRequest:
    """Private adapter input required to authenticate and deduplicate one update."""

    update_id: int | None = field(repr=False)
    owner_telegram_user_id: int = field(repr=False)
    chat_id: int = field(repr=False)
    locale: str = field(repr=False)
    timezone: str = field(repr=False)
    currency: str = field(repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.update_id, bool) or (
            self.update_id is not None and not isinstance(self.update_id, int)
        ):
            raise TypeError("Telegram update id must be an integer")
        if self.update_id is not None and self.update_id <= 0:
            raise ValueError("Telegram update id must be positive")
        for value, label in (
            (self.owner_telegram_user_id, "owner user"),
            (self.chat_id, "chat"),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"Telegram {label} id must be an integer")
            if value <= 0:
                raise ValueError(f"Telegram {label} id must be positive")


@dataclass(frozen=True, slots=True)
class DuplicateTelegramMutation:
    """The update was already claimed by a committed transaction."""


@dataclass(frozen=True, slots=True)
class SuccessfulTelegramMutation[MutationValue, ReceiptValue]:
    """A committed mutation and its post-commit receipt, both repr-hidden."""

    value: MutationValue = field(repr=False)
    receipt: ReceiptValue = field(repr=False)


type TelegramMutationExecution[MutationValue, ReceiptValue] = (
    DuplicateTelegramMutation | SuccessfulTelegramMutation[MutationValue, ReceiptValue]
)
type MutationCallback[MutationValue] = Callable[[AsyncSession, UUID], Awaitable[MutationValue]]
type ReceiptBuilder[MutationValue, ReceiptValue] = Callable[[MutationValue], ReceiptValue]
type ReceiptCallback[ReceiptValue] = Callable[
    [AsyncSession, TelegramMutationRequest, ReceiptValue], Awaitable[None]
]


class TelegramMutationExecutor:
    """Commit an owner mutation and its Telegram receipt in one transaction.

    Callbacks may flush but must not commit, roll back, perform network I/O, or
    acknowledge Telegram callbacks. The receipt callback only queues a durable
    outbox row using the supplied session. Delivery remains post-commit work.
    """

    __slots__ = ("_sessions",)

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def execute[MutationValue, ReceiptValue](
        self,
        request: TelegramMutationRequest,
        *,
        mutate: MutationCallback[MutationValue],
        build_receipt: ReceiptBuilder[MutationValue, ReceiptValue],
        enqueue_receipt: ReceiptCallback[ReceiptValue],
    ) -> TelegramMutationExecution[MutationValue, ReceiptValue]:
        async with self._sessions() as session:
            try:
                if not await claim_update(session, request.update_id):
                    await session.rollback()
                    return DuplicateTelegramMutation()
                owner = await ensure_owner_user(
                    session,
                    telegram_user_id=request.owner_telegram_user_id,
                    telegram_chat_id=request.chat_id,
                    locale=request.locale,
                    timezone=request.timezone,
                    currency=request.currency,
                )
                value = await mutate(session, owner.id)
                receipt = build_receipt(value)
                if request.update_id is not None:
                    await enqueue_receipt(session, request, receipt)
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
        return SuccessfulTelegramMutation(value, receipt)
