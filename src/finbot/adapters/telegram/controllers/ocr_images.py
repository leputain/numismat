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
from finbot.application.dto import (
    AccountSnapshot,
    CategorySnapshot,
    DraftRef,
    OcrImageIngressStatus,
    OwnerSnapshot,
    ProcessOcrImageCommand,
    ProcessOcrImageResult,
)
from finbot.application.use_cases.ocr_queue import ProcessOcrImage
from finbot.application.use_cases.queries import GetOwnerSettings, ListAccounts, ListCategories
from finbot.domain.transactions import TransactionType


@dataclass(frozen=True, slots=True)
class TelegramOcrImageContext:
    """Bounded image input plus the private Telegram mutation envelope.

    An empty byte string is a safe adapter sentinel for a metadata/download
    rejection that still needs an idempotent durable response.
    """

    request: TelegramMutationRequest = field(repr=False)
    content: bytes = field(repr=False)
    mime_type: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.request, TelegramMutationRequest):
            raise TypeError("Telegram mutation request is invalid")
        if not isinstance(self.content, bytes):
            raise TypeError("OCR image content must be bytes")
        if not isinstance(self.mime_type, str):
            raise TypeError("OCR image MIME type must be a string")


@dataclass(frozen=True, slots=True)
class OcrImageReceiptSnapshot:
    """Pure, complete renderer input captured in the mutation transaction."""

    result: ProcessOcrImageResult = field(repr=False)
    owner: OwnerSnapshot = field(repr=False)
    active_accounts: tuple[AccountSnapshot, ...] = field(default=(), repr=False)
    active_categories: tuple[CategorySnapshot, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.result, ProcessOcrImageResult):
            raise TypeError("OCR image result is invalid")
        if not isinstance(self.owner, OwnerSnapshot):
            raise TypeError("OCR image owner snapshot is invalid")
        if not isinstance(self.active_accounts, tuple) or not all(
            isinstance(account, AccountSnapshot) for account in self.active_accounts
        ):
            raise TypeError("OCR image account choices are invalid")
        if not isinstance(self.active_categories, tuple) or not all(
            isinstance(category, CategorySnapshot) for category in self.active_categories
        ):
            raise TypeError("OCR image category choices are invalid")

        if self.result.status is not OcrImageIngressStatus.STARTED:
            if self.active_accounts or self.active_categories:
                raise ValueError("Non-started OCR receipt must not contain catalog choices")
            return

        draft = self.result.draft
        if draft is None:  # pragma: no cover - application DTO invariant
            raise ValueError("Started OCR receipt requires a draft")
        if draft.state == "account_required":
            if self.active_categories:
                raise ValueError("OCR account receipt must not contain category choices")
            return
        if draft.state == "category_required":
            if self.active_accounts:
                raise ValueError("OCR category receipt must not contain account choices")
            try:
                kind = TransactionType(str(draft.payload.get("type", "")))
            except ValueError:
                raise ValueError("OCR category receipt has an invalid transaction type") from None
            if any(category.kind is not kind for category in self.active_categories):
                raise ValueError("OCR category receipt contains choices of another type")
            return
        if draft.state != "review" or self.active_accounts or self.active_categories:
            raise ValueError("Started OCR receipt has an unsupported prepared state")

    @property
    def draft_ref(self) -> DraftRef | None:
        draft = self.result.draft
        return draft.ref if draft is not None else None


@dataclass(frozen=True, slots=True)
class OcrImageSessionUseCases:
    process_image: ProcessOcrImage = field(repr=False)
    get_owner_settings: GetOwnerSettings = field(repr=False)
    list_accounts: ListAccounts = field(repr=False)
    list_categories: ListCategories = field(repr=False)


class OcrImageUseCaseFactory(Protocol):
    def __call__(self, session: TelegramMutationSession, /) -> OcrImageSessionUseCases: ...


type OcrImageReceiptEnqueuer = Callable[
    [TelegramMutationSession, TelegramMutationRequest, OcrImageReceiptSnapshot],
    Awaitable[None],
]


def _same_receipt(receipt: OcrImageReceiptSnapshot) -> OcrImageReceiptSnapshot:
    return receipt


class OcrImageController:
    """Claim, process, persist, and enqueue one OCR ingress in one SQL UoW.

    Telegram download happens before this boundary.  Local extraction runs only
    after the durable update claim, so a replay cannot invoke OCR twice after a
    successful commit.  No network I/O is performed by this controller.
    """

    __slots__ = ("_enqueue_receipt", "_executor", "_use_cases")

    def __init__(
        self,
        executor: TelegramMutationExecutor,
        use_case_factory: OcrImageUseCaseFactory,
        enqueue_receipt: OcrImageReceiptEnqueuer,
    ) -> None:
        self._executor = executor
        self._use_cases = use_case_factory
        self._enqueue_receipt = enqueue_receipt

    async def process(
        self,
        context: TelegramOcrImageContext,
    ) -> OcrImageReceiptSnapshot | None:
        if not isinstance(context, TelegramOcrImageContext):
            raise TypeError("Telegram OCR image context is invalid")

        async def mutate(
            session: TelegramMutationSession,
            owner_id: UUID,
        ) -> OcrImageReceiptSnapshot:
            use_cases = self._use_cases(session)
            try:
                command = ProcessOcrImageCommand(
                    owner_id=owner_id,
                    content=context.content,
                    mime_type=context.mime_type,
                )
            except ValueError:
                result = ProcessOcrImageResult(OcrImageIngressStatus.REJECTED)
            else:
                result = await use_cases.process_image(command)

            owner = await use_cases.get_owner_settings(owner_id)
            active_accounts: tuple[AccountSnapshot, ...] = ()
            active_categories: tuple[CategorySnapshot, ...] = ()
            if result.status is OcrImageIngressStatus.STARTED:
                draft = result.draft
                if draft is None:  # pragma: no cover - application DTO invariant
                    raise RuntimeError("Started OCR image result has no draft")
                if draft.state == "account_required":
                    active_accounts = await use_cases.list_accounts(owner_id)
                elif draft.state == "category_required":
                    try:
                        kind = TransactionType(str(draft.payload.get("type", "")))
                    except ValueError:
                        raise RuntimeError("Prepared OCR draft has an invalid type") from None
                    active_categories = await use_cases.list_categories(owner_id, kind=kind)

            return OcrImageReceiptSnapshot(
                result=result,
                owner=owner,
                active_accounts=active_accounts,
                active_categories=active_categories,
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
