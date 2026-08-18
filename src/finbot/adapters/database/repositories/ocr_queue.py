from collections.abc import Mapping
from copy import deepcopy
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.category_rules import SqlAlchemyCategoryRuleRepository
from finbot.adapters.database.models import Account, Category, Draft, ImportRow, User
from finbot.adapters.database.queries.transactions import get_transaction_details
from finbot.adapters.database.services.transactions import resolve_account, save_transaction
from finbot.application.draft_conflicts import PENDING_DRAFT_INTENT_KEY
from finbot.application.dto import (
    DraftSnapshot,
    OcrOwnerContext,
    OcrQueueContinuation,
    OcrQueueMutationCommand,
    OcrQueueMutationResult,
    OcrQueueStatus,
    ReviewedTransactionInput,
    TransactionSnapshot,
)
from finbot.application.errors import (
    CatalogUnavailableError,
    DraftRevisionConflictError,
    EntityNotFoundError,
    InvalidStateError,
    OcrQueueInvalidError,
    ReviewRequiredError,
)
from finbot.application.ocr_queue import OcrQueueState, decode_ocr_queue, encode_ocr_queue
from finbot.application.rules import validate_staged_rule
from finbot.domain.errors import UnknownAccountError, UnknownCategoryError
from finbot.domain.transactions import TransactionDraft, TransactionType

_REVIEW_STATES = frozenset({"review", "quick_confirm", "wizard_confirm"})
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


def _pending_rule(payload: Mapping[str, Any]) -> tuple[str, str] | None:
    """Revalidate staged learning metadata against final reviewed values."""

    pending = payload.get("pending_rule")
    if not isinstance(pending, Mapping):
        return None
    pattern = str(pending.get("pattern", "")).strip()
    scope = str(pending.get("scope", ""))
    if pattern != str(payload.get("rule_offer_pattern", "")).strip():
        return None
    if str(pending.get("account_id", "")) != str(payload.get("account_id", "")):
        return None
    if str(pending.get("category_id", "")) != str(payload.get("category_id", "")):
        return None
    return validate_staged_rule(
        pattern,
        scope,
        str(payload.get("description", "")),
        flow=str(payload.get("flow", "")),
        category_explicit=bool(payload.get("category_explicit")),
    )


