from importlib.util import module_from_spec, spec_from_file_location
from inspect import getsource
from pathlib import Path
from types import ModuleType

import pytest


def _load_migration() -> ModuleType:
    path = Path(__file__).parents[2] / "migrations" / "versions" / "0006_csv_export_outbox_job.py"
    spec = spec_from_file_location("finbot_migration_0006", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load migration 0006")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_csv_export_job_migration_follows_head_and_has_roundtrip() -> None:
    migration = _load_migration()

    assert migration.revision == "0006_csv_export_outbox_job"
    assert migration.down_revision == "0005_channel_neutral_drafts"
    assert callable(migration.upgrade)
    assert callable(migration.downgrade)


def test_upgrade_allows_only_constant_privacy_safe_export_jobs() -> None:
    migration = _load_migration()
    source = getsource(migration.upgrade)

    assert "'send_csv_export'" in source
    assert "text = 'csv_export:v1'" in source
    assert "parse_mode IS NULL" in source
    assert "reply_markup IS NULL" in source
    assert "draft_id IS NULL" in source
    assert "draft_revision IS NULL" in source
    assert "history_page IS NULL" in source
    assert "pending_history_page IS NULL" in source
    for forbidden in ("filename", "content", "amount", "description", "row_count"):
        assert forbidden not in source


def test_downgrade_fails_closed_while_export_jobs_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    mutations: list[str] = []

    class _Bind:
        def scalar(self, statement: object) -> int:
            assert "method = 'send_csv_export'" in str(statement)
            return 1

    class _Op:
        def get_bind(self) -> _Bind:
            return _Bind()

        def drop_constraint(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            mutations.append("drop")

        def create_check_constraint(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            mutations.append("create")

    monkeypatch.setattr(migration, "op", _Op())

    with pytest.raises(RuntimeError, match="durable CSV export jobs exist"):
        migration.downgrade()

    assert mutations == []
