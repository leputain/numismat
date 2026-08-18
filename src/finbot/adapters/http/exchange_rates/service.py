from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from finbot.adapters.database.repositories.http_idempotency import IdempotencyResultKind
from finbot.adapters.http.auth.crypto import HttpSecurityDigester
from finbot.adapters.http.auth.service import SessionAuthenticator
from finbot.adapters.http.exchange_rates.cursor import ExchangeRateCursorCodec
from finbot.adapters.http.exchange_rates.ports import ExchangeRateQueryUnitOfWorkFactory
from finbot.adapters.http.mutations.ports import MutationUnitOfWork
from finbot.adapters.http.mutations.service import (
    HttpMutationExecutor,
    MutationCredentials,
    MutationOperation,
    MutationReceipt,
)
from finbot.application.errors import ApplicationValidationError
from finbot.application.exchange_rates import (
    ConvertedPeriodValuation,
    PublishManualRateVersionCommand,
    RateSourceSnapshot,
    RateVersionPageSnapshot,
    RateVersionSnapshot,
)
from finbot.application.use_cases.exchange_rates import (
    GetConvertedPeriodValuation,
    GetExchangeRateVersion,
    ListExchangeRateSources,
    ListExchangeRateVersions,
)
from finbot.domain.exchange_rates import (
    ExchangeRateEntry,
    parse_exchange_rate,
    validate_exchange_currency,
)

DEFAULT_EXCHANGE_RATE_VERSION_LIMIT = 20
_VERSION_CREATED = frozenset({(201, IdempotencyResultKind.EXCHANGE_RATE_VERSION)})


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True, repr=False)
class ManualRateInput:
    source_currency: str = field(repr=False)
    rate: str = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class ManualRateVersionFields:
    target_currency: str = field(repr=False)
    expected_source_version: int
    effective_at: datetime = field(repr=False)
    entries: tuple[ManualRateInput, ...] = field(repr=False)

    def _normalized(
        self,
    ) -> tuple[str, datetime, tuple[ExchangeRateEntry, ...]]:
        try:
            target = validate_exchange_currency(self.target_currency)
            if self.effective_at.utcoffset() is None:
                raise ValueError("Rate effective timestamp must contain a timezone")
            parsed = tuple(
                ExchangeRateEntry(
                    source_currency=validate_exchange_currency(item.source_currency),
                    target_currency=target,
                    value=parse_exchange_rate(item.rate),
                )
                for item in self.entries
            )
            return target, self.effective_at.astimezone(UTC), parsed
        except (TypeError, ValueError) as exc:
            raise ApplicationValidationError("Параметры версии курсов не прошли проверку") from exc

    def command(self, owner_id: UUID) -> PublishManualRateVersionCommand:
        target, effective_at, entries = self._normalized()
        try:
            return PublishManualRateVersionCommand(
                owner_id=owner_id,
                target_currency=target,
                expected_source_version=self.expected_source_version,
                effective_at=effective_at,
                entries=entries,
            )
        except (TypeError, ValueError) as exc:
            raise ApplicationValidationError("Параметры версии курсов не прошли проверку") from exc

    def semantics(self) -> dict[str, object]:
        target, effective_at, entries = self._normalized()
        return {
            "effective_at": effective_at.isoformat().replace("+00:00", "Z"),
            "entries": [
                {
                    "rate": item.value.canonical,
                    "source_currency": item.source_currency,
                }
                for item in sorted(entries, key=lambda item: item.source_currency)
            ],
            "expected_source_version": self.expected_source_version,
            "target_currency": target,
        }


@dataclass(frozen=True, slots=True, repr=False)
class HttpRateVersionPage:
    page: RateVersionPageSnapshot = field(repr=False)
    next_cursor: str | None = field(default=None, repr=False)


class HttpExchangeRateService:
    """Authenticated version reads, explicit conversion and idempotent publication."""

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
        cursor_codec: ExchangeRateCursorCodec,
        query_uow_factory: ExchangeRateQueryUnitOfWorkFactory,
        mutation_executor: HttpMutationExecutor,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._authenticator = SessionAuthenticator(digester)
        self._cursor_codec = cursor_codec
        self._query_uow_factory = query_uow_factory
        self._mutation_executor = mutation_executor
        self._clock = clock

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError("exchange-rate service clock must contain a timezone")
        return value.astimezone(UTC)

    async def list_sources(
        self,
        raw_session_token: str,
    ) -> tuple[RateSourceSnapshot, ...]:
        now = self._now()
        async with self._query_uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                raw_session_token,
                now=now,
            )
            return await ListExchangeRateSources(uow.rates)(authenticated.owner.owner_id)

    async def list_versions(
        self,
        raw_session_token: str,
        source_id: UUID,
        *,
        limit: int,
        raw_cursor: str | None,
    ) -> HttpRateVersionPage:
        now = self._now()
        async with self._query_uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                raw_session_token,
                now=now,
            )
            owner_id = authenticated.owner.owner_id
            cursor = (
                self._cursor_codec.decode(owner_id, source_id, raw_cursor)
                if raw_cursor is not None
                else None
            )
            page = await ListExchangeRateVersions(uow.rates)(
                owner_id,
                source_id,
                cursor=cursor,
                limit=limit,
            )
            next_cursor = (
                self._cursor_codec.encode(owner_id, source_id, page.next_cursor)
                if page.next_cursor is not None
                else None
            )
            return HttpRateVersionPage(page=page, next_cursor=next_cursor)

    async def get_version(
        self,
        raw_session_token: str,
        version_id: UUID,
    ) -> RateVersionSnapshot:
        now = self._now()
        async with self._query_uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                raw_session_token,
                now=now,
            )
            return await GetExchangeRateVersion(uow.rates)(
                authenticated.owner.owner_id,
                version_id,
            )

    async def converted_period(
        self,
        raw_session_token: str,
        version_id: UUID,
        start: datetime,
        end: datetime,
    ) -> ConvertedPeriodValuation:
        now = self._now()
        async with self._query_uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                raw_session_token,
                now=now,
            )
            return await GetConvertedPeriodValuation(uow.finance, uow.rates)(
                authenticated.owner.owner_id,
                version_id,
                start,
                end,
            )

    async def publish(
        self,
        credentials: MutationCredentials,
        fields: ManualRateVersionFields,
    ) -> MutationReceipt:
        semantics = fields.semantics()

        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            version = await uow.exchange_rates.publish(fields.command(owner_id))
            return MutationReceipt(
                201,
                IdempotencyResultKind.EXCHANGE_RATE_VERSION,
                version.summary.rate_version_id,
                version.summary.version,
            )

        return await self._mutation_executor.execute(
            credentials,
            operation=MutationOperation.EXCHANGE_RATE_VERSION_PUBLISH,
            semantic_request=semantics,
            allowed_results=_VERSION_CREATED,
            mutate=mutate,
        )


__all__ = [
    "DEFAULT_EXCHANGE_RATE_VERSION_LIMIT",
    "HttpExchangeRateService",
    "HttpRateVersionPage",
    "ManualRateInput",
    "ManualRateVersionFields",
]
