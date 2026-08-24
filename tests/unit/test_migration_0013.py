from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _migration() -> ModuleType:
    path = Path(__file__).parents[2] / "migrations" / "versions" / "0013_settings_version.py"
    spec = importlib.util.spec_from_file_location("migration_0013_settings_version", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_settings_version_migration_follows_multitenant_head_and_has_roundtrip() -> None:
    migration = _migration()

    assert migration.revision == "0013_settings_version"
    assert migration.down_revision == "0012_multitenant_integrity"
    assert callable(migration.upgrade)
    assert callable(migration.downgrade)


def test_settings_version_migration_is_bounded_and_non_nullable() -> None:
    source = (
        Path(__file__).parents[2] / "migrations" / "versions" / "0013_settings_version.py"
    ).read_text(encoding="utf-8")

    assert '"settings_version"' in source
    assert "nullable=False" in source
    assert 'server_default=sa.text("1")' in source
    assert '"settings_version >= 1"' in source
