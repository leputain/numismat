import pytest

from finbot.application.services.catalogs import catalog_slug
from finbot.application.services.onboarding import (
    DEFAULT_ACCOUNT_NAME,
    DEFAULT_ACCOUNT_SLUG,
    INITIAL_CATEGORIES,
    normalize_currency_code,
)
from finbot.domain.categories import EXPENSE_CATEGORIES, INCOME_CATEGORIES


def test_initial_catalog_constants_use_canonical_unique_slugs() -> None:
    assert DEFAULT_ACCOUNT_SLUG == catalog_slug(DEFAULT_ACCOUNT_NAME)
    assert len(INITIAL_CATEGORIES) == len(EXPENSE_CATEGORIES) + len(INCOME_CATEGORIES)

    scoped_slugs = {(category.kind, category.slug) for category in INITIAL_CATEGORIES}
    assert len(scoped_slugs) == len(INITIAL_CATEGORIES)
    assert all(category.slug == catalog_slug(category.name) for category in INITIAL_CATEGORIES)
    assert all(catalog_slug(category.slug) == category.slug for category in INITIAL_CATEGORIES)
    assert ("expense", "кафе-и-рестораны") in scoped_slugs
    assert ("income", "прочие-доходы") in scoped_slugs


def test_onboarding_currency_is_normalized_and_validated() -> None:
    assert normalize_currency_code(" rub ") == "RUB"

    for invalid in ("RU", "RUBL", "руб", "R1B"):
        with pytest.raises(ValueError, match="валюта"):
            normalize_currency_code(invalid)
