from __future__ import annotations

from uuid import UUID

from finbot.application.errors import ApplicationValidationError, EntityNotFoundError
from finbot.application.recurring import (
    MAX_RECURRING_PAGE_SIZE,
    CreateRecurringScheduleCommand,
    RecurringInstanceCursor,
    RecurringInstancePageSnapshot,
    RecurringInstanceSnapshot,
    RecurringReader,
    RecurringRepository,
    RecurringScheduleCursor,
    RecurringSchedulePageSnapshot,
    RecurringScheduleSnapshot,
    ReplaceRecurringScheduleCommand,
    VersionedRecurringInstanceCommand,
    VersionedRecurringScheduleCommand,
)


def _uuid(value: UUID, *, field: str) -> UUID:
    if not isinstance(value, UUID):
        raise ApplicationValidationError(f"{field} не прошёл проверку")
    return value


def _limit(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_RECURRING_PAGE_SIZE
    ):
        raise ApplicationValidationError("Размер страницы должен быть от 1 до 50")
    return value


class RecurringUseCases:
    __slots__ = ("_commands",)

    def __init__(self, commands: RecurringRepository) -> None:
        self._commands = commands

    async def create(
        self,
        command: CreateRecurringScheduleCommand,
    ) -> RecurringScheduleSnapshot:
        return await self._commands.create(command)

    async def replace(
        self,
        command: ReplaceRecurringScheduleCommand,
    ) -> RecurringScheduleSnapshot:
        return await self._commands.replace(command)

    async def pause(
        self,
        command: VersionedRecurringScheduleCommand,
    ) -> RecurringScheduleSnapshot:
        return await self._commands.pause(command)

    async def resume(
        self,
        command: VersionedRecurringScheduleCommand,
    ) -> RecurringScheduleSnapshot:
        return await self._commands.resume(command)

    async def delete(
        self,
        command: VersionedRecurringScheduleCommand,
    ) -> RecurringScheduleSnapshot:
        return await self._commands.delete(command)

    async def restore(
        self,
        command: VersionedRecurringScheduleCommand,
    ) -> RecurringScheduleSnapshot:
        return await self._commands.restore(command)

    async def skip_instance(
        self,
        command: VersionedRecurringInstanceCommand,
    ) -> RecurringInstanceSnapshot:
        return await self._commands.skip_instance(command)

    async def retry_instance(
        self,
        command: VersionedRecurringInstanceCommand,
    ) -> RecurringInstanceSnapshot:
        return await self._commands.retry_instance(command)

    async def get_schedule(
        self,
        owner_id: UUID,
        schedule_id: UUID,
    ) -> RecurringScheduleSnapshot | None:
        """Read the immutable schedule timezone inside the enclosing mutation UoW."""

        return await self._commands.get_schedule(
            _uuid(owner_id, field="Владелец"),
            _uuid(schedule_id, field="Расписание"),
        )


class GetRecurringSchedule:
    __slots__ = ("_reader",)

    def __init__(self, reader: RecurringReader) -> None:
        self._reader = reader

    async def __call__(
        self,
        owner_id: UUID,
        schedule_id: UUID,
    ) -> RecurringScheduleSnapshot:
        result = await self._reader.get_schedule(
            _uuid(owner_id, field="Владелец"),
            _uuid(schedule_id, field="Расписание"),
        )
        if result is None:
            raise EntityNotFoundError("Расписание не найдено")
        return result


class ListRecurringSchedules:
    __slots__ = ("_reader",)

    def __init__(self, reader: RecurringReader) -> None:
        self._reader = reader

    async def __call__(
        self,
        owner_id: UUID,
        *,
        deleted: bool = False,
        cursor: RecurringScheduleCursor | None = None,
        limit: int = 20,
    ) -> RecurringSchedulePageSnapshot:
        if type(deleted) is not bool:
            raise ApplicationValidationError("Признак удаления не прошёл проверку")
        safe_limit = _limit(limit)
        rows = await self._reader.list_schedules_after(
            _uuid(owner_id, field="Владелец"),
            deleted=deleted,
            cursor=cursor,
            limit=safe_limit + 1,
        )
        if len(rows) > safe_limit + 1:
            raise RuntimeError("Recurring reader violated the bounded schedule contract")
        selected = rows[:safe_limit]
        return RecurringSchedulePageSnapshot(
            items=tuple(item.schedule for item in selected),
            next_cursor=selected[-1].cursor if len(rows) > safe_limit else None,
        )


class GetRecurringInstance:
    __slots__ = ("_reader",)

    def __init__(self, reader: RecurringReader) -> None:
        self._reader = reader

    async def __call__(
        self,
        owner_id: UUID,
        instance_id: UUID,
    ) -> RecurringInstanceSnapshot:
        result = await self._reader.get_instance(
            _uuid(owner_id, field="Владелец"),
            _uuid(instance_id, field="Экземпляр расписания"),
        )
        if result is None:
            raise EntityNotFoundError("Экземпляр расписания не найден")
        return result


class ListRecurringInstances:
    __slots__ = ("_reader",)

    def __init__(self, reader: RecurringReader) -> None:
        self._reader = reader

    async def __call__(
        self,
        owner_id: UUID,
        schedule_id: UUID,
        *,
        cursor: RecurringInstanceCursor | None = None,
        limit: int = 20,
    ) -> RecurringInstancePageSnapshot:
        safe_owner = _uuid(owner_id, field="Владелец")
        safe_schedule = _uuid(schedule_id, field="Расписание")
        if await self._reader.get_schedule(safe_owner, safe_schedule) is None:
            raise EntityNotFoundError("Расписание не найдено")
        safe_limit = _limit(limit)
        rows = await self._reader.list_instances_after(
            safe_owner,
            safe_schedule,
            cursor=cursor,
            limit=safe_limit + 1,
        )
        if len(rows) > safe_limit + 1:
            raise RuntimeError("Recurring reader violated the bounded instance contract")
        selected = rows[:safe_limit]
        return RecurringInstancePageSnapshot(
            items=tuple(item.instance for item in selected),
            next_cursor=selected[-1].cursor if len(rows) > safe_limit else None,
        )


__all__ = [
    "GetRecurringInstance",
    "GetRecurringSchedule",
    "ListRecurringInstances",
    "ListRecurringSchedules",
    "RecurringUseCases",
]
