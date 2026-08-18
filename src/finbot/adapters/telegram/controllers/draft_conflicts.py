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
from finbot.application.draft_conflicts import (
    DraftConflictResult,
    ResolveDraftConflictCommand,
)
from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    DraftConflictResolution,
    DraftRef,
    OwnerSnapshot,
    TransactionSnapshot,
)
from finbot.application.errors import DraftRevisionConflictError
from finbot.application.interactions import MAX_PAGE
from finbot.application.use_cases.draft_conflicts import ResolveDraftConflict
from finbot.application.use_cases.queries import (
    GetOwnerSettings,
    GetTransaction,
    ListAccounts,
    ListCategories,
)
from finbot.domain.transactions import TransactionType

_CATEGORY_CHOICE_STATES = frozenset(
    {
        "wizard_category",
        "quick_category",
        "review_category",
        "category_required",
    }
)
_ACCOUNT_CHOICE_STATES = frozenset(
    {
        "wizard_account",
        "quick_account",
        "review_account",
        "account_required",
    }
)
_EDIT_CATEGORY_STATE = "edit_category"
_EDIT_ACCOUNT_STATE = "edit_account"
# The edit callback codec reserves three values for each history page.
_MAX_EDIT_HISTORY_PAGE = MAX_PAGE // 3


@dataclass(frozen=True, slots=True)
class TelegramDraftConflictContext:
    request: TelegramMutationRequest = field(repr=False)
    message_id: int = field(repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.message_id, bool) or not isinstance(self.message_id, int):
            raise TypeError("Telegram message id must be an integer")
        if self.message_id <= 0:
            raise ValueError("Telegram message id must be positive")


@dataclass(frozen=True, slots=True)
class DraftConflictReceiptChoices:
    """Active catalog rows required by the resulting draft screen."""

    accounts: tuple[AccountSnapshot, ...] = field(default=(), repr=False)
    categories: tuple[CategorySnapshot, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.accounts, tuple) or any(
            not isinstance(account, AccountSnapshot) for account in self.accounts
        ):
            raise TypeError("Draft conflict account choices are invalid")
        if not isinstance(self.categories, tuple) or any(
            not isinstance(category, CategorySnapshot) for category in self.categories
        ):
            raise TypeError("Draft conflict category choices are invalid")
        if self.accounts and self.categories:
            raise ValueError("Draft conflict receipt cannot present two catalog kinds")
        if any(account.archived_at is not None for account in self.accounts):
            raise ValueError("Draft conflict account choices must be active")
        if any(category.archived_at is not None for category in self.categories):
            raise ValueError("Draft conflict category choices must be active")


