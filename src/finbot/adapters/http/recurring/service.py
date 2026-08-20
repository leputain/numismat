from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from uuid import UUID

from finbot.adapters.database.repositories.http_idempotency import IdempotencyResultKind
from finbot.adapters.http.auth.crypto import HttpSecurityDigester
from finbot.adapters.http.auth.service import SessionAuthenticator, SessionCredentials
from finbot.adapters.http.mutations.ports import MutationUnitOfWork
from finbot.adapters.http.mutations.service import (
    HttpMutationExecutor,
    MutationCredentials,
    MutationOperation,
    MutationReceipt,
)
from finbot.adapters.http.recurring.cursor import RecurringCursorCodec
from finbot.adapters.http.recurring.ports import RecurringQueryUnitOfWorkFactory
from finbot.application.errors import ApplicationValidationError, EntityNotFoundError
from finbot.application.recurring import (
    CreateRecurringScheduleCommand,
    RecurringInstancePageSnapshot,
    RecurringInstanceRef,
    RecurringInstanceSnapshot,
    RecurringSchedulePageSnapshot,
    RecurringScheduleRef,
    RecurringScheduleSnapshot,
    ReplaceRecurringScheduleCommand,
    VersionedRecurringInstanceCommand,
    VersionedRecurringScheduleCommand,
)
from finbot.application.use_cases.recurring import (
    GetRecurringInstance,
    GetRecurringSchedule,
    ListRecurringInstances,
    ListRecurringSchedules,
)
from finbot.domain.recurrence import (
    RecurrenceCadence,
    RecurrenceRule,
    RecurringTransactionDefinition,
)
from finbot.domain.transactions import TransactionType

DEFAULT_RECURRING_LIMIT = 20
_SCHEDULE_CREATED = frozenset({(201, IdempotencyResultKind.RECURRING_SCHEDULE)})
_SCHEDULE_UPDATED = frozenset({(200, IdempotencyResultKind.RECURRING_SCHEDULE)})
_INSTANCE_UPDATED = frozenset({(200, IdempotencyResultKind.RECURRING_INSTANCE)})


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True, repr=False)
class HttpRecurringSchedulePage:
    page: RecurringSchedulePageSnapshot = field(repr=False)
    next_cursor: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class HttpRecurringInstancePage:
    page: RecurringInstancePageSnapshot = field(repr=False)
    next_cursor: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class RecurringDefinitionFields:
    name: str = field(repr=False)
    kind: TransactionType = field(repr=False)
    amount_minor: int = field(repr=False)
    currency: str = field(repr=False)
    account_id: UUID = field(repr=False)
    category_id: UUID = field(repr=False)
    cadence: RecurrenceCadence
    interval: int
    anchor_date: date = field(repr=False)
    local_time: time = field(repr=False)
    ends_on: date | None = field(default=None, repr=False)
    description: str = field(default="", repr=False)

    def definition(self, timezone: str) -> RecurringTransactionDefinition:
        try:
            return RecurringTransactionDefinition(
                name=self.name,
                kind=self.kind,
                amount_minor=self.amount_minor,
                currency=self.currency,
                account_id=self.account_id,
                category_id=self.category_id,
                recurrence=RecurrenceRule(
                    cadence=self.cadence,
                    interval=self.interval,
                    anchor_date=self.anchor_date,
                    local_time=self.local_time,
                    timezone=timezone,
                    ends_on=self.ends_on,
                ),
                description=self.description,
            )
        except (TypeError, ValueError) as exc:
            raise ApplicationValidationError(
                "Параметры регулярного расписания не прошли проверку"
            ) from exc

    def semantics(self) -> dict[str, object]:
        return {
            "account_id": str(self.account_id),
            "amount_minor": str(self.amount_minor),
            "anchor_date": self.anchor_date.isoformat(),
            "cadence": self.cadence.value,
            "category_id": str(self.category_id),
            "currency": self.currency,
            "description": self.description,
            "ends_on": self.ends_on.isoformat() if self.ends_on is not None else None,
            "interval": self.interval,
            "kind": self.kind.value,
            "local_time": self.local_time.isoformat(timespec="minutes"),
            "name": self.name,
        }


