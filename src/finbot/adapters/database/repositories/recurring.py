from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid7

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import (
    Account,
    Category,
    RecurringInstance,
    RecurringSchedule,
    Transaction,
    User,
)
from finbot.application.errors import (
    ApplicationValidationError,
    CatalogUnavailableError,
    EntityNotFoundError,
    InvalidStateError,
    ObjectVersionConflictError,
)
from finbot.application.recurring import (
    MAX_ACTIVE_RECURRING_SCHEDULES_PER_OWNER,
    MAX_RECURRING_PAGE_SIZE,
    MAX_RETAINED_RECURRING_SCHEDULES_PER_OWNER,
    CreateRecurringScheduleCommand,
    RecurringCommandRepository,
    RecurringFailureCode,
    RecurringInstanceCursor,
    RecurringInstanceCursorItem,
    RecurringInstanceSnapshot,
    RecurringInstanceStatus,
    RecurringReader,
    RecurringScheduleCursor,
    RecurringScheduleCursorItem,
    RecurringScheduleSnapshot,
    ReplaceRecurringScheduleCommand,
    VersionedRecurringInstanceCommand,
    VersionedRecurringScheduleCommand,
)
from finbot.domain.recurrence import (
    RecurrenceCadence,
    RecurrenceRule,
    RecurringTransactionDefinition,
    recurrence_occurrence,
)
from finbot.domain.transactions import TransactionType

_MAX_FETCH_LIMIT = MAX_RECURRING_PAGE_SIZE + 1
_MAX_VERSION = 2**31 - 1


def _next_version(current: int) -> int:
    if current >= _MAX_VERSION:
        raise InvalidStateError("Достигнут предел версий регулярного объекта")
    return current + 1


def _definition(schedule: RecurringSchedule) -> RecurringTransactionDefinition:
    return RecurringTransactionDefinition(
        name=schedule.name,
        kind=TransactionType(schedule.type),
        amount_minor=schedule.amount_minor,
        currency=schedule.currency,
        account_id=schedule.account_id,
        category_id=schedule.category_id,
        recurrence=RecurrenceRule(
            cadence=RecurrenceCadence(schedule.cadence),
            interval=schedule.interval,
            anchor_date=schedule.anchor_date,
            local_time=schedule.local_time,
            timezone=schedule.timezone,
            ends_on=schedule.ends_on,
        ),
        description=schedule.description,
    )


def _schedule_snapshot(schedule: RecurringSchedule) -> RecurringScheduleSnapshot:
    return RecurringScheduleSnapshot(
        schedule_id=schedule.id,
        owner_id=schedule.user_id,
        definition=_definition(schedule),
        next_occurrence_index=schedule.next_occurrence_index,
        next_due_local=schedule.next_due_local,
        next_due_at=schedule.next_due_at,
        paused_at=schedule.paused_at,
        pause_reason=(
            RecurringFailureCode(schedule.pause_reason)
            if schedule.pause_reason is not None
            else None
        ),
        deleted_at=schedule.deleted_at,
        version=schedule.version,
        created_at=schedule.created_at,
        updated_at=schedule.updated_at,
    )


def _instance_snapshot(
    instance: RecurringInstance,
    transaction_id: UUID | None,
) -> RecurringInstanceSnapshot:
    return RecurringInstanceSnapshot(
        instance_id=instance.id,
        schedule_id=instance.schedule_id,
        owner_id=instance.user_id,
        occurrence_index=instance.occurrence_index,
        nominal_local=instance.nominal_local,
        scheduled_for=instance.scheduled_for,
        timezone=instance.timezone,
        dst_adjusted=instance.dst_adjusted,
        kind=TransactionType(instance.type),
        amount_minor=instance.amount_minor,
        currency=instance.currency,
        account_id=instance.account_id,
        category_id=instance.category_id,
        description=instance.description,
        status=RecurringInstanceStatus(instance.status),
        next_attempt_at=instance.next_attempt_at,
        draft_id=instance.draft_id,
        transaction_id=transaction_id,
        attempt_count=instance.attempt_count,
        failure_code=(
            RecurringFailureCode(instance.failure_code)
            if instance.failure_code is not None
            else None
        ),
        generated_at=instance.generated_at,
        skipped_at=instance.skipped_at,
        created_at=instance.created_at,
        updated_at=instance.updated_at,
        version=instance.version,
    )


