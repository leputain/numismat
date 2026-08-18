import pytest

from finbot.domain.exchange_rates import (
    ExchangeRateEntry,
    convert_minor_units,
    parse_exchange_rate,
)


def _rate(value: str) -> ExchangeRateEntry:
    return ExchangeRateEntry(
        source_currency="USD",
        target_currency="RUB",
        value=parse_exchange_rate(value),
    )


def test_rate_parser_normalizes_an_exact_ascii_decimal() -> None:
    value = parse_exchange_rate("92.3500")

    assert value.coefficient == 9_235
    assert value.scale == 2
    assert value.canonical == "92.35"


def test_conversion_uses_integer_half_even_ties() -> None:
    rate = _rate("0.5")

    assert convert_minor_units(1, rate) == 0
    assert convert_minor_units(3, rate) == 2


@pytest.mark.parametrize("value", ["0", "1e3", "1,25", " 1.25", "1.0000000000000"])
def test_rate_parser_rejects_zero_and_noncanonical_wire_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_exchange_rate(value)
