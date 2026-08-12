import re

MAX_MINOR_UNITS = 2**63 - 1
_PLAIN_AMOUNT = re.compile(r"^\+?\d+(?:\.\d+)?$")


class MoneyError(ValueError):
    pass


def validate_minor(minor: int) -> int:
    """Validate a positive amount that can be stored in PostgreSQL BIGINT."""
    if isinstance(minor, bool) or not isinstance(minor, int):
        raise MoneyError("Сумма должна быть целым числом в минимальных единицах")
    if minor <= 0:
        raise MoneyError("Сумма должна быть больше нуля")
    if minor > MAX_MINOR_UNITS:
        raise MoneyError("Сумма слишком велика")
    return minor


def parse_minor(raw: str, minor_digits: int = 2) -> int:
    """Parse a plain decimal amount without floats or Decimal context surprises."""
    if isinstance(minor_digits, bool) or not isinstance(minor_digits, int):
        raise MoneyError("Некорректное число знаков после запятой")
    if not 0 <= minor_digits <= 18:
        raise MoneyError("Некорректное число знаков после запятой")

    normalized = raw.replace(" ", "").replace("\u00a0", "").replace(",", ".")
    if not _PLAIN_AMOUNT.fullmatch(normalized):
        if normalized.startswith("-") and _PLAIN_AMOUNT.fullmatch(normalized[1:]):
            raise MoneyError("Сумма должна быть больше нуля")
        raise MoneyError("Некорректная сумма")

    unsigned = normalized.removeprefix("+")
    major, separator, fraction = unsigned.partition(".")
    if separator and not fraction:
        raise MoneyError("Некорректная сумма")
    if len(fraction) > minor_digits:
        raise MoneyError("Слишком много знаков после запятой")
    padded_fraction = fraction.ljust(minor_digits, "0")
    minor = int(major) * (10**minor_digits)
    if padded_fraction:
        minor += int(padded_fraction)
    return validate_minor(minor)


def format_minor(minor: int, currency: str = "RUB", minor_digits: int = 2) -> str:
    sign = "−" if minor < 0 else ""
    absolute = abs(minor)
    if minor_digits:
        major, fraction = divmod(absolute, 10**minor_digits)
        number = f"{major:,}".replace(",", " ") + f",{fraction:0{minor_digits}d}"
    else:
        number = f"{absolute:,}".replace(",", " ")
    return f"{sign}{number} {currency}"
