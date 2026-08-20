from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from finbot.adapters.database.repositories.http_idempotency import IdempotencyResultKind
from finbot.adapters.http.auth.crypto import HttpSecurityDigester
from finbot.adapters.http.auth.service import SessionAuthenticator, SessionCredentials
from finbot.adapters.http.budgets.cursor import BudgetCursorCodec
from finbot.adapters.http.budgets.ports import BudgetQueryUnitOfWorkFactory
from finbot.adapters.http.mutations.ports import MutationUnitOfWork
from finbot.adapters.http.mutations.service import (
    HttpMutationExecutor,
    MutationCredentials,
    MutationOperation,
    MutationReceipt,
)
from finbot.application.budgets import (
    BudgetPageSnapshot,
    BudgetProgressSnapshot,
    BudgetRef,
    CreateBudgetCommand,
    ReplaceBudgetCommand,
    VersionedBudgetCommand,
)
from finbot.application.errors import ApplicationValidationError
from finbot.application.use_cases.budgets import (
    GetBudgetProgress,
    ListBudgetProgress,
)
from finbot.domain.budgets import (
    BudgetDefinition,
    normalize_budget_name,
    validate_budget_currency,
    validate_budget_period,
)
from finbot.domain.money import validate_minor

DEFAULT_BUDGET_LIMIT = 20
_BUDGET_CREATED = frozenset({(201, IdempotencyResultKind.BUDGET)})
_BUDGET_UPDATED = frozenset({(200, IdempotencyResultKind.BUDGET)})


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _month_window(now: datetime, timezone: str) -> tuple[date, date]:
    local = now.astimezone(ZoneInfo(timezone)).date()
    start = local.replace(day=1)
    if start.month == 12:
        next_month = date(start.year + 1, 1, 1)
    else:
        next_month = date(start.year, start.month + 1, 1)
    return start, next_month - timedelta(days=1)


def _validated_fields(
    *,
    name: str,
    limit_minor: int,
    currency: str,
    category_id: UUID | None,
    starts_on: date,
    ends_on: date,
) -> tuple[str, int, str, UUID | None, date, date]:
    try:
        normalized_name = normalize_budget_name(name)
        validate_minor(limit_minor)
        validate_budget_currency(currency)
        validate_budget_period(starts_on, ends_on)
        if category_id is not None and not isinstance(category_id, UUID):
            raise ValueError("Категория бюджета не прошла проверку")
    except ValueError as exc:
        raise ApplicationValidationError("Параметры бюджета не прошли проверку") from exc
    return normalized_name, limit_minor, currency, category_id, starts_on, ends_on


def _definition(
    fields: tuple[str, int, str, UUID | None, date, date],
    timezone: str,
) -> BudgetDefinition:
    name, limit_minor, currency, category_id, starts_on, ends_on = fields
    return BudgetDefinition(
        name=name,
        limit_minor=limit_minor,
        currency=currency,
        category_id=category_id,
        starts_on=starts_on,
        ends_on=ends_on,
        timezone=timezone,
    )


@dataclass(frozen=True, slots=True, repr=False)
class HttpBudgetPage:
    page: BudgetPageSnapshot = field(repr=False)
    window_start: date = field(repr=False)
    window_end: date = field(repr=False)
    measured_at: datetime = field(repr=False)
    next_cursor: str | None = field(default=None, repr=False)


