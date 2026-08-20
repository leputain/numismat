from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, cast
from uuid import uuid7

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database.repositories.queries import SqlAlchemyQueryRepository
from finbot.application.dto import TimeSeriesGrain


class _Rows:
    def __iter__(self) -> Any:
        return iter(((date(2026, 8, 1), "RUB", 900, 250, 1, 2),))


class _CapturingSession:
    def __init__(self) -> None:
        self.statement: Any = None

    async def execute(self, statement: Any) -> _Rows:
        self.statement = statement
        return _Rows()


@pytest.mark.asyncio
async def test_timeseries_sql_is_owner_scoped_bounded_and_payload_free() -> None:
    session = _CapturingSession()
    repository = SqlAlchemyQueryRepository(cast(AsyncSession, session))

    result = await repository.timeseries_by_currency(
        uuid7(),
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 8, 2, tzinfo=UTC),
        timezone="Europe/Moscow",
        grain=TimeSeriesGrain.DAY,
        row_limit=11_713,
    )

    compiled = session.statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "date_trunc" in sql and "timezone" in sql
    assert "transactions.user_id" in sql
    assert "transactions.deleted_at IS NULL" in sql
    assert "count(*) FILTER" in sql
    assert "transactions.description" not in sql
    assert "transactions.id" not in sql
    assert 11_713 in compiled.params.values()
    assert result[0].bucket_local_date == date(2026, 8, 1)
    assert result[0].totals.net_minor == 650
    assert (result[0].totals.income_count, result[0].totals.expense_count) == (1, 2)
