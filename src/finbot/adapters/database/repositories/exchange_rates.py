from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid7

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.models import (
    ExchangeRateEntryRecord,
    ExchangeRateSource,
    ExchangeRateVersion,
    User,
)
from finbot.application.errors import (
    ApplicationValidationError,
    EntityNotFoundError,
    InvalidStateError,
    ObjectVersionConflictError,
)
from finbot.application.exchange_rates import (
    MAX_EXCHANGE_RATE_SOURCES,
    MAX_EXCHANGE_RATE_VERSION_PAGE_SIZE,
    ExchangeRateCommandRepository,
    ExchangeRateReader,
    ExchangeRateSourceKind,
    PublishManualRateVersionCommand,
    RateEntrySnapshot,
    RateSourceSnapshot,
    RateVersionCursor,
    RateVersionCursorItem,
    RateVersionSnapshot,
    RateVersionSummary,
)
from finbot.domain.exchange_rates import ExchangeRateEntry, ExchangeRateValue

_MAX_VERSION = 2**31 - 1
_MAX_SOURCE_FETCH = MAX_EXCHANGE_RATE_SOURCES + 1
_MAX_VERSION_FETCH = MAX_EXCHANGE_RATE_VERSION_PAGE_SIZE + 1


def _source_snapshot(source: ExchangeRateSource) -> RateSourceSnapshot:
    return RateSourceSnapshot(
        source_id=source.id,
        owner_id=source.user_id,
        kind=ExchangeRateSourceKind(source.kind),
        target_currency=source.target_currency,
        latest_version=source.latest_version,
    )


def _version_summary(version: ExchangeRateVersion) -> RateVersionSummary:
    return RateVersionSummary(
        rate_version_id=version.id,
        source_id=version.source_id,
        owner_id=version.user_id,
        target_currency=version.target_currency,
        version=version.version,
        effective_at=version.effective_at,
        created_at=version.created_at,
    )


def _entry_snapshot(entry: ExchangeRateEntryRecord) -> RateEntrySnapshot:
    return RateEntrySnapshot(
        rate_version_id=entry.rate_version_id,
        entry=ExchangeRateEntry(
            source_currency=entry.source_currency,
            target_currency=entry.target_currency,
            value=ExchangeRateValue(
                coefficient=entry.coefficient,
                scale=entry.scale,
            ),
            source_minor_digits=entry.source_minor_digits,
            target_minor_digits=entry.target_minor_digits,
        ),
    )


