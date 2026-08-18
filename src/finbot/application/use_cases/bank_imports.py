from __future__ import annotations

from datetime import datetime
from uuid import UUID

from finbot.application.bank_imports import (
    MAX_BANK_IMPORT_PAGE_SIZE,
    BankImportBatchCursor,
    BankImportBatchPage,
    BankImportBatchSnapshot,
    BankImportBatchState,
    BankImportCommandRepository,
    BankImportDigester,
    BankImportDraftResult,
    BankImportFailureReason,
    BankImportField,
    BankImportMutationResult,
    BankImportParser,
    BankImportReader,
    BankImportRequestDigest,
    BankImportRowCursor,
    BankImportRowPage,
    BankImportRowSnapshot,
    BankImportRowState,
    BankImportValidationError,
    CancelBankImportBatchCommand,
    CreateBankImportCommand,
    KeyedBankImportDigest,
    LinkBankImportRowCommand,
    ReconciliationCandidate,
    StageBankImportBatchCommand,
    StagedBankImportRow,
    VersionedBankImportRowCommand,
)
from finbot.application.errors import (
    ApplicationValidationError,
    EntityNotFoundError,
    InvalidStateError,
)
from finbot.domain.bank_imports import (
    MAX_RECONCILIATION_CANDIDATES,
    reconciliation_bounds,
    reconciliation_rank,
)


def _uuid(value: UUID, *, field_name: str) -> UUID:
    if not isinstance(value, UUID):
        raise ApplicationValidationError(f"{field_name} не прошёл проверку")
    return value


def _page_limit(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_BANK_IMPORT_PAGE_SIZE
    ):
        raise ApplicationValidationError("Размер страницы импорта должен быть от 1 до 50")
    return value


class BankImportPreparer:
    """Bounded parse and HMAC work that must run before any database UoW."""

    __slots__ = ("_digester", "_parser")

    def __init__(
        self,
        parser: BankImportParser,
        digester: BankImportDigester,
    ) -> None:
        self._parser = parser
        self._digester = digester

    def prepare(self, command: CreateBankImportCommand) -> StageBankImportBatchCommand:
        """Parse and digest outside a database transaction; return no raw references."""

        parsed = self._parser.parse(
            command.content,
            profile=command.profile,
            encoding=command.encoding,
        )
        if parsed.profile is not command.profile:
            raise RuntimeError("Bank import parser returned another profile")
        expected_account_reference = parsed.rows[0].source_account_reference
        for row_number, row in enumerate(parsed.rows, start=2):
            if row.source_account_reference != expected_account_reference:
                raise BankImportValidationError(
                    BankImportFailureReason.INVALID_FIELD,
                    row_number=row_number,
                    field=BankImportField.ACCOUNT_REFERENCE,
                )
        staged: list[StagedBankImportRow] = []
        for position, row in enumerate(parsed.rows, start=1):
            fingerprint = self._digester.fingerprint(row)
            reference_digest = (
                self._digester.reference(row.source_reference)
                if row.source_reference is not None
                else None
            )
            if not isinstance(fingerprint, KeyedBankImportDigest) or (
                reference_digest is not None
                and not isinstance(reference_digest, KeyedBankImportDigest)
            ):
                raise RuntimeError("Bank import digester violated its typed contract")
            staged.append(
                StagedBankImportRow(
                    position=position,
                    occurred_at=row.occurred_at,
                    kind=row.kind,
                    amount_minor=row.amount_minor,
                    currency=row.currency,
                    description=row.description,
                    fingerprint=fingerprint,
                    reference_digest=reference_digest,
                )
            )
        return StageBankImportBatchCommand(
            owner_id=command.owner_id,
            account_id=command.account_id,
            expected_account_version=command.expected_account_version,
            profile=parsed.profile,
            encoding=parsed.encoding,
            rows=tuple(staged),
        )

    def request_digest(
        self,
        command: StageBankImportBatchCommand,
    ) -> BankImportRequestDigest:
        """Return the only import-payload token allowed in HTTP mutation semantics."""

        digest = self._digester.request_digest(command)
        if not isinstance(digest, BankImportRequestDigest):
            raise RuntimeError("Bank import digester violated its request-digest contract")
        return digest