class HttpBudgetService:
    """Authenticated shared budget queries and idempotent versioned mutations."""

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
        cursor_codec: BudgetCursorCodec,
        query_uow_factory: BudgetQueryUnitOfWorkFactory,
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
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("budget clock must be timezone-aware")
        return value.astimezone(UTC)

    async def list(
        self,
        credentials: SessionCredentials,
        *,
        window_start: date | None,
        window_end: date | None,
        deleted: bool,
        limit: int,
        raw_cursor: str | None,
    ) -> HttpBudgetPage:
        now = self._now()
        async with self._query_uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                credentials,
                now=now,
            )
            if (window_start is None) != (window_end is None):
                raise ApplicationValidationError("Границы периода бюджета задаются вместе")
            if window_start is None or window_end is None:
                start, end = _month_window(now, authenticated.owner.timezone)
            else:
                start, end = window_start, window_end
            try:
                validate_budget_period(start, end)
            except ValueError as exc:
                raise ApplicationValidationError("Период бюджетов не прошёл проверку") from exc
            owner_id = authenticated.owner.owner_id
            cursor = (
                self._cursor_codec.decode(
                    owner_id,
                    raw_cursor,
                    window_start=start,
                    window_end=end,
                    deleted=deleted,
                )
                if raw_cursor is not None
                else None
            )
            page = await ListBudgetProgress(uow.budgets)(
                owner_id,
                window_start=start,
                window_end=end,
                deleted=deleted,
                cursor=cursor,
                limit=limit,
                as_of=now,
            )
            next_cursor = (
                self._cursor_codec.encode(
                    owner_id,
                    page.next_cursor,
                    window_start=start,
                    window_end=end,
                    deleted=deleted,
                )
                if page.next_cursor is not None
                else None
            )
            return HttpBudgetPage(
                page=page,
                window_start=start,
                window_end=end,
                measured_at=now,
                next_cursor=next_cursor,
            )

    async def get(
        self,
        credentials: SessionCredentials,
        budget_id: UUID,
    ) -> BudgetProgressSnapshot:
        now = self._now()
        async with self._query_uow_factory() as uow:
            authenticated = await self._authenticator.authenticate_read(
                uow.auth,
                credentials,
                now=now,
            )
            return await GetBudgetProgress(uow.budgets)(
                authenticated.owner.owner_id,
                budget_id,
                as_of=now,
            )

    async def create(
        self,
        credentials: MutationCredentials,
        *,
        name: str,
        limit_minor: int,
        currency: str,
        category_id: UUID | None,
        starts_on: date,
        ends_on: date,
    ) -> MutationReceipt:
        fields = _validated_fields(
            name=name,
            limit_minor=limit_minor,
            currency=currency,
            category_id=category_id,
            starts_on=starts_on,
            ends_on=ends_on,
        )

        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            definition = _definition(fields, uow.owner_timezone)
            budget = await uow.budgets.create(CreateBudgetCommand(owner_id, definition))
            return MutationReceipt(
                http_status=201,
                kind=IdempotencyResultKind.BUDGET,
                result_id=budget.budget_id,
                revision=budget.version,
            )

        return await self._mutation_executor.execute(
            credentials,
            operation=MutationOperation.BUDGET_CREATE,
            semantic_request=_field_semantics(fields),
            allowed_results=_BUDGET_CREATED,
            mutate=mutate,
        )

    async def replace(
        self,
        credentials: MutationCredentials,
        budget_id: UUID,
        *,
        version: int,
        name: str,
        limit_minor: int,
        currency: str,
        category_id: UUID | None,
        starts_on: date,
        ends_on: date,
    ) -> MutationReceipt:
        expected = BudgetRef(budget_id, version)
        fields = _validated_fields(
            name=name,
            limit_minor=limit_minor,
            currency=currency,
            category_id=category_id,
            starts_on=starts_on,
            ends_on=ends_on,
        )

        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            current = await uow.budgets.get(owner_id, budget_id)
            definition = _definition(fields, current.definition.timezone)
            budget = await uow.budgets.replace(ReplaceBudgetCommand(owner_id, expected, definition))
            return MutationReceipt(
                http_status=200,
                kind=IdempotencyResultKind.BUDGET,
                result_id=budget.budget_id,
                revision=budget.version,
            )

        semantics = _field_semantics(fields)
        semantics.update({"budget_id": str(budget_id), "version": version})
        return await self._mutation_executor.execute(
            credentials,
            operation=MutationOperation.BUDGET_REPLACE,
            semantic_request=semantics,
            allowed_results=_BUDGET_UPDATED,
            mutate=mutate,
        )

    async def delete(
        self,
        credentials: MutationCredentials,
        budget_id: UUID,
        version: int,
    ) -> MutationReceipt:
        return await self._lifecycle(credentials, budget_id, version, restore=False)

    async def restore(
        self,
        credentials: MutationCredentials,
        budget_id: UUID,
        version: int,
    ) -> MutationReceipt:
        return await self._lifecycle(credentials, budget_id, version, restore=True)

    async def _lifecycle(
        self,
        credentials: MutationCredentials,
        budget_id: UUID,
        version: int,
        *,
        restore: bool,
    ) -> MutationReceipt:
        expected = BudgetRef(budget_id, version)

        async def mutate(uow: MutationUnitOfWork, owner_id: UUID) -> MutationReceipt:
            command = VersionedBudgetCommand(owner_id, expected)
            budget = (
                await uow.budgets.restore(command) if restore else await uow.budgets.delete(command)
            )
            return MutationReceipt(
                http_status=200,
                kind=IdempotencyResultKind.BUDGET,
                result_id=budget.budget_id,
                revision=budget.version,
            )

        return await self._mutation_executor.execute(
            credentials,
            operation=(
                MutationOperation.BUDGET_RESTORE if restore else MutationOperation.BUDGET_DELETE
            ),
            semantic_request={"budget_id": str(budget_id), "version": version},
            allowed_results=_BUDGET_UPDATED,
            mutate=mutate,
        )


def _field_semantics(
    fields: tuple[str, int, str, UUID | None, date, date],
) -> dict[str, object]:
    name, limit_minor, currency, category_id, starts_on, ends_on = fields
    return {
        "category_id": str(category_id) if category_id is not None else None,
        "currency": currency,
        "ends_on": ends_on.isoformat(),
        "limit_minor": str(limit_minor),
        "name": name,
        "starts_on": starts_on.isoformat(),
    }


__all__ = ["DEFAULT_BUDGET_LIMIT", "HttpBudgetPage", "HttpBudgetService"]
