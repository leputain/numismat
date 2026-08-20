from collections.abc import Iterable
from datetime import UTC, datetime
from typing import cast
from uuid import uuid7

import pytest
from sqlalchemy import CheckConstraint, Column, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from finbot.adapters.database import recurring_runner
from finbot.adapters.database.models import Base
from finbot.adapters.database.recurring_runner import (
    _fair_due_schedule_query,
    _fair_stage_owner_query,
    _materialize_schedules_isolated,
    _stage_owners_isolated,
    _StageResult,
)
from finbot.application.recurring import (
    MAX_DUE_SCHEDULES_PER_TICK,
    MAX_STAGE_OWNERS_PER_TICK,
)


def _named_constraint(table: str, name: str) -> object:
    matches = [
        constraint
        for constraint in Base.metadata.tables[table].constraints
        if constraint.name == name
    ]
    assert len(matches) == 1
    return matches[0]


def _column_names(columns: Iterable[Column[object]]) -> tuple[str, ...]:
    return tuple(column.name for column in columns)


def _assert_foreign_key(
    table: str,
    name: str,
    local: tuple[str, ...],
    remote: tuple[str, ...],
) -> None:
    constraint = _named_constraint(table, name)
    assert isinstance(constraint, ForeignKeyConstraint)
    assert _column_names(constraint.columns) == local
    assert tuple(element.target_fullname for element in constraint.elements) == remote
    assert constraint.ondelete is None


def test_orm_enforces_private_chat_and_cross_tenant_reference_integrity() -> None:
    private_chat = _named_constraint("users", "users_private_telegram_chat_check")
    assert isinstance(private_chat, CheckConstraint)
    assert str(private_chat.sqltext) == (
        "telegram_chat_id IS NULL OR telegram_chat_id = telegram_user_id"
    )

    _assert_foreign_key(
        "users",
        "fk_users_default_account_owner",
        ("default_account_id", "id"),
        ("accounts.id", "accounts.user_id"),
    )
    _assert_foreign_key(
        "categories",
        "fk_categories_parent_owner_kind",
        ("parent_id", "user_id", "kind"),
        ("categories.id", "categories.user_id", "categories.kind"),
    )
    _assert_foreign_key(
        "audit_events",
        "fk_audit_events_transaction_owner",
        ("transaction_id", "user_id"),
        ("transactions.id", "transactions.user_id"),
    )
    _assert_foreign_key(
        "recurring_instances",
        "fk_recurring_instances_draft_owner",
        ("draft_id", "user_id"),
        ("drafts.id", "drafts.user_id"),
    )
    _assert_foreign_key(
        "import_rows",
        "fk_import_rows_draft_owner",
        ("draft_id", "user_id"),
        ("drafts.id", "drafts.user_id"),
    )
    _assert_foreign_key(
        "telegram_response_outbox",
        "fk_telegram_response_outbox_user_chat",
        ("owner_telegram_user_id", "chat_id"),
        ("users.telegram_user_id", "users.telegram_chat_id"),
    )


def test_orm_exposes_every_composite_foreign_key_target_as_unique() -> None:
    expected = {
        ("users", "uq_users_telegram_user_chat"): (
            "telegram_user_id",
            "telegram_chat_id",
        ),
        ("transactions", "uq_transactions_id_user_id"): ("id", "user_id"),
        ("drafts", "uq_drafts_id_user_id"): ("id", "user_id"),
    }
    for (table, name), columns in expected.items():
        constraint = _named_constraint(table, name)
        assert isinstance(constraint, UniqueConstraint)
        assert _column_names(constraint.columns) == columns


def test_due_schedule_query_ranks_one_earliest_schedule_per_owner_before_cap() -> None:
    statement = _fair_due_schedule_query(datetime(2026, 8, 20, 12, tzinfo=UTC))
    sql = " ".join(
        str(
            statement.compile(
                dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
                compile_kwargs={"literal_binds": True},
            )
        )
        .lower()
        .split()
    )

    assert "row_number() over (partition by recurring_schedules.user_id" in sql
    assert "order by recurring_schedules.next_due_at, recurring_schedules.id) as owner_rank" in sql
    assert "where ranked_due_schedules.owner_rank = 1" in sql
    assert "order by ranked_due_schedules.next_due_at, ranked_due_schedules.schedule_id" in sql
    assert f"limit {MAX_DUE_SCHEDULES_PER_TICK}" in sql


def test_stage_owner_query_prioritizes_old_backlog_before_fresh_work() -> None:
    statement = _fair_stage_owner_query(datetime(2026, 8, 20, 12, tzinfo=UTC))
    sql = " ".join(
        str(
            statement.compile(
                dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
                compile_kwargs={"literal_binds": True},
            )
        )
        .lower()
        .split()
    )

    assert "group by recurring_instances.user_id" in sql
    assert "order by min(recurring_instances.next_attempt_at), recurring_instances.user_id" in sql
    assert f"limit {MAX_STAGE_OWNERS_PER_TICK}" in sql


class _NestedTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> bool:
        return False


class _NestedSession:
    def __init__(self) -> None:
        self.savepoints = 0

    def begin_nested(self) -> _NestedTransaction:
        self.savepoints += 1
        return _NestedTransaction()


@pytest.mark.asyncio
async def test_recurring_phase_savepoints_isolate_one_tenant_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = uuid7(), uuid7()
    now = datetime(2026, 8, 20, 12, tzinfo=UTC)
    materialize_session = _NestedSession()
    stage_session = _NestedSession()

    async def materialize(
        _session: AsyncSession,
        schedule_id: object,
        _now: datetime,
    ) -> bool:
        if schedule_id == first:
            raise RuntimeError("synthetic isolated failure")
        return True

    async def stage(
        _session: AsyncSession,
        owner_id: object,
        _now: datetime,
    ) -> _StageResult:
        if owner_id == first:
            raise RuntimeError("synthetic isolated failure")
        return _StageResult(staged=1)

    monkeypatch.setattr(recurring_runner, "_materialize_one", materialize)
    monkeypatch.setattr(recurring_runner, "_stage_one", stage)

    materialized, materialize_failures = await _materialize_schedules_isolated(
        cast(AsyncSession, materialize_session),
        (first, second),
        now,
    )
    staged, stage_failures = await _stage_owners_isolated(
        cast(AsyncSession, stage_session),
        (first, second),
        now,
    )

    assert (materialized, materialize_failures) == (1, 1)
    assert (staged, stage_failures) == (_StageResult(staged=1), 1)
    assert materialize_session.savepoints == stage_session.savepoints == 2