class SqlAlchemyOcrQueueCommandRepository:
    """Linearizable OCR review mutations inside an external transaction."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_ocr_context(self, owner_id: UUID) -> OcrOwnerContext | None:
        owner = await self._session.scalar(select(User).where(User.id == owner_id))
        if owner is None:
            return None
        try:
            account = await resolve_account(
                self._session,
                owner_id,
                None,
                owner.default_account_id,
            )
        except UnknownAccountError:
            raise CatalogUnavailableError("Основной счёт недоступен") from None
        currency = str(account.currency or owner.base_currency).strip().upper()
        try:
            return OcrOwnerContext(timezone=owner.timezone, currency=currency)
        except ValueError:
            raise CatalogUnavailableError("Валюта основного счёта некорректна") from None

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

    async def _locked_queue(
        self,
        command: OcrQueueMutationCommand,
    ) -> tuple[Draft, OcrQueueState]:
        draft = cast(
            Draft | None,
            await self._session.scalar(
                select(Draft)
                .where(Draft.user_id == command.owner_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ),
        )
        if (
            draft is None
            or draft.id != command.expected.draft_id
            or draft.revision != command.expected.revision
        ):
            raise DraftRevisionConflictError(
                current_revision=draft.revision if draft is not None else None
            )
        if PENDING_DRAFT_INTENT_KEY in draft.payload:
            raise InvalidStateError("Сначала выберите действие для незавершённого ввода")
        import_row_id = await self._session.scalar(
            select(ImportRow.id)
            .where(
                ImportRow.user_id == command.owner_id,
                ImportRow.draft_id == draft.id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if import_row_id is not None:
            raise InvalidStateError("Черновик банковского импорта нельзя обработать как OCR")
        return draft, decode_ocr_queue(draft.payload)

    @staticmethod
    def _require_review(draft: Draft) -> ReviewedTransactionInput:
        if draft.suspended or draft.state not in _REVIEW_STATES:
            raise ReviewRequiredError("Сначала проверьте текущую операцию")
        try:
            return ReviewedTransactionInput.from_payload(draft.payload)
        except TypeError, ValueError:
            raise ReviewRequiredError("Черновик операции неполон") from None

    async def _active_catalogs(
        self,
        owner_id: UUID,
        reviewed: ReviewedTransactionInput,
    ) -> tuple[Account, Category]:
        account = await self._session.scalar(
            select(Account)
            .where(
                Account.id == reviewed.account_id,
                Account.user_id == owner_id,
                Account.archived_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        category = await self._session.scalar(
            select(Category)
            .where(
                Category.id == reviewed.category_id,
                Category.user_id == owner_id,
                Category.kind == reviewed.kind.value,
                Category.archived_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if account is None or category is None:
            raise CatalogUnavailableError("Счёт или категория недоступны")
        return account, category

    @staticmethod
    def _validated_continuation(
        queue: OcrQueueState,
        continuation: OcrQueueContinuation | None,
    ) -> OcrQueueContinuation | None:
        expected = queue.remaining[0] if queue.remaining else None
        if expected is None:
            if continuation is not None:
                raise OcrQueueInvalidError("Очередь OCR повреждена")
            return None
        if continuation is None or continuation.expected_candidate != expected:
            raise OcrQueueInvalidError("Очередь OCR повреждена")
        return continuation

    async def _refresh_draft(self, draft: Draft) -> Draft:
        refreshed = await self._session.scalar(
            select(Draft).where(Draft.id == draft.id).execution_options(populate_existing=True)
        )
        if refreshed is None:  # pragma: no cover - the row was just flushed
            raise RuntimeError("Сохранённый черновик не найден")
        return refreshed

    async def _transaction_snapshot(
        self,
        owner_id: UUID,
        transaction_id: UUID,
    ) -> TransactionSnapshot:
        details = await get_transaction_details(self._session, owner_id, transaction_id)
        if details is None:  # pragma: no cover - the row was just flushed
            raise RuntimeError("Сохранённая операция не найдена")
        return TransactionSnapshot(
            transaction_id=details.id,
            kind=TransactionType(details.type),
            amount_minor=details.amount_minor,
            currency=details.currency,
            account_id=details.account_id,
            account_name=details.account_name,
            category_id=details.category_id,
            category_name=details.category_name,
            category_emoji=details.category_emoji,
            occurred_at=details.occurred_at,
            description=details.description,
            source=details.source,
            deleted_at=details.deleted_at,
            version=details.version,
        )

    async def _advance(
        self,
        draft: Draft,
        queue: OcrQueueState,
        continuation: OcrQueueContinuation | None,
        *,
        saved: bool,
        transaction: TransactionSnapshot | None = None,
    ) -> OcrQueueMutationResult:
        _candidate, next_queue, saved_count, skipped_count = queue.consume(saved=saved)
        if next_queue is None:
            await self._session.delete(draft)
            await self._session.flush()
            return OcrQueueMutationResult(
                status=OcrQueueStatus.COMPLETED,
                saved=saved_count,
                skipped=skipped_count,
                transaction=transaction,
            )

        if continuation is None:  # pragma: no cover - validated before mutation
            raise OcrQueueInvalidError("Очередь OCR повреждена")
        payload = dict(continuation.prepared.payload)
        payload["flow"] = "ocr"
        payload["ocr_batch"] = encode_ocr_queue(next_queue)
        draft.state = continuation.prepared.state
        draft.payload = payload
        draft.revision += 1
        draft.suspended = False
        await self._session.flush()
        refreshed = await self._refresh_draft(draft)
        return OcrQueueMutationResult(
            status=OcrQueueStatus.ADVANCED,
            saved=saved_count,
            skipped=skipped_count,
            draft=_draft_snapshot(refreshed),
            transaction=transaction,
        )

    async def confirm_and_continue(
        self,
        command: OcrQueueMutationCommand,
    ) -> OcrQueueMutationResult:
        owner = await self._lock_owner(command.owner_id)
        draft, queue = await self._locked_queue(command)
        continuation = self._validated_continuation(queue, command.continuation)
        reviewed = self._require_review(draft)
        account, category = await self._active_catalogs(command.owner_id, reviewed)

        pending_rule = _pending_rule(draft.payload)
        if pending_rule is not None:
            pattern, scope = pending_rule
            await SqlAlchemyCategoryRuleRepository(self._session).upsert(
                command.owner_id,
                reviewed.kind,
                reviewed.category_id,
                pattern,
                reviewed.account_id if scope == "account" else None,
            )

        try:
            transaction = await save_transaction(
                self._session,
                command.owner_id,
                TransactionDraft(
                    amount_minor=reviewed.amount_minor,
                    type=reviewed.kind,
                    occurred_at=reviewed.occurred_at,
                    category_hint=category.slug,
                    category_explicit=True,
                    account_hint=account.slug,
                    description=reviewed.description,
                ),
                currency=owner.base_currency,
                default_account_id=owner.default_account_id,
            )
        except UnknownAccountError, UnknownCategoryError:
            raise CatalogUnavailableError("Счёт или категория недоступны") from None

        return await self._advance(
            draft,
            queue,
            continuation,
            saved=True,
            transaction=await self._transaction_snapshot(command.owner_id, transaction.id),
        )

    async def skip_and_continue(
        self,
        command: OcrQueueMutationCommand,
    ) -> OcrQueueMutationResult:
        await self._lock_owner(command.owner_id)
        draft, queue = await self._locked_queue(command)
        continuation = self._validated_continuation(queue, command.continuation)
        self._require_review(draft)
        return await self._advance(
            draft,
            queue,
            continuation,
            saved=False,
        )

    async def cancel(self, command: OcrQueueMutationCommand) -> OcrQueueMutationResult:
        if command.continuation is not None:
            raise OcrQueueInvalidError("Очередь OCR повреждена")
        await self._lock_owner(command.owner_id)
        draft, queue = await self._locked_queue(command)
        await self._session.delete(draft)
        await self._session.flush()
        return OcrQueueMutationResult(
            status=OcrQueueStatus.CANCELLED,
            saved=queue.saved,
            skipped=queue.skipped,
        )