class BankImportUseCases:
    """Prepared staged-import commands inside an adapter-owned UoW."""

    __slots__ = ("_commands",)

    def __init__(self, commands: BankImportCommandRepository) -> None:
        self._commands = commands

    async def create_prepared(
        self,
        command: StageBankImportBatchCommand,
    ) -> BankImportBatchSnapshot:
        return await self._commands.create_batch(command)

    async def stage_draft(
        self,
        command: VersionedBankImportRowCommand,
    ) -> BankImportDraftResult:
        return await self._commands.stage_draft(command)

    async def link(
        self,
        command: LinkBankImportRowCommand,
    ) -> BankImportMutationResult:
        return await self._commands.link(command)

    async def skip(
        self,
        command: VersionedBankImportRowCommand,
    ) -> BankImportMutationResult:
        return await self._commands.skip(command)

    async def cancel_batch(
        self,
        command: CancelBankImportBatchCommand,
    ) -> BankImportBatchSnapshot:
        return await self._commands.cancel_batch(command)


class GetBankImportBatch:
    __slots__ = ("_reader",)

    def __init__(self, reader: BankImportReader) -> None:
        self._reader = reader

    async def __call__(
        self,
        owner_id: UUID,
        batch_id: UUID,
    ) -> BankImportBatchSnapshot:
        result = await self._reader.get_batch(
            _uuid(owner_id, field_name="Владелец"),
            _uuid(batch_id, field_name="Пакет импорта"),
        )
        if result is None:
            raise EntityNotFoundError("Пакет импорта не найден")
        return result


class ListBankImportBatches:
    __slots__ = ("_reader",)

    def __init__(self, reader: BankImportReader) -> None:
        self._reader = reader

    async def __call__(
        self,
        owner_id: UUID,
        *,
        state: BankImportBatchState | None = None,
        cursor: BankImportBatchCursor | None = None,
        limit: int = 20,
    ) -> BankImportBatchPage:
        if state is not None and not isinstance(state, BankImportBatchState):
            raise ApplicationValidationError("Состояние пакета импорта не прошло проверку")
        if cursor is not None and not isinstance(cursor, BankImportBatchCursor):
            raise ApplicationValidationError("Курсор пакетов импорта не прошёл проверку")
        safe_limit = _page_limit(limit)
        rows = await self._reader.list_batches_after(
            _uuid(owner_id, field_name="Владелец"),
            state=state,
            cursor=cursor,
            limit=safe_limit + 1,
        )
        if len(rows) > safe_limit + 1:
            raise RuntimeError("Bank import reader violated the bounded batch contract")
        selected = rows[:safe_limit]
        return BankImportBatchPage(
            items=tuple(item.batch for item in selected),
            next_cursor=selected[-1].cursor if len(rows) > safe_limit else None,
        )


class GetBankImportRow:
    __slots__ = ("_reader",)

    def __init__(self, reader: BankImportReader) -> None:
        self._reader = reader

    async def __call__(
        self,
        owner_id: UUID,
        batch_id: UUID,
        row_id: UUID,
    ) -> BankImportRowSnapshot:
        result = await self._reader.get_row(
            _uuid(owner_id, field_name="Владелец"),
            _uuid(batch_id, field_name="Пакет импорта"),
            _uuid(row_id, field_name="Строка импорта"),
        )
        if result is None:
            raise EntityNotFoundError("Строка импорта не найдена")
        return result


