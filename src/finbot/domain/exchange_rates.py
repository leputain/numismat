from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_EXCHANGE_RATE_ENTRIES = 32
MAX_EXCHANGE_RATE_SCALE = 12
MAX_EXCHANGE_RATE_INTEGER_DIGITS = 6
MAX_CONVERTED_MINOR_DIGITS = 64
MVP_CURRENCY_MINOR_DIGITS = 2

_CURRENCY = re.compile(r"^[A-Z]{3}$", re.ASCII)
_RATE = re.compile(
    rf"^(?:0|[1-9]\d{{0,{MAX_EXCHANGE_RATE_INTEGER_DIGITS - 1}}})"
    rf"(?:\.(\d{{1,{MAX_EXCHANGE_RATE_SCALE}}}))?$",
    re.ASCII,
)


def validate_exchange_currency(value: str) -> str:
    if not isinstance(value, str) or _CURRENCY.fullmatch(value) is None:
        raise ValueError("Валюта курса должна состоять из трёх заглавных букв")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class ExchangeRateValue:
    coefficient: int = field(repr=False)
    scale: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.coefficient, bool)
            or not isinstance(self.coefficient, int)
            or self.coefficient < 1
            or self.coefficient > 2**63 - 1
        ):
            raise ValueError("Коэффициент курса вышел за допустимые границы")
        if (
            isinstance(self.scale, bool)
            or not isinstance(self.scale, int)
            or not 0 <= self.scale <= MAX_EXCHANGE_RATE_SCALE
        ):
            raise ValueError("Масштаб курса вышел за допустимые границы")
        if self.scale > 0 and self.coefficient % 10 == 0:
            raise ValueError("Коэффициент курса должен быть канонически нормализован")
        if self.coefficient >= 10 ** (MAX_EXCHANGE_RATE_INTEGER_DIGITS + self.scale):
            raise ValueError("Курс слишком велик")

    @property
    def canonical(self) -> str:
        digits = str(self.coefficient)
        if self.scale == 0:
            return digits
        padded = digits.zfill(self.scale + 1)
        return f"{padded[: -self.scale]}.{padded[-self.scale :]}"


def parse_exchange_rate(value: str) -> ExchangeRateValue:
    """Parse a canonical ASCII decimal rate without float or ambient Decimal context."""

    if not isinstance(value, str):
        raise ValueError("Курс должен быть строкой")
    match = _RATE.fullmatch(value)
    if match is None:
        raise ValueError("Курс должен быть положительным десятичным числом")
    integer, separator, fraction = value.partition(".")
    normalized_fraction = fraction.rstrip("0") if separator else ""
    scale = len(normalized_fraction)
    coefficient = int(integer + normalized_fraction) if normalized_fraction else int(integer)
    if coefficient == 0:
        raise ValueError("Курс должен быть больше нуля")
    return ExchangeRateValue(coefficient=coefficient, scale=scale)


@dataclass(frozen=True, slots=True, repr=False)
class ExchangeRateEntry:
    source_currency: str = field(repr=False)
    target_currency: str = field(repr=False)
    value: ExchangeRateValue = field(repr=False)
    source_minor_digits: int = MVP_CURRENCY_MINOR_DIGITS
    target_minor_digits: int = MVP_CURRENCY_MINOR_DIGITS

    def __post_init__(self) -> None:
        validate_exchange_currency(self.source_currency)
        validate_exchange_currency(self.target_currency)
        if self.source_currency == self.target_currency:
            raise ValueError("Identity-курс не должен храниться явно")
        if not isinstance(self.value, ExchangeRateValue):
            raise ValueError("Курс не прошёл проверку")
        if (
            self.source_minor_digits != MVP_CURRENCY_MINOR_DIGITS
            or self.target_minor_digits != MVP_CURRENCY_MINOR_DIGITS
        ):
            raise ValueError("MVP поддерживает только двухзнаковую валютную модель")


def round_half_even_ratio(numerator: int, denominator: int) -> int:
    if isinstance(numerator, bool) or not isinstance(numerator, int) or numerator < 0:
        raise ValueError("Числитель должен быть неотрицательным целым числом")
    if isinstance(denominator, bool) or not isinstance(denominator, int) or denominator <= 0:
        raise ValueError("Знаменатель должен быть положительным целым числом")
    quotient, remainder = divmod(numerator, denominator)
    doubled = remainder * 2
    if doubled > denominator or (doubled == denominator and quotient % 2 == 1):
        quotient += 1
    return quotient


def convert_minor_units(amount_minor: int, rate: ExchangeRateEntry) -> int:
    """Convert an aggregate amount with deterministic integer HALF_EVEN rounding."""

    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor < 0:
        raise ValueError("Конвертируемая сумма должна быть неотрицательным целым числом")
    numerator = amount_minor * rate.value.coefficient * 10**rate.target_minor_digits
    denominator = 10**rate.value.scale * 10**rate.source_minor_digits
    converted = round_half_even_ratio(numerator, denominator)
    if len(str(converted)) > MAX_CONVERTED_MINOR_DIGITS:
        raise ValueError("Конвертированный итог слишком велик")
    return converted