class SqlAlchemyExchangeRateRepository(ExchangeRateReader, ExchangeRateCommandRepository):
    """Immutable manual rate versions with owner-serialized publication."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _lock_owner(self, owner_id: UUID) -> None:
        owner = await self._session.scalar(
            select(User.id).where(User.id == owner_id).with_for_update()
        )
        if owner is None:
            raise EntityNotFoundError("Владелец источника курсов не найден")

    async def get_source(
        self,
        owner_id: UUID,
        source_id: UUID,
    ) -> RateSourceSnapshot | None:
        source = await self._session.scalar(
            select(ExchangeRateSource).where(
                ExchangeRateSource.id == source_id,
                ExchangeRateSource.user_id == owner_id,
            )
        )
        return _source_snapshot(source) if source is not None else None

    async def list_sources_bounded(
        self,
        owner_id: UUID,
        *,
        limit: int,
    ) -> tuple[RateSourceSnapshot, ...]:
        if type(limit) is not int or not 1 <= limit <= _MAX_SOURCE_FETCH:
            raise ValueError("exchange-rate source fetch limit is invalid")
        rows = await self._session.scalars(
            select(ExchangeRateSource)
            .where(ExchangeRateSource.user_id == owner_id)
            .order_by(
                ExchangeRateSource.target_currency,
                ExchangeRateSource.kind,
                ExchangeRateSource.id,
            )
            .limit(limit)
        )
        return tuple(_source_snapshot(source) for source in rows)

    async def list_versions_after(
        self,
        owner_id: UUID,
        source_id: UUID,
        *,
        cursor: RateVersionCursor | None,
        limit: int,
    ) -> tuple[RateVersionCursorItem, ...]:
        if type(limit) is not int or not 1 <= limit <= _MAX_VERSION_FETCH:
            raise ValueError("exchange-rate version fetch limit is invalid")
        statement = select(ExchangeRateVersion).where(
            ExchangeRateVersion.user_id == owner_id,
            ExchangeRateVersion.source_id == source_id,
        )
        if cursor is not None:
            statement = statement.where(
                or_(
                    ExchangeRateVersion.created_at < cursor.created_at,
                    and_(
                        ExchangeRateVersion.created_at == cursor.created_at,
                        ExchangeRateVersion.id < cursor.rate_version_id,
                    ),
                )
            )
        rows = await self._session.scalars(
            statement.order_by(
                ExchangeRateVersion.created_at.desc(),
                ExchangeRateVersion.id.desc(),
            ).limit(limit)
        )
        return tuple(
            RateVersionCursorItem(
                version=_version_summary(version),
                cursor=RateVersionCursor(
                    created_at=version.created_at,
                    rate_version_id=version.id,
                ),
            )
            for version in rows
        )

    async def get_version(
        self,
        owner_id: UUID,
        rate_version_id: UUID,
    ) -> RateVersionSnapshot | None:
        version = await self._session.scalar(
            select(ExchangeRateVersion).where(
                ExchangeRateVersion.id == rate_version_id,
                ExchangeRateVersion.user_id == owner_id,
            )
        )
        if version is None:
            return None
        rows = await self._session.scalars(
            select(ExchangeRateEntryRecord)
            .where(ExchangeRateEntryRecord.rate_version_id == version.id)
            .order_by(ExchangeRateEntryRecord.source_currency)
            .limit(33)
        )
        entries = tuple(_entry_snapshot(entry) for entry in rows)
        return RateVersionSnapshot(summary=_version_summary(version), entries=entries)

    async def publish(
        self,
        command: PublishManualRateVersionCommand,
    ) -> RateVersionSnapshot:
        await self._lock_owner(command.owner_id)
        source = await self._session.scalar(
            select(ExchangeRateSource)
            .where(
                ExchangeRateSource.user_id == command.owner_id,
                ExchangeRateSource.kind == ExchangeRateSourceKind.MANUAL.value,
                ExchangeRateSource.target_currency == command.target_currency,
            )
            .with_for_update()
        )
        now = datetime.now(UTC)
        if source is None:
            if command.expected_source_version != 0:
                raise EntityNotFoundError("Источник курсов не найден")
            await self._ensure_source_capacity(command.owner_id)
            source = ExchangeRateSource(
                id=uuid7(),
                user_id=command.owner_id,
                kind=ExchangeRateSourceKind.MANUAL.value,
                target_currency=command.target_currency,
                latest_version=1,
                created_at=now,
                updated_at=now,
            )
            self._session.add(source)
            next_version = 1
        else:
            if source.latest_version != command.expected_source_version:
                raise ObjectVersionConflictError(current_version=source.latest_version)
            if source.latest_version >= _MAX_VERSION:
                raise InvalidStateError("Достигнут предел версий источника курсов")
            next_version = source.latest_version + 1
            source.latest_version = next_version
            source.updated_at = now

        rate_version = ExchangeRateVersion(
            id=uuid7(),
            source_id=source.id,
            user_id=command.owner_id,
            target_currency=command.target_currency,
            version=next_version,
            effective_at=command.effective_at.astimezone(UTC),
            created_at=now,
        )
        self._session.add(rate_version)
        # These mappers intentionally expose no ORM relationship.  Flush the
        # parent first so the composite entry FK never depends on mapper order.
        await self._session.flush()
        records = tuple(
            ExchangeRateEntryRecord(
                rate_version_id=rate_version.id,
                source_currency=entry.source_currency,
                target_currency=entry.target_currency,
                coefficient=entry.value.coefficient,
                scale=entry.value.scale,
                source_minor_digits=entry.source_minor_digits,
                target_minor_digits=entry.target_minor_digits,
            )
            for entry in command.entries
        )
        self._session.add_all(records)
        await self._session.flush()
        return RateVersionSnapshot(
            summary=_version_summary(rate_version),
            entries=tuple(_entry_snapshot(record) for record in records),
        )

    async def _ensure_source_capacity(self, owner_id: UUID) -> None:
        source_ids = await self._session.scalars(
            select(ExchangeRateSource.id)
            .where(ExchangeRateSource.user_id == owner_id)
            .order_by(ExchangeRateSource.id)
            .limit(MAX_EXCHANGE_RATE_SOURCES)
        )
        if len(source_ids.all()) >= MAX_EXCHANGE_RATE_SOURCES:
            raise ApplicationValidationError("Достигнут предел источников курсов")


__all__ = ["SqlAlchemyExchangeRateRepository"]