class ListBankImportRows:
    __slots__ = ("_reader",)

    def __init__(self, reader: BankImportReader) -> None:
        self._reader = reader

    async def __call__(
        self,
        owner_id: UUID,
        batch_id: UUID,
        *,
        state: BankImportRowState | None = None,
        cursor: BankImportRowCursor | None = None,
        limit: int = 20,
    ) -> BankImportRowPage:
        safe_owner = _uuid(owner_id, field_name="Владелец")
        safe_batch = _uuid(batch_id, field_name="Пакет импорта")
        if state is not None and not isinstance(state, BankImportRowState):
            raise ApplicationValidationError("Состояние строки импорта не прошло проверку")
        if cursor is not None and not isinstance(cursor, BankImportRowCursor):
            raise ApplicationValidationError("Курсор строк импорта не прошёл проверку")
        if await self._reader.get_batch(safe_owner, safe_batch) is None:
            raise EntityNotFoundError("Пакет импорта не найден")
        safe_limit = _page_limit(limit)
        rows = await self._reader.list_rows_after(
            safe_owner,
            safe_batch,
            state=state,
            cursor=cursor,
            limit=safe_limit + 1,
        )
        if len(rows) > safe_limit + 1:
            raise RuntimeError("Bank import reader violated the bounded row contract")
        selected = rows[:safe_limit]
        return BankImportRowPage(
            items=tuple(item.row for item in selected),
            next_cursor=selected[-1].cursor if len(rows) > safe_limit else None,
        )


class ListReconciliationCandidates:
    """Return at most five exact-filtered, deterministically ranked suggestions."""

    __slots__ = ("_reader",)

    def __init__(self, reader: BankImportReader) -> None:
        self._reader = reader

    async def __call__(
        self,
        owner_id: UUID,
        batch_id: UUID,
        row_id: UUID,
    ) -> tuple[ReconciliationCandidate, ...]:
        safe_owner = _uuid(owner_id, field_name="Владелец")
        safe_batch = _uuid(batch_id, field_name="Пакет импорта")
        safe_row = _uuid(row_id, field_name="Строка импорта")
        batch = await self._reader.get_batch(safe_owner, safe_batch)
        row = await self._reader.get_row(safe_owner, safe_batch, safe_row)
        if batch is None or row is None:
            raise EntityNotFoundError("Строка импорта не найдена")
        if (
            batch.owner_id != safe_owner
            or row.owner_id != safe_owner
            or row.batch_id != batch.batch_id
        ):
            raise RuntimeError("Bank import reader returned inconsistent ownership")
        if batch.state is not BankImportBatchState.OPEN:
            raise InvalidStateError("Пакет импорта уже закрыт")
        if row.state is BankImportRowState.STAGED and row.draft_id is not None:
            raise InvalidStateError("Строка импорта уже ожидает проверки")
        if row.state not in {BankImportRowState.PENDING, BankImportRowState.STAGED}:
            raise InvalidStateError("Строка импорта уже обработана")

        transactions = await self._reader.list_candidate_transactions(
            safe_owner,
            safe_row,
            limit=MAX_RECONCILIATION_CANDIDATES + 1,
        )
        if len(transactions) > MAX_RECONCILIATION_CANDIDATES + 1:
            raise RuntimeError("Bank import reader violated the candidate bound")
        if len({item.transaction_id for item in transactions}) != len(transactions):
            raise RuntimeError("Bank import reader returned duplicate candidates")

        start, end = reconciliation_bounds(row.occurred_at)
        for transaction in transactions:
            if (
                transaction.source != "manual"
                or transaction.deleted_at is not None
                or transaction.account_id != batch.account_id
                or transaction.kind is not row.kind
                or transaction.amount_minor != row.amount_minor
                or transaction.currency != row.currency
                or not isinstance(transaction.occurred_at, datetime)
                or transaction.occurred_at.utcoffset() is None
                or not start <= transaction.occurred_at <= end
            ):
                raise RuntimeError("Bank import reader returned an inexact candidate")

        ordered = sorted(
            transactions,
            key=lambda item: reconciliation_rank(
                row.occurred_at,
                item.occurred_at,
                item.transaction_id,
            ),
        )[:MAX_RECONCILIATION_CANDIDATES]
        return tuple(
            ReconciliationCandidate(
                transaction=transaction,
                distance_microseconds=reconciliation_rank(
                    row.occurred_at,
                    transaction.occurred_at,
                    transaction.transaction_id,
                )[0],
                rank=index,
            )
            for index, transaction in enumerate(ordered, start=1)
        )


__all__ = [
    "BankImportPreparer",
    "BankImportUseCases",
    "GetBankImportBatch",
    "GetBankImportRow",
    "ListBankImportBatches",
    "ListBankImportRows",
    "ListReconciliationCandidates",
]