class HttpRecurringService:
    """Authenticated recurring reads and owner-locked idempotent mutations."""

    __slots__ = (
        "_authenticator",
        "_clock",
        "_cursor_codec",
        "_mutation_executor",
        "_query_uow_factory",
    )

    def __init__(
        self,
        *,
        digester: HttpSecurityDigester,
        cursor_codec: RecurringCursorCodec,
        query_uow_factory: RecurringQueryUnitOfWorkFactory,
        mutation_executor: HttpMutationExecutor,
        allowed_telegram_user_ids: frozenset[int] | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._authenticator = SessionAuthenticator(digester, allowed_telegram_user_ids)
        self._cursor_codec = cursor_codec
        self._query_uow_factory = query_uow_factory
        self._mutation_executor = mutation_executor
        self._clock = clock

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError("recurring service clock must contain a timezone")
        return value.astimezone(UTC)

    async def list_schedules(
        self,
        credentials: SessionCredentials,
        *,
        deleted: bool,
        limit: int,
        raw_cursor: str | None,
    ) -> HttpRecurringSchedulePage:
        now = self._now()
        async with self._query_uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                credentials,
                now=now,
            )
            owner_id = authenticated.owner.owner_id
            cursor = (
                self._cursor_codec.decode_schedule(owner_id, raw_cursor, deleted=deleted)
                if raw_cursor is not None
                else None
            )
            page = await ListRecurringSchedules(uow.recurring)(
                owner_id,
                deleted=deleted,
                cursor=cursor,
                limit=limit,
            )
            next_cursor = (
                self._cursor_codec.encode_schedule(
                    owner_id,
                    page.next_cursor,
                    deleted=deleted,
                )
                if page.next_cursor is not None
                else None
            )
            return HttpRecurringSchedulePage(page=page, next_cursor=next_cursor)

    async def get_schedule(
        self,
        credentials: SessionCredentials,
        schedule_id: UUID,
    ) -> RecurringScheduleSnapshot:
        now = self._now()
        async with self._query_uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                credentials,
                now=now,
            )
            return await GetRecurringSchedule(uow.recurring)(
                authenticated.owner.owner_id,
                schedule_id,
            )

    async def list_instances(
        self,
        credentials: SessionCredentials,
        schedule_id: UUID,
        *,
        limit: int,
        raw_cursor: str | None,
    ) -> HttpRecurringInstancePage:
        now = self._now()
        async with self._query_uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                credentials,
                now=now,
            )
            owner_id = authenticated.owner.owner_id
            cursor = (
                self._cursor_codec.decode_instance(owner_id, schedule_id, raw_cursor)
                if raw_cursor is not None
                else None
            )
            page = await ListRecurringInstances(uow.recurring)(
                owner_id,
                schedule_id,
                cursor=cursor,
                limit=limit,
            )
            next_cursor = (
                self._cursor_codec.encode_instance(owner_id, schedule_id, page.next_cursor)
                if page.next_cursor is not None
                else None
            )
            return HttpRecurringInstancePage(page=page, next_cursor=next_cursor)

    async def get_instance(
        self,
        credentials: SessionCredentials,
        instance_id: UUID,
    ) -> RecurringInstanceSnapshot:
        now = self._now()
        async with self._query_uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                credentials,
                now=now,
            )
            return await GetRecurringInstance(uow.recurring)(
                authenticated.owner.owner_id,
                instance_id,
            )

    async def create(
        self,
        credentials: MutationCredentials,
        fields: RecurringDefinitionFields,
    ) -> MutationReceipt:
        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            schedule = await uow.recurring.create(
                CreateRecurringScheduleCommand(
                    owner_id,
                    fields.definition(uow.owner_timezone),
                )
            )
            return MutationReceipt(
                201,
                IdempotencyResultKind.RECURRING_SCHEDULE,
                schedule.schedule_id,
                schedule.version,
            )

        return await self._mutation_executor.execute(
            credentials,
            operation=MutationOperation.RECURRING_SCHEDULE_CREATE,
            semantic_request=fields.semantics(),
            allowed_results=_SCHEDULE_CREATED,
            mutate=mutate,
        )

    async def replace(
        self,
        credentials: MutationCredentials,
        schedule_id: UUID,
        version: int,
        fields: RecurringDefinitionFields,
    ) -> MutationReceipt:
        expected = RecurringScheduleRef(schedule_id, version)

        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            current = await uow.recurring.get_schedule(owner_id, schedule_id)
            if current is None:
                raise EntityNotFoundError("Расписание не найдено")
            schedule = await uow.recurring.replace(
                ReplaceRecurringScheduleCommand(
                    owner_id,
                    expected,
                    fields.definition(current.definition.recurrence.timezone),
                )
            )
            return MutationReceipt(
                200,
                IdempotencyResultKind.RECURRING_SCHEDULE,
                schedule.schedule_id,
                schedule.version,
            )

        semantics = fields.semantics()
        semantics.update({"schedule_id": str(schedule_id), "version": version})
        return await self._mutation_executor.execute(
            credentials,
            operation=MutationOperation.RECURRING_SCHEDULE_REPLACE,
            semantic_request=semantics,
            allowed_results=_SCHEDULE_UPDATED,
            mutate=mutate,
        )

    async def schedule_lifecycle(
        self,
        credentials: MutationCredentials,
        schedule_id: UUID,
        version: int,
        operation: MutationOperation,
    ) -> MutationReceipt:
        expected = RecurringScheduleRef(schedule_id, version)

        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            command = VersionedRecurringScheduleCommand(owner_id, expected)
            if operation is MutationOperation.RECURRING_SCHEDULE_PAUSE:
                schedule = await uow.recurring.pause(command)
            elif operation is MutationOperation.RECURRING_SCHEDULE_RESUME:
                schedule = await uow.recurring.resume(command)
            elif operation is MutationOperation.RECURRING_SCHEDULE_DELETE:
                schedule = await uow.recurring.delete(command)
            elif operation is MutationOperation.RECURRING_SCHEDULE_RESTORE:
                schedule = await uow.recurring.restore(command)
            else:  # pragma: no cover - closed route wiring
                raise RuntimeError("unsupported recurring schedule lifecycle operation")
            return MutationReceipt(
                200,
                IdempotencyResultKind.RECURRING_SCHEDULE,
                schedule.schedule_id,
                schedule.version,
            )

        return await self._mutation_executor.execute(
            credentials,
            operation=operation,
            semantic_request={"schedule_id": str(schedule_id), "version": version},
            allowed_results=_SCHEDULE_UPDATED,
            mutate=mutate,
        )

    async def instance_lifecycle(
        self,
        credentials: MutationCredentials,
        instance_id: UUID,
        version: int,
        operation: MutationOperation,
    ) -> MutationReceipt:
        expected = RecurringInstanceRef(instance_id, version)

        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            command = VersionedRecurringInstanceCommand(owner_id, expected)
            if operation is MutationOperation.RECURRING_INSTANCE_SKIP:
                instance = await uow.recurring.skip_instance(command)
            elif operation is MutationOperation.RECURRING_INSTANCE_RETRY:
                instance = await uow.recurring.retry_instance(command)
            else:  # pragma: no cover - closed route wiring
                raise RuntimeError("unsupported recurring instance lifecycle operation")
            return MutationReceipt(
                200,
                IdempotencyResultKind.RECURRING_INSTANCE,
                instance.instance_id,
                instance.version,
            )

        return await self._mutation_executor.execute(
            credentials,
            operation=operation,
            semantic_request={"instance_id": str(instance_id), "version": version},
            allowed_results=_INSTANCE_UPDATED,
            mutate=mutate,
        )


__all__ = [
    "DEFAULT_RECURRING_LIMIT",
    "HttpRecurringInstancePage",
    "HttpRecurringSchedulePage",
    "HttpRecurringService",
    "RecurringDefinitionFields",
]