@dataclass(frozen=True, slots=True)
class DraftConflictReceiptSnapshot:
    """Complete Telegram renderer input captured before the transaction commits."""

    expected: DraftRef = field(repr=False)
    result: DraftConflictResult = field(repr=False)
    owner: OwnerSnapshot = field(repr=False)
    message_id: int = field(repr=False)
    history_page: int = field(repr=False)
    choices: DraftConflictReceiptChoices = field(
        default_factory=DraftConflictReceiptChoices,
        repr=False,
    )
    transaction: TransactionSnapshot | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _validate_expected(self.expected)
        _validate_message_id(self.message_id)
        if not isinstance(self.result, DraftConflictResult):
            raise TypeError("Draft conflict result is invalid")
        if not isinstance(self.owner, OwnerSnapshot):
            raise TypeError("Draft conflict owner snapshot is invalid")
        if not isinstance(self.choices, DraftConflictReceiptChoices):
            raise TypeError("Draft conflict receipt choices are invalid")
        if self.transaction is not None and not isinstance(self.transaction, TransactionSnapshot):
            raise TypeError("Draft conflict transaction snapshot is invalid")
        if self.result.draft.suspended:
            raise ValueError("Resolved draft must be active")
        if not isinstance(self.result.resolution, DraftConflictResolution):
            raise TypeError("Draft conflict result resolution is invalid")
        if self.result.resolution is DraftConflictResolution.REPLACE:
            if (
                self.result.draft.draft_id == self.expected.draft_id
                or self.result.draft.revision != 1
            ):
                raise ValueError("Replacement receipt requires a fresh draft identity")
        elif (
            self.result.draft.draft_id != self.expected.draft_id
            or self.result.draft.revision != self.expected.revision + 1
        ):
            raise ValueError("Continued draft receipt does not match the expected revision")
        if "pending_intent" in self.result.draft.payload:
            raise ValueError("Resolved draft must not retain a pending intent")
        if "history_page" in self.result.draft.payload:
            raise ValueError("Resolved draft must not contain Telegram navigation state")
        _validate_history_page(self.history_page)

        state = self.result.draft.state
        requires_transaction = state.startswith("edit_")
        if requires_transaction != (self.transaction is not None):
            raise ValueError("Edit draft receipt requires one transaction snapshot")
        if self.transaction is not None:
            transaction_id, expected_version = _transaction_target(self.result)
            if (
                self.transaction.transaction_id != transaction_id
                or self.transaction.version != expected_version
                or self.transaction.deleted_at is not None
            ):
                raise ValueError("Edit draft transaction snapshot is not authoritative")
        if requires_transaction and self.history_page > _MAX_EDIT_HISTORY_PAGE:
            raise ValueError("Edit draft history page cannot be encoded safely")
        if state in _ACCOUNT_CHOICE_STATES | {_EDIT_ACCOUNT_STATE}:
            if self.choices.categories:
                raise ValueError("Account draft receipt cannot contain category choices")
        elif state in _CATEGORY_CHOICE_STATES | {_EDIT_CATEGORY_STATE}:
            if self.choices.accounts:
                raise ValueError("Category draft receipt cannot contain account choices")
            if state == _EDIT_CATEGORY_STATE:
                if self.transaction is None:  # pragma: no cover - invariant above
                    raise RuntimeError("Edit category receipt has no transaction")
                category_kind = self.transaction.kind
            else:
                try:
                    category_kind = TransactionType(str(self.result.draft.payload["type"]))
                except KeyError, ValueError:
                    raise ValueError("Category draft transaction type is invalid") from None
            if any(category.kind is not category_kind for category in self.choices.categories):
                raise ValueError("Draft conflict categories do not match the screen type")
        elif self.choices.accounts or self.choices.categories:
            raise ValueError("Draft receipt contains catalog choices for the wrong screen")

    @property
    def draft_ref(self) -> DraftRef:
        return self.result.draft.ref


@dataclass(frozen=True, slots=True)
class DraftConflictSessionUseCases:
    resolve: ResolveDraftConflict = field(repr=False)
    get_owner_settings: GetOwnerSettings = field(repr=False)
    list_accounts: ListAccounts = field(repr=False)
    list_categories: ListCategories = field(repr=False)
    get_transaction: GetTransaction = field(repr=False)


class DraftConflictUseCaseFactory(Protocol):
    def __call__(self, session: TelegramMutationSession, /) -> DraftConflictSessionUseCases: ...


class DraftConflictPresentationContext(Protocol):
    """Structural view of adapter-only context; application DTOs never see it."""

    @property
    def history_page(self) -> int | None: ...

    @property
    def pending_history_page(self) -> int | None: ...


class DraftConflictPresentationContextReader(Protocol):
    async def __call__(
        self,
        session: TelegramMutationSession,
        owner_id: UUID,
        expected: DraftRef,
        chat_id: int,
        message_id: int,
        *,
        allow_suspended: bool = False,
    ) -> DraftConflictPresentationContext | None: ...


type DraftConflictReceiptEnqueuer = Callable[
    [TelegramMutationSession, TelegramMutationRequest, DraftConflictReceiptSnapshot],
    Awaitable[None],
]


def _validate_expected(expected: DraftRef) -> None:
    if not isinstance(expected, DraftRef):
        raise TypeError("Expected draft reference must be a DraftRef")
    if not isinstance(expected.draft_id, UUID):
        raise TypeError("Draft id must be a UUID")
    if isinstance(expected.revision, bool) or not isinstance(expected.revision, int):
        raise TypeError("Draft revision must be an integer")
    if expected.revision < 1:
        raise ValueError("Draft revision must be positive")


