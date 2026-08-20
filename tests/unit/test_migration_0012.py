from importlib.util import module_from_spec, spec_from_file_location
from inspect import getsource
from pathlib import Path
from types import ModuleType

import pytest


def _load_migration() -> ModuleType:
    path = Path(__file__).parents[2] / "migrations" / "versions" / "0012_multitenant_integrity.py"
    spec = spec_from_file_location("finbot_migration_0012", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load migration 0012")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_multitenant_integrity_migration_follows_bank_import_head() -> None:
    migration = _load_migration()

    assert migration.revision == "0012_multitenant_integrity"
    assert migration.down_revision == "0011_bank_imports"
    assert callable(migration.upgrade)
    assert callable(migration.downgrade)


def test_upgrade_guards_and_installs_every_owner_constraint_without_set_null() -> None:
    migration = _load_migration()
    guard_sql = str(migration._INTEGRITY_GUARD)
    source = getsource(migration.upgrade)

    for required in (
        "telegram_chat_id <> app_user.telegram_user_id",
        "account.user_id = app_user.id",
        "parent.user_id = category.user_id",
        "txn.user_id = event.user_id",
        "draft.user_id = instance.user_id",
        "draft.user_id = import_row.user_id",
        "app_user.telegram_user_id = response.owner_telegram_user_id",
        "app_user.telegram_chat_id = response.chat_id",
    ):
        assert required in guard_sql

    for required in (
        "users_private_telegram_chat_check",
        "fk_users_default_account_owner",
        "fk_categories_parent_owner_kind",
        "fk_audit_events_transaction_owner",
        "fk_recurring_instances_draft_owner",
        "fk_import_rows_draft_owner",
        "fk_telegram_response_outbox_user_chat",
        "uq_transactions_id_user_id",
        "uq_drafts_id_user_id",
    ):
        assert required in source

    assert "ondelete=" not in source


def test_upgrade_fails_before_ddl_when_existing_data_is_unsafe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    events: list[str] = []

    class _Bind:
        def scalar(self, statement: object) -> bool:
            assert "telegram_response_outbox" in str(statement)
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
            raise AssertionError(f"upgrade DDL must not run: {name}")

    monkeypatch.setattr(migration, "op", _Op())

    with pytest.raises(RuntimeError, match="cross-owner or non-private"):
        migration.upgrade()

    assert events == ["lock", "guard"]


def test_downgrade_restores_only_the_original_single_column_foreign_keys() -> None:
    source = getsource(_load_migration().downgrade)

    for original in (
        "fk_users_default_account_id",
        "categories_parent_id_fkey",
        "audit_events_transaction_id_fkey",
        "recurring_instances_draft_id_fkey",
        "import_rows_draft_id_fkey",
    ):
        assert original in source

    assert source.count('ondelete="SET NULL"') == 3
    assert "execute(" not in source
    assert "UPDATE " not in source
    assert "DELETE " not in source