class SqlAlchemyRecurringRepository(RecurringCommandRepository, RecurringReader):
    """Owner-scoped schedule commands and bounded recurring reads.

    Commit and rollback remain the enclosing channel's responsibility.  Every
    write locks the owner row before schedule, instance, catalog, or draft rows.
    """

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

    async def _lock_schedule(self, owner_id: UUID, schedule_id: UUID) -> RecurringSchedule:
        schedule = await self._session.scalar(
            select(RecurringSchedule)
            .where(
                RecurringSchedule.id == schedule_id,
                RecurringSchedule.user_id == owner_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if schedule is None:
            raise EntityNotFoundError("Регулярное расписание не найдено")
        return schedule

    async def _lock_instance(self, owner_id: UUID, instance_id: UUID) -> RecurringInstance:
        instance = await self._session.scalar(
            select(RecurringInstance)
            .where(
                RecurringInstance.id == instance_id,
                RecurringInstance.user_id == owner_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if instance is None:
            raise EntityNotFoundError("Экземпляр расписания не найден")
        return instance

    async def _require_catalogs(
        self,
        owner_id: UUID,
        definition: RecurringTransactionDefinition,
    ) -> None:
        account = await self._session.scalar(
            select(Account)
            .where(
                Account.id == definition.account_id,
                Account.user_id == owner_id,
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        category = await self._session.scalar(
            select(Category)
            .where(
                Category.id == definition.category_id,
                Category.user_id == owner_id,
                Category.kind == definition.kind.value,
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if account is None or account.archived_at is not None:
            raise CatalogUnavailableError("Счёт регулярной операции недоступен")
        if category is None or category.archived_at is not None:
            raise CatalogUnavailableError("Категория регулярной операции недоступна")
        if account.currency != definition.currency:
            raise ApplicationValidationError(
                "Валюта регулярной операции должна совпадать с валютой счёта"
            )

    async def _require_schedule_capacity(
        self,
        owner_id: UUID,
        *,
        include_active: bool,
    ) -> None:
        retained = tuple(
            await self._session.scalars(
                select(RecurringSchedule.id)
                .where(RecurringSchedule.user_id == owner_id)
                .limit(MAX_RETAINED_RECURRING_SCHEDULES_PER_OWNER + 1)
            )
        )
        if len(retained) >= MAX_RETAINED_RECURRING_SCHEDULES_PER_OWNER:
            raise InvalidStateError("Достигнут предел сохранённых расписаний")
        if not include_active:
            return
        active = tuple(
            await self._session.scalars(
                select(RecurringSchedule.id)
                .where(
                    RecurringSchedule.user_id == owner_id,
                    RecurringSchedule.deleted_at.is_(None),
                )
                .limit(MAX_ACTIVE_RECURRING_SCHEDULES_PER_OWNER + 1)
            )
        )
        if len(active) >= MAX_ACTIVE_RECURRING_SCHEDULES_PER_OWNER:
            raise InvalidStateError("Достигнут предел активных расписаний")

    @staticmethod
    def _set_next_occurrence(
        schedule: RecurringSchedule,
        definition: RecurringTransactionDefinition,
    ) -> None:
        occurrence = recurrence_occurrence(
            definition.recurrence,
            schedule.next_occurrence_index,
        )
        if occurrence is None:
            schedule.next_due_local = None
            schedule.next_due_at = None
            return
        schedule.next_due_local = occurrence.nominal_local
        schedule.next_due_at = occurrence.scheduled_for

    @staticmethod
    def _require_version(current: int, expected: int) -> None:
        if current != expected:
            raise ObjectVersionConflictError(current_version=current)

    async def create(
        self,
        command: CreateRecurringScheduleCommand,
    ) -> RecurringScheduleSnapshot:
        owner = await self._lock_owner(command.owner_id)
        definition = command.definition
        if definition.recurrence.timezone != owner.timezone:
            raise ApplicationValidationError(
                "Часовой пояс расписания должен совпадать с владельцем"
            )
        await self._require_schedule_capacity(command.owner_id, include_active=True)
        await self._require_catalogs(command.owner_id, definition)
        occurrence = recurrence_occurrence(definition.recurrence, 0)
        if occurrence is None:  # pragma: no cover - rejected by RecurrenceRule
            raise ApplicationValidationError("Расписание не содержит первого occurrence")
        now = datetime.now(UTC)
        schedule = RecurringSchedule(
            id=uuid7(),
            user_id=command.owner_id,
            name=definition.name,
            type=definition.kind.value,
            amount_minor=definition.amount_minor,
            currency=definition.currency,
            account_id=definition.account_id,
            category_id=definition.category_id,
            cadence=definition.recurrence.cadence.value,
            interval=definition.recurrence.interval,
            anchor_date=definition.recurrence.anchor_date,
            local_time=definition.recurrence.local_time,
            timezone=definition.recurrence.timezone,
            ends_on=definition.recurrence.ends_on,
            description=definition.description,
            next_occurrence_index=0,
            next_due_local=occurrence.nominal_local,
            next_due_at=occurrence.scheduled_for,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._session.add(schedule)
        await self._session.flush()
        return _schedule_snapshot(schedule)

    async def replace(
        self,
        command: ReplaceRecurringScheduleCommand,
    ) -> RecurringScheduleSnapshot:
        await self._lock_owner(command.owner_id)
        schedule = await self._lock_schedule(command.owner_id, command.expected.schedule_id)
        self._require_version(schedule.version, command.expected.version)
        if schedule.deleted_at is not None:
            raise InvalidStateError("Удалённое расписание нельзя изменить")
        definition = command.definition
        if definition.recurrence.timezone != schedule.timezone:
            raise ApplicationValidationError("Часовой пояс расписания неизменяем")
        await self._require_catalogs(command.owner_id, definition)
        schedule.name = definition.name
        schedule.type = definition.kind.value
        schedule.amount_minor = definition.amount_minor
        schedule.currency = definition.currency
        schedule.account_id = definition.account_id
        schedule.category_id = definition.category_id
        schedule.cadence = definition.recurrence.cadence.value
        schedule.interval = definition.recurrence.interval
        schedule.anchor_date = definition.recurrence.anchor_date
        schedule.local_time = definition.recurrence.local_time
        schedule.ends_on = definition.recurrence.ends_on
        schedule.description = definition.description
        self._set_next_occurrence(schedule, definition)
        if schedule.pause_reason is not None:
            schedule.paused_at = None
            schedule.pause_reason = None
        schedule.version = _next_version(schedule.version)
        schedule.updated_at = datetime.now(UTC)
        await self._session.flush()
        return _schedule_snapshot(schedule)

    async def pause(
        self,
        command: VersionedRecurringScheduleCommand,
    ) -> RecurringScheduleSnapshot:
        await self._lock_owner(command.owner_id)
        schedule = await self._lock_schedule(command.owner_id, command.expected.schedule_id)
        self._require_version(schedule.version, command.expected.version)
        if schedule.deleted_at is not None:
            raise InvalidStateError("Удалённое расписание нельзя приостановить")
        if schedule.next_due_at is None:
            raise InvalidStateError("Завершённое расписание нельзя приостановить")
        if schedule.paused_at is not None:
            raise InvalidStateError("Расписание уже приостановлено")
        now = datetime.now(UTC)
        schedule.paused_at = now
        schedule.pause_reason = None
        schedule.version = _next_version(schedule.version)
        schedule.updated_at = now
        await self._session.flush()
        return _schedule_snapshot(schedule)

    async def resume(
        self,
        command: VersionedRecurringScheduleCommand,
    ) -> RecurringScheduleSnapshot:
        await self._lock_owner(command.owner_id)
        schedule = await self._lock_schedule(command.owner_id, command.expected.schedule_id)
        self._require_version(schedule.version, command.expected.version)
        if schedule.deleted_at is not None:
            raise InvalidStateError("Удалённое расписание нельзя возобновить")
        if schedule.paused_at is None:
            raise InvalidStateError("Расписание не приостановлено")
        if schedule.next_due_at is None:
            raise InvalidStateError("Расписание уже завершено")
        await self._require_catalogs(command.owner_id, _definition(schedule))
        schedule.paused_at = None
        schedule.pause_reason = None
        schedule.version = _next_version(schedule.version)
        schedule.updated_at = datetime.now(UTC)
        await self._session.flush()
        return _schedule_snapshot(schedule)

    async def delete(
        self,
        command: VersionedRecurringScheduleCommand,
    ) -> RecurringScheduleSnapshot:
        await self._lock_owner(command.owner_id)
        schedule = await self._lock_schedule(command.owner_id, command.expected.schedule_id)
        self._require_version(schedule.version, command.expected.version)
        if schedule.deleted_at is not None:
            raise InvalidStateError("Расписание уже удалено")
        now = datetime.now(UTC)
        schedule.deleted_at = now
        schedule.version = _next_version(schedule.version)
        schedule.updated_at = now
        await self._session.flush()
        return _schedule_snapshot(schedule)

    async def restore(
        self,
        command: VersionedRecurringScheduleCommand,
    ) -> RecurringScheduleSnapshot:
        await self._lock_owner(command.owner_id)
        schedule = await self._lock_schedule(command.owner_id, command.expected.schedule_id)
        self._require_version(schedule.version, command.expected.version)
        if schedule.deleted_at is None:
            raise InvalidStateError("Расписание не удалено")
        active = tuple(
            await self._session.scalars(
                select(RecurringSchedule.id)
                .where(
                    RecurringSchedule.user_id == command.owner_id,
                    RecurringSchedule.deleted_at.is_(None),
                )
                .limit(MAX_ACTIVE_RECURRING_SCHEDULES_PER_OWNER + 1)
            )
        )
        if len(active) >= MAX_ACTIVE_RECURRING_SCHEDULES_PER_OWNER:
            raise InvalidStateError("Достигнут предел активных расписаний")
        await self._require_catalogs(command.owner_id, _definition(schedule))
        schedule.deleted_at = None
        schedule.version = _next_version(schedule.version)
        schedule.updated_at = datetime.now(UTC)
        await self._session.flush()
        return _schedule_snapshot(schedule)

    async def skip_instance(
        self,
        command: VersionedRecurringInstanceCommand,
    ) -> RecurringInstanceSnapshot:
        await self._lock_owner(command.owner_id)
        instance = await self._lock_instance(command.owner_id, command.expected.instance_id)
        self._require_version(instance.version, command.expected.version)
        if instance.status not in {
            RecurringInstanceStatus.PENDING.value,
            RecurringInstanceStatus.BLOCKED.value,
        }:
            raise InvalidStateError("Экземпляр расписания уже обработан")
        now = datetime.now(UTC)
        instance.status = RecurringInstanceStatus.SKIPPED.value
        instance.failure_code = None
        instance.skipped_at = now
        instance.version = _next_version(instance.version)
        instance.updated_at = now
        await self._session.flush()
        return _instance_snapshot(instance, None)

    async def retry_instance(
        self,
        command: VersionedRecurringInstanceCommand,
    ) -> RecurringInstanceSnapshot:
        await self._lock_owner(command.owner_id)
        instance = await self._lock_instance(command.owner_id, command.expected.instance_id)
        self._require_version(instance.version, command.expected.version)
        if instance.status != RecurringInstanceStatus.BLOCKED.value:
            raise InvalidStateError("Повтор доступен только для заблокированного экземпляра")
        schedule = await self._lock_schedule(command.owner_id, instance.schedule_id)
        await self._require_catalogs(command.owner_id, _definition(schedule))
        now = datetime.now(UTC)
        instance.status = RecurringInstanceStatus.PENDING.value
        instance.failure_code = None
        instance.next_attempt_at = now
        instance.version = _next_version(instance.version)
        instance.updated_at = now
        if schedule.pause_reason is not None:
            schedule.pause_reason = None
            schedule.paused_at = None
            schedule.version = _next_version(schedule.version)
            schedule.updated_at = now
        await self._session.flush()
        return _instance_snapshot(instance, None)

    async def get_schedule(
        self,
        owner_id: UUID,
        schedule_id: UUID,
    ) -> RecurringScheduleSnapshot | None:
        schedule = await self._session.scalar(
            select(RecurringSchedule).where(
                RecurringSchedule.id == schedule_id,
                RecurringSchedule.user_id == owner_id,
            )
        )
        return _schedule_snapshot(schedule) if schedule is not None else None

    async def list_schedules_after(
        self,
        owner_id: UUID,
        *,
        deleted: bool,
        cursor: RecurringScheduleCursor | None,
        limit: int,
    ) -> tuple[RecurringScheduleCursorItem, ...]:
        if type(limit) is not int or not 1 <= limit <= _MAX_FETCH_LIMIT:
            raise ValueError("recurring schedule fetch limit is invalid")
        deleted_filter = (
            RecurringSchedule.deleted_at.is_not(None)
            if deleted
            else RecurringSchedule.deleted_at.is_(None)
        )
        statement = select(RecurringSchedule).where(
            RecurringSchedule.user_id == owner_id,
            deleted_filter,
        )
        if cursor is not None:
            statement = statement.where(
                or_(
                    RecurringSchedule.created_at < cursor.created_at,
                    and_(
                        RecurringSchedule.created_at == cursor.created_at,
                        RecurringSchedule.id < cursor.schedule_id,
                    ),
                )
            )
        rows = await self._session.scalars(
            statement.order_by(
                RecurringSchedule.created_at.desc(),
                RecurringSchedule.id.desc(),
            ).limit(limit)
        )
        return tuple(
            RecurringScheduleCursorItem(
                schedule=_schedule_snapshot(schedule),
                cursor=RecurringScheduleCursor(schedule.created_at, schedule.id),
            )
            for schedule in rows
        )

    async def get_instance(
        self,
        owner_id: UUID,
        instance_id: UUID,
    ) -> RecurringInstanceSnapshot | None:
        row = (
            await self._session.execute(
                select(RecurringInstance, Transaction.id)
                .outerjoin(
                    Transaction,
                    Transaction.recurring_instance_id == RecurringInstance.id,
                )
                .where(
                    RecurringInstance.id == instance_id,
                    RecurringInstance.user_id == owner_id,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        instance, transaction_id = row._t
        return _instance_snapshot(instance, transaction_id)

    async def list_instances_after(
        self,
        owner_id: UUID,
        schedule_id: UUID,
        *,
        cursor: RecurringInstanceCursor | None,
        limit: int,
    ) -> tuple[RecurringInstanceCursorItem, ...]:
        if type(limit) is not int or not 1 <= limit <= _MAX_FETCH_LIMIT:
            raise ValueError("recurring instance fetch limit is invalid")
        statement = (
            select(RecurringInstance, Transaction.id)
            .outerjoin(
                Transaction,
                Transaction.recurring_instance_id == RecurringInstance.id,
            )
            .where(
                RecurringInstance.user_id == owner_id,
                RecurringInstance.schedule_id == schedule_id,
            )
        )
        if cursor is not None:
            statement = statement.where(
                or_(
                    RecurringInstance.scheduled_for < cursor.scheduled_for,
                    and_(
                        RecurringInstance.scheduled_for == cursor.scheduled_for,
                        RecurringInstance.id < cursor.instance_id,
                    ),
                )
            )
        rows = await self._session.execute(
            statement.order_by(
                RecurringInstance.scheduled_for.desc(),
                RecurringInstance.id.desc(),
            ).limit(limit)
        )
        return tuple(
            RecurringInstanceCursorItem(
                instance=_instance_snapshot(instance, transaction_id),
                cursor=RecurringInstanceCursor(instance.scheduled_for, instance.id),
            )
            for instance, transaction_id in rows
        )

    async def pending_count(self, owner_id: UUID) -> int:
        """Bounded runner helper; not part of the application reader contract."""

        rows = await self._session.scalars(
            select(RecurringInstance.id)
            .where(
                RecurringInstance.user_id == owner_id,
                RecurringInstance.status == RecurringInstanceStatus.PENDING.value,
            )
            .limit(33)
        )
        return len(tuple(rows))


__all__ = ["SqlAlchemyRecurringRepository"]