def _validate_message_id(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("Telegram message id must be an integer")
    if value <= 0:
        raise ValueError("Telegram message id must be positive")


def _validate_history_page(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("Transaction history page must be an integer")
    if not 0 <= value <= MAX_PAGE:
        raise ValueError("Transaction history page is outside the supported range")


def _effective_history_page(
    context: DraftConflictPresentationContext,
    resolution: DraftConflictResolution,
) -> int:
    value = (
        context.pending_history_page
        if resolution is DraftConflictResolution.REPLACE
        else context.history_page
    )
    # Both nullable fields are absent on a valid rolling-deploy legacy binding.
    # Its historical behaviour was page zero, which remains the only safe fallback.
    if value is None:
        value = 0
    _validate_history_page(value)
    return value


def _transaction_target(result: DraftConflictResult) -> tuple[UUID, int]:
    payload = result.draft.payload
    try:
        transaction_id = UUID(str(payload["transaction_id"]))
        version_raw = payload["version"]
        if isinstance(version_raw, bool):
            raise ValueError
        version = int(str(version_raw))
    except KeyError, TypeError, ValueError:
        raise ValueError("Edit draft transaction reference is invalid") from None
    if version < 1:
        raise ValueError("Edit draft transaction version must be positive")
    return transaction_id, version


async def _receipt_choices(
    use_cases: DraftConflictSessionUseCases,
    owner_id: UUID,
    result: DraftConflictResult,
) -> tuple[DraftConflictReceiptChoices, TransactionSnapshot | None]:
    state = result.draft.state
    transaction: TransactionSnapshot | None = None

    if state.startswith("edit_"):
        transaction_id, expected_version = _transaction_target(result)
        transaction = await use_cases.get_transaction(owner_id, transaction_id)
        if (
            transaction.transaction_id != transaction_id
            or transaction.version != expected_version
            or transaction.deleted_at is not None
        ):
            raise ValueError("Edit draft transaction snapshot is no longer authoritative")

    if state in _ACCOUNT_CHOICE_STATES or state == _EDIT_ACCOUNT_STATE:
        accounts = await use_cases.list_accounts(owner_id, archived=False)
        return DraftConflictReceiptChoices(accounts=accounts), transaction

    if state in _CATEGORY_CHOICE_STATES:
        try:
            kind = TransactionType(str(result.draft.payload["type"]))
        except KeyError, ValueError:
            raise ValueError("Category draft transaction type is invalid") from None
        categories = await use_cases.list_categories(
            owner_id,
            kind=kind,
            archived=False,
        )
        if any(category.kind is not kind for category in categories):
            raise ValueError("Category draft choices do not match its transaction type")
        return DraftConflictReceiptChoices(categories=categories), transaction

    if state == _EDIT_CATEGORY_STATE:
        if transaction is None:  # pragma: no cover - guarded by the edit prefix
            raise RuntimeError("Edit category receipt has no transaction")
        categories = await use_cases.list_categories(
            owner_id,
            kind=transaction.kind,
            archived=False,
        )
        if any(category.kind is not transaction.kind for category in categories):
            raise ValueError("Edit category choices do not match the transaction type")
        return DraftConflictReceiptChoices(categories=categories), transaction

    return DraftConflictReceiptChoices(), transaction


def _same_receipt(receipt: DraftConflictReceiptSnapshot) -> DraftConflictReceiptSnapshot:
    return receipt


class DraftConflictController:
    """Resolve one exact Telegram-presented conflict in a single SQL transaction."""

    __slots__ = ("_enqueue_receipt", "_executor", "_presentation_context", "_use_cases")

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        use_case_factory: DraftConflictUseCaseFactory,
        presentation_context_reader: DraftConflictPresentationContextReader,
        enqueue_receipt: DraftConflictReceiptEnqueuer,
    ) -> None:
        self._executor = executor
        self._use_cases = use_case_factory
        self._presentation_context = presentation_context_reader
        self._enqueue_receipt = enqueue_receipt

    async def resolve(
        self,
        context: TelegramDraftConflictContext,
        expected: DraftRef,
        resolution: DraftConflictResolution,
    ) -> DraftConflictReceiptSnapshot | None:
        _validate_expected(expected)
        if not isinstance(resolution, DraftConflictResolution):
            raise TypeError("Draft conflict resolution is invalid")

        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> DraftConflictReceiptSnapshot:
            presentation_context = await self._presentation_context(
                session,
                owner_id,
                expected,
                context.request.chat_id,
                context.message_id,
                allow_suspended=True,
            )
            if presentation_context is None:
                raise DraftRevisionConflictError(current_revision=None)
            use_cases = self._use_cases(session)
            result = await use_cases.resolve.execute(
                ResolveDraftConflictCommand(owner_id, expected, resolution)
            )
            if result.resolution is not resolution:
                raise ValueError("Draft conflict result does not match the requested resolution")
            owner = await use_cases.get_owner_settings(owner_id)
            if owner.owner_id != owner_id:
                raise ValueError("Draft conflict owner snapshot does not match the request")
            choices, transaction = await _receipt_choices(use_cases, owner_id, result)
            return DraftConflictReceiptSnapshot(
                expected=expected,
                result=result,
                owner=owner,
                choices=choices,
                transaction=transaction,
                message_id=context.message_id,
                history_page=_effective_history_page(presentation_context, resolution),
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
