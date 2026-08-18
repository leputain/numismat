from importlib.util import module_from_spec, spec_from_file_location
from inspect import getsource
from pathlib import Path
from types import ModuleType

import pytest


def _load_migration() -> ModuleType:
    path = Path(__file__).parents[2] / "migrations" / "versions" / "0007_http_security_state.py"
    spec = spec_from_file_location("finbot_migration_0007", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load migration 0007")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_http_security_state_migration_follows_head_and_has_roundtrip() -> None:
    migration = _load_migration()

    assert migration.revision == "0007_http_security_state"
    assert migration.down_revision == "0006_csv_export_outbox_job"
    assert callable(migration.upgrade)
    assert callable(migration.downgrade)


def test_upgrade_stores_only_bounded_digest_and_result_reference_state() -> None:
    source = getsource(_load_migration().upgrade)

    for required in (
        '"web_sessions"',
        '"http_idempotency"',
        "octet_length(session_token_hash) = 32",
        "octet_length(csrf_token_hash) = 32",
        "octet_length(idempotency_key_hash) = 32",
        "octet_length(request_fingerprint) = 32",
        "INTERVAL '24 hours'",
        "http_status IS NOT NULL",
        "result_revision IS NOT NULL",
        '"ix_web_sessions_expires_at"',
        '"ix_web_sessions_revoked_at"',
        '"ix_http_idempotency_expires_at"',
    ):
        assert required in source
    for forbidden in (
        "raw_token",
        "request_body",
        "response_body",
        "telegram_user_id",
        "user_agent",
        "ip_address",
    ):
        assert forbidden not in source


def test_downgrade_fails_before_ddl_while_unexpired_state_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    events: list[str] = []

    class _Bind:
        def scalar(self, statement: object) -> bool:
            events.append("guard")
            rendered = str(statement)
            assert "revoked_at IS NULL AND expires_at > now()" in rendered
            assert "http_idempotency WHERE expires_at > now()" in rendered
            return True

    class _Op:
        class _Context:
            as_sql = False

        def get_context(self) -> _Context:
            return self._Context()

        def get_bind(self) -> _Bind:
            return _Bind()

        def execute(self, statement: object) -> None:
            assert str(statement) == (
                "LOCK TABLE web_sessions, http_idempotency IN ACCESS EXCLUSIVE MODE"
            )
            events.append("lock")

        def drop_index(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            events.append("drop_index")

        def drop_table(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            events.append("drop_table")

    monkeypatch.setattr(migration, "op", _Op())

    with pytest.raises(RuntimeError, match="unexpired HTTP security state"):
        migration.downgrade()

    assert events == ["lock", "guard"]


def test_downgrade_rejects_offline_mode_before_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    mutations: list[str] = []

    class _Op:
        class _Context:
            as_sql = True

        def get_context(self) -> _Context:
            return self._Context()

        def get_bind(self) -> object:
            raise AssertionError("offline downgrade must not query a synthetic bind")

        def execute(self, statement: object) -> None:
            del statement
            raise AssertionError("offline downgrade must not lock tables")

        def drop_index(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            mutations.append("drop_index")

        def drop_table(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            mutations.append("drop_table")

    monkeypatch.setattr(migration, "op", _Op())

    with pytest.raises(RuntimeError, match="offline mode"):
        migration.downgrade()

    assert mutations == []


def test_downgrade_holds_exclusive_lock_through_guard_and_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    events: list[str] = []

    class _Bind:
        def scalar(self, statement: object) -> bool:
            del statement
            events.append("guard")
            return False

    class _Op:
        class _Context:
            as_sql = False

        def get_context(self) -> _Context:
            return self._Context()

        def execute(self, statement: object) -> None:
            assert str(statement) == (
                "LOCK TABLE web_sessions, http_idempotency IN ACCESS EXCLUSIVE MODE"
            )
            events.append("lock")

        def get_bind(self) -> _Bind:
            return _Bind()

        def drop_index(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            events.append("ddl")

        def drop_table(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            events.append("ddl")

    monkeypatch.setattr(migration, "op", _Op())

    migration.downgrade()

    assert events[:2] == ["lock", "guard"]
    assert events[2:] == ["ddl"] * 6
