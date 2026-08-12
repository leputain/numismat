from pathlib import Path

import pytest

from finbot.adapters.database import provision as runtime_provision


def _write(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8")
    return path


def test_load_target_requires_exact_endpoint_and_hides_credentials(tmp_path: Path) -> None:
    migrations = _write(
        tmp_path / "migrations",
        "postgresql+psycopg://finbot_migrations:migrations-change-me@db:5432/finbot_test"
        "?sslmode=require",
    )
    runtime = _write(
        tmp_path / "runtime",
        "postgresql+psycopg://finbot_runtime:runtime-change-me@db:5432/finbot_test?sslmode=require",
    )

    target = runtime_provision.load_target(migrations, runtime)

    assert target.database == "finbot_test"
    assert target.owner_role == "finbot_migrations"
    assert target.runtime_role == "finbot_runtime"
    rendered = repr(target)
    assert "migrations-change-me" not in rendered
    assert "runtime-change-me" not in rendered
    assert "postgresql" not in rendered


@pytest.mark.parametrize(
    ("migration_suffix", "runtime_suffix"),
    [
        ("", "?sslmode=require"),
        ("?sslmode=require", "?sslmode=verify-full"),
    ],
)
def test_load_target_rejects_different_query_parameters(
    tmp_path: Path, migration_suffix: str, runtime_suffix: str
) -> None:
    migrations = _write(
        tmp_path / "migrations",
        "postgresql+psycopg://migration:migrations-change-me@db/finbot_test" + migration_suffix,
    )
    runtime = _write(
        tmp_path / "runtime",
        "postgresql+psycopg://runtime:runtime-change-me@db/finbot_test" + runtime_suffix,
    )

    with pytest.raises(ValueError, match="exact same"):
        runtime_provision.load_target(migrations, runtime)


def test_load_target_rejects_session_or_target_overrides(tmp_path: Path) -> None:
    suffix = "?options=-crole%3Dfinbot_migrations"
    migrations = _write(
        tmp_path / "migrations",
        "postgresql+psycopg://migration:migrations-change-me@db/finbot_test" + suffix,
    )
    runtime = _write(
        tmp_path / "runtime",
        "postgresql+psycopg://runtime:runtime-change-me@db/finbot_test" + suffix,
    )

    with pytest.raises(ValueError, match="cannot override"):
        runtime_provision.load_target(migrations, runtime)


def test_main_never_prints_exception_or_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    leaked = "postgresql+psycopg://runtime:do-not-print@db/finbot_test"

    def fail() -> runtime_provision.ProvisioningTarget:
        raise RuntimeError(leaked)

    monkeypatch.setattr(runtime_provision, "load_target", fail)

    assert runtime_provision.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error_class":"RuntimeError","status":"error"}\n'
    assert leaked not in captured.err
    assert "do-not-print" not in captured.err


def test_password_is_never_interpolated_into_role_sql() -> None:
    source = Path(runtime_provision.__file__).read_text(encoding="utf-8")

    assert "change_password(" in source
    assert "sql.Literal(target.runtime_password)" not in source
    assert "PASSWORD {}" not in source
