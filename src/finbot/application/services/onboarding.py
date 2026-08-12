from dataclasses import dataclass

from finbot.application.services.catalogs import catalog_slug
from finbot.domain.categories import (
    CATEGORY_EMOJIS,
    EXPENSE_CATEGORIES,
    INCOME_CATEGORIES,
)

DEFAULT_ACCOUNT_NAME = "Основная карта"
DEFAULT_ACCOUNT_SLUG = catalog_slug(DEFAULT_ACCOUNT_NAME)


@dataclass(frozen=True, slots=True)
class InitialCategory:
    kind: str
    name: str
    slug: str
    emoji: str


def _initial_category(kind: str, name: str) -> InitialCategory:
    display_name = name.title()
    return InitialCategory(
        kind=kind,
        name=display_name,
        slug=catalog_slug(display_name),
        emoji=CATEGORY_EMOJIS[name],
    )


INITIAL_CATEGORIES = tuple(
    _initial_category("expense", name) for name in EXPENSE_CATEGORIES
) + tuple(_initial_category("income", name) for name in INCOME_CATEGORIES)


def normalize_currency_code(value: str) -> str:
    """Return the canonical ISO-style code accepted by the persistence schema."""

    code = value.strip().upper()
    if len(code) != 3 or not code.isascii() or not code.isalpha():
        raise ValueError("Некорректная валюта счёта")
    return code
