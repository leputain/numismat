from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[2] / "migrations" / "versions" / "0004_telegram_response_outbox.py"
    )
    spec = spec_from_file_location("finbot_migration_0004", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load migration 0004")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_outbox_migration_follows_current_head_and_has_roundtrip() -> None:
    migration = _load_migration()

    assert migration.revision == "0004_telegram_response_outbox"
    assert migration.down_revision == "0003_reliability_and_smart_input"
    assert callable(migration.upgrade)
    assert callable(migration.downgrade)
