from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

import pytest

from finbot.application.services.catalogs import catalog_slug
from finbot.domain.category_rules import normalize_rule_pattern


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[2]
        / "migrations"
        / "versions"
        / "0003_reliability_and_smart_input.py"
    )
    spec = spec_from_file_location("finbot_migration_0003", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load migration 0003")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration()


def test_revision_pins_runtime_unicode_normalization() -> None:
    rule_samples = ("Ёлка!!!", "  CAFÉ  ", "кафе-бар", "one two three")
    for sample in rule_samples:
        assert migration._normalize_rule_pattern(sample) == normalize_rule_pattern(sample)

    slug_samples = ("  Кафе   И Рестораны  ", "STRAßE", "кафе-бар")
    for sample in slug_samples:
        assert migration._catalog_slug(sample) == catalog_slug(sample)


def test_revision_discards_rules_rejected_by_runtime() -> None:
    assert migration._normalize_rule_pattern("") is None
    assert migration._normalize_rule_pattern("... !!! ___") is None
    assert migration._normalize_rule_pattern("one two three four five six") is None


def test_revision_fails_closed_in_offline_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: True)

    with pytest.raises(RuntimeError, match="requires an online database connection"):
        migration.upgrade()
