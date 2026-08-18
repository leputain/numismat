from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from finbot.domain.money import parse_minor, validate_minor
from finbot.domain.transactions import TransactionType

MAX_BANK_IMPORT_BYTES = 2 * 1024 * 1024
MAX_BANK_IMPORT_ROWS = 2_000
MAX_BANK_IMPORT_COLUMNS = 32
MAX_BANK_IMPORT_FIELD_LENGTH = 500
MAX_BANK_IMPORT_DESCRIPTION_LENGTH = 500
MAX_RECONCILIATION_CANDIDATES = 5
RECONCILIATION_DATE_WINDOW_DAYS = 3

_CURRENCY = re.compile(r"^[A-Z]{3}$", re.ASCII)


def validate_bank_import_currency(value: str) -> str:
    if not isinstance(value, str) or _CURRENCY.fullmatch(value) is None:
        raise ValueError("Валюта импортируемой операции должна состоять из трёх заглавных букв")
    return value


def normalize_bank_import_text(
    value: str,
    *,
    field_name: str,
    allow_empty: bool,
    max_length: int = MAX_BANK_IMPORT_FIELD_LENGTH,
) -> str:
    """Normalize one already-decoded field without retaining its raw spelling."""

    if not isinstance(value, str):
        raise ValueError(f"Поле {field_name} должно быть строкой")
    if not value.isprintable() and value:
        raise ValueError(f"Поле {field_name} содержит недопустимые символы")
    normalized = " ".join(unicodedata.normalize("NFKC", value).strip().split())
    if not normalized and not allow_empty:
        raise ValueError(f"Поле {field_name} не может быть пустым")
    if len(normalized) > max_length:
        raise ValueError(f"Поле {field_name} слишком длинное")
    return normalized


def parse_bank_import_amount(value: str) -> int:
    """Parse a positive two-decimal amount through the shared integer-money boundary."""

    return parse_minor(value, minor_digits=2)


def reconciliation_bounds(occurred_at: datetime) -> tuple[datetime, datetime]:
    if not isinstance(occurred_at, datetime) or occurred_at.utcoffset() is None:
        raise ValueError("Дата импортируемой операции должна содержать часовой пояс")
    normalized = occurred_at.astimezone(UTC)
    try:
        window = timedelta(days=RECONCILIATION_DATE_WINDOW_DAYS)
        return normalized - window, normalized + window
    except OverflowError as exc:
        raise ValueError("Дата импортируемой операции вышла за допустимые границы") from exc


def reconciliation_distance_microseconds(left: datetime, right: datetime) -> int:
    if left.utcoffset() is None or right.utcoffset() is None:
        raise ValueError("Даты reconciliation должны содержать часовой пояс")
    delta = abs(left.astimezone(UTC) - right.astimezone(UTC))
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def reconciliation_rank(
    imported_at: datetime,
    candidate_at: datetime,
    candidate_id: UUID,
) -> tuple[int, datetime, int]:
    if not isinstance(candidate_id, UUID):
        raise ValueError("Кандидат reconciliation должен иметь UUID")
    if not isinstance(candidate_at, datetime) or candidate_at.utcoffset() is None:
        raise ValueError("Дата кандида reconciliation должна содержать часовой пояс")
    candidate_utc = candidate_at.astimezone(UTC)
    return (
        reconciliation_distance_microseconds(imported_at, candidate_utc),
        candidate_utc,
        candidate_id.int,
    )


@dataclass(frozen=True, slots=True, repr=False)
class ParsedBankImportRow:
    """Normalized row held only in memory until keyed digests are produced."""

    occurred_at: datetime = field(repr=False)
    kind: TransactionType = field(repr=False)
    amount_minor: int = field(repr=False)
    currency: str = field(repr=False)
    description: str = field(repr=False)
    source_account_reference: str = field(repr=False)
    source_reference: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.occurred_at, datetime) or self.occurred_at.utcoffset() is None:
            raise ValueError("Дата импортируемой операции должна содержать часовой пояс")
        reconciliation_bounds(self.occurred_at)
        if not isinstance(self.kind, TransactionType):
            raise ValueError("Тип импортируемой операции не поддерживается")
        validate_minor(self.amount_minor)
        validate_bank_import_currency(self.currency)
        description = normalize_bank_import_text(
            self.description,
            field_name="description",
            allow_empty=True,
            max_length=MAX_BANK_IMPORT_DESCRIPTION_LENGTH,
        )
        account_reference = normalize_bank_import_text(
            self.source_account_reference,
            field_name="account_reference",
            allow_empty=False,
        )
        reference = (
            normalize_bank_import_text(
                self.source_reference,
                field_name="reference",
                allow_empty=True,
            )
            if self.source_reference is not None
            else ""
        )
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(UTC))
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "source_account_reference", account_reference)
        object.__setattr__(self, "source_reference", reference or None)
