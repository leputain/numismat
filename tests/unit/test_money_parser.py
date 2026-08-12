import pytest

from finbot.adapters.telegram.parser import DeterministicParser
from finbot.domain.money import MAX_MINOR_UNITS, MoneyError, parse_minor


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1450", 145000), ("1 450", 145000), ("1450,50", 145050), ("0.01", 1), ("12,3", 1230)],
)
def test_money(raw, expected):
    assert parse_minor(raw) == expected
    assert parse_minor(raw) > 0


def test_money_rejects_zero_and_extra_precision():
    with pytest.raises(MoneyError):
        parse_minor("0")
    with pytest.raises(MoneyError):
        parse_minor("1.001")


def test_money_accepts_bigint_boundary_and_rejects_larger_or_scientific_values():
    assert parse_minor("92233720368547758,07") == MAX_MINOR_UNITS
    for raw in ("92233720368547758,08", "1e2", "NaN", "Infinity"):
        with pytest.raises(MoneyError):
            parse_minor(raw)


@pytest.mark.parametrize(
    "text",
    [
        "1450 ресторан",
        "1 450 ресторан",
        "1450,50 ресторан",
        "+250000 зарплата",
        "-3200 бензин",
        "вчера 799 кино",
        "12.08 540 аптека",
        "3200 бензин @наличные",
        "1450 #рестораны ужин",
    ],
)
def test_parser_examples(text):
    draft = DeterministicParser().parse(text)
    assert draft.amount_minor > 0


def test_parser_kind_and_hints():
    assert DeterministicParser().parse("+250000 зарплата").type.value == "income"
    assert DeterministicParser().parse("250000 зарплата").type.value == "expense"
    assert DeterministicParser().parse("-250000 зарплата").type.value == "expense"
    draft = DeterministicParser().parse("3200 бензин @наличные")
    assert draft.account_hint == "наличные"
    assert not draft.category_explicit


def test_parser_supports_quoted_multiword_hints():
    draft = DeterministicParser().parse('1450 ужин @"Карта Мир" #"Кафе и рестораны"')
    assert draft.account_hint == "Карта Мир"
    assert draft.category_hint == "кафе и рестораны"
    assert draft.category_explicit
    assert draft.description == "ужин"


def test_parser_rejects_broken_or_duplicate_hints():
    with pytest.raises(ValueError, match="кавычки"):
        DeterministicParser().parse('1450 ужин @"Карта Мир')
    with pytest.raises(ValueError, match="только один раз"):
        DeterministicParser().parse("1450 ужин @карта @наличные")


def test_keyword_matching_uses_whole_tokens() -> None:
    draft = DeterministicParser().parse("1450 метрополитен")
    assert draft.category_hint is None
