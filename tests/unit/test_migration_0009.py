from importlib.util import module_from_spec, spec_from_file_location
from inspect import getsource
from pathlib import Path
from types import ModuleType

import pytest


def _load_migration() -> ModuleType:
    path = Path(__file__).parents[2] / "migrations" / "versions" / "0009_recurring_transactions.py"
    spec = spec_from_file_location("finbot_migration_0009", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load migration 0009")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_recurring_migration_follows_budget_head_and_has_roundtrip() -> None:
    migration = _load_migration()

    assert migration.revision == "0009_recurring_transactions"
    assert migration.down_revision == "0008_budgets"
    assert callable(migration.upgrade)
    assert callable(migration.downgrade)


def test_upgrade_enforces_due_uniqueness_owner_provenance_and_review_receipts() -> None:
    source = getsource(_load_migration().upgrade)

    for required in (
        '"recurring_schedules"',
        '"recurring_instances"',
        "uq_recurring_instances_schedule_occurrence",
        "fk_recurring_instances_schedule_owner",
        "fk_transactions_recurring_instance_owner",
        "uq_transactions_recurring_instance_id",
        "transactions_recurring_source_check",
        "recurring_schedule",
        "recurring_instance",
        "ix_recurring_schedules_due_active",
        "ix_recurring_instances_pending_due",
    ):
        assert required in source


def test_downgrade_fails_closed_before_ddl_while_recurring_state_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    events: list[str] = []

    class _Bind:
        def scalar(self, statement: object) -> bool:
            rendered = str(statement)
            assert "recurring_schedules" in rendered
            assert "recurring_instance_id IS NOT NULL" in rendered
            events.append("guard")
            return True

    class _Op:
        class _Context:
            as_sql = False

        def get_context(self) -> _Context:
            return self._Context()

        def get_bind(self) -> _Bind:
            return _Bind()

        def execute(self, statement: object) -> None:
            assert "ACCESS EXCLUSIVE" in str(statement)
            events.append("lock")

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"downgrade DDL must not run: {name}")

    monkeypatch.setattr(migration, "op", _Op())

    with pytest.raises(RuntimeError, match="recurring schedules"):
        migration.downgrade()

    assert events == ["lock", "guard"]
