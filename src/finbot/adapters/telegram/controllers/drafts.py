from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from finbot.adapters.telegram.executor import (
    DuplicateTelegramMutation,
    TelegramMutationExecutor,
    TelegramMutationRequest,
    TelegramMutationSession,
)
from finbot.application.dto import AccountSnapshot, DraftRef, OwnerSnapshot
from finbot.application.errors import DraftRevisionConflictError
from finbot.application.use_cases.drafts import DraftUseCases
from finbot.application.use_cases.queries import GetOwnerSettings, ListAccounts


@dataclass(frozen=True, slots=True)
class TelegramDraftContext:
    request: TelegramMutationRequest = field(repr=False)
    message_id: int = field(repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.message_id, bool) or not isinstance(self.message_id, int):
            raise TypeError("Telegram message id must be an integer")
        if self.message_id <= 0:
            raise ValueError("Telegram message id must be positive")


@dataclass(frozen=True, slots=True)
class DraftSettingsReceiptSnapshot:
    """Settings data captured after the exact draft revision was discarded."""

    owner: OwnerSnapshot = field(repr=False)
    default_account: AccountSnapshot | None = field(repr=False)
    message_id: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.message_id, bool) or (
            self.message_id is not None and not isinstance(self.message_id, int)
        ):
            raise TypeError("Telegram message id must be an integer")
        if self.message_id is not None and self.message_id <= 0:
            raise ValueError("Telegram message id must be positive")
        if self.default_account is not None and self.default_account.archived_at is not None:
            raise ValueError("Settings receipt default account must be active")
        if self.default_account is not None and (
            self.owner.default_account_id != self.default_account.account_id
        ):
            raise ValueError("Settings receipt default account does not match owner settings")


@dataclass(frozen=True, slots=True)
class DraftSessionUseCases:
    """Session-scoped application facade supplied by the composition root."""

    drafts: DraftUseCases = field(repr=False)
    get_owner_settings: GetOwnerSettings = field(repr=False)
    list_accounts: ListAccounts = field(repr=False)


class DraftUseCaseFactory(Protocol):
    def __call__(self, session: TelegramMutationSession, /) -> DraftSessionUseCases: ...


class DraftPresentationGuard(Protocol):
    """Validate the exact message presenting the draft inside the mutation UoW."""

    async def __call__(
        self,
        session: TelegramMutationSession,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
        message_id: int,
    ) -> bool: ...


type DraftReceiptEnqueuer = Callable[
    [TelegramMutationSession, TelegramMutationRequest, DraftSettingsReceiptSnapshot],
    Awaitable[None],
]


def _same_receipt(value: DraftSettingsReceiptSnapshot) -> DraftSettingsReceiptSnapshot:
    return value


def _validate_expected(expected: DraftRef) -> None:
    if not isinstance(expected, DraftRef):
        raise TypeError("Expected draft reference must be a DraftRef")
    if not isinstance(expected.draft_id, UUID):
        raise TypeError("Draft id must be a UUID")
    if isinstance(expected.revision, bool) or not isinstance(expected.revision, int):
        raise TypeError("Draft revision must be an integer")
    if expected.revision < 1:
        raise ValueError("Draft revision must be positive")


class DraftController:
    """Atomically discard one exact draft revision and queue the settings receipt."""

    __slots__ = (
        "_enqueue_receipt",
        "_executor",
        "_presentation_guard",
        "_use_case_factory",
    )

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        use_case_factory: DraftUseCaseFactory,
        presentation_guard: DraftPresentationGuard,
        enqueue_receipt: DraftReceiptEnqueuer,
    ) -> None:
        self._executor = executor
        self._use_case_factory = use_case_factory
        self._presentation_guard = presentation_guard
        self._enqueue_receipt = enqueue_receipt

    async def discard(
        self,
        context: TelegramDraftContext,
        expected: DraftRef,
    ) -> DraftSettingsReceiptSnapshot | None:
        _validate_expected(expected)

        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> DraftSettingsReceiptSnapshot:
            if not await self._presentation_guard(
                session,
                owner_id,
                expected,
                context.request.chat_id,
                context.message_id,
            ):
                raise DraftRevisionConflictError(current_revision=None)
            use_cases = self._use_case_factory(session)
            await use_cases.drafts.cancel(owner_id, expected)
            owner = await use_cases.get_owner_settings(owner_id)
            active_accounts = await use_cases.list_accounts(owner_id, archived=False)
            default_account = next(
                (
                    account
                    for account in active_accounts
                    if account.account_id == owner.default_account_id
                ),
                None,
            )
            return DraftSettingsReceiptSnapshot(
                owner=owner,
                default_account=default_account,
                message_id=context.message_id,
            )

        execution = await self._executor.execute(
            context.request,
            mutate=mutate,
            build_receipt=_same_receipt,
            enqueue_receipt=self._enqueue_receipt,
        )
        if isinstance(execution, DuplicateTelegramMutation):
            return None
        return execution.receipt
