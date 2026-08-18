from __future__ import annotations

import csv
import io
import re
from datetime import datetime

from finbot.application.bank_imports import (
    BankImportEncoding,
    BankImportFailureReason,
    BankImportField,
    BankImportProfile,
    BankImportValidationError,
    ParsedBankImport,
)
from finbot.domain.bank_imports import (
    MAX_BANK_IMPORT_BYTES,
    MAX_BANK_IMPORT_COLUMNS,
    MAX_BANK_IMPORT_FIELD_LENGTH,
    MAX_BANK_IMPORT_ROWS,
    ParsedBankImportRow,
    normalize_bank_import_text,
    parse_bank_import_amount,
    validate_bank_import_currency,
)
from finbot.domain.transactions import TransactionType

_UTF8_BOM = b"\xef\xbb\xbf"
_CANONICAL_HEADER = (
    "occurred_at",
    "type",
    "amount",
    "currency",
    "description",
    "account_reference",
    "reference",
)
_AWARE_ISO_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$",
    re.ASCII,
)
_CANONICAL_AMOUNT = re.compile(r"^(?:0|[1-9]\d*)(?:[.,]\d{1,2})?$", re.ASCII)


def _failure(
    reason: BankImportFailureReason,
    *,
    row_number: int | None = None,
    field: BankImportField | None = None,
) -> BankImportValidationError:
    return BankImportValidationError(reason, row_number=row_number, field=field)


def _decode(
    content: bytes,
    encoding: BankImportEncoding | None,
) -> tuple[str, BankImportEncoding]:
    selected = encoding or BankImportEncoding.UTF8
    if selected is BankImportEncoding.WINDOWS_1251 and content.startswith(_UTF8_BOM):
        raise _failure(BankImportFailureReason.INVALID_ENCODING)
    codec = "utf-8-sig" if selected is BankImportEncoding.UTF8 else "cp1251"
    try:
        return content.decode(codec, errors="strict"), selected
    except UnicodeDecodeError:
        raise _failure(BankImportFailureReason.INVALID_ENCODING) from None


def _validate_cell(
    value: str,
    *,
    row_number: int,
    field: BankImportField,
) -> str:
    if len(value) > MAX_BANK_IMPORT_FIELD_LENGTH:
        raise _failure(
            BankImportFailureReason.FIELD_LIMIT,
            row_number=row_number,
            field=field,
        )
    if value and not value.isprintable():
        raise _failure(
            BankImportFailureReason.INVALID_FIELD,
            row_number=row_number,
            field=field,
        )
    return value


def _parse_timestamp(value: str, *, row_number: int) -> datetime:
    if _AWARE_ISO_TIMESTAMP.fullmatch(value) is None:
        raise _failure(
            BankImportFailureReason.INVALID_FIELD,
            row_number=row_number,
            field=BankImportField.OCCURRED_AT,
        )
    try:
        normalized = value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else "")
        result = datetime.fromisoformat(normalized)
    except ValueError:
        raise _failure(
            BankImportFailureReason.INVALID_FIELD,
            row_number=row_number,
            field=BankImportField.OCCURRED_AT,
        ) from None
    if result.utcoffset() is None:
        raise _failure(
            BankImportFailureReason.INVALID_FIELD,
            row_number=row_number,
            field=BankImportField.OCCURRED_AT,
        )
    return result


def _parse_kind(value: str, *, row_number: int) -> TransactionType:
    try:
        return TransactionType(value)
    except ValueError:
        raise _failure(
            BankImportFailureReason.INVALID_FIELD,
            row_number=row_number,
            field=BankImportField.TYPE,
        ) from None


def _parse_amount(value: str, *, row_number: int) -> int:
    if _CANONICAL_AMOUNT.fullmatch(value) is None:
        raise _failure(
            BankImportFailureReason.INVALID_FIELD,
            row_number=row_number,
            field=BankImportField.AMOUNT,
        )
    try:
        return parse_bank_import_amount(value)
    except ValueError:
        raise _failure(
            BankImportFailureReason.INVALID_FIELD,
            row_number=row_number,
            field=BankImportField.AMOUNT,
        ) from None


def _parse_row(values: list[str], *, row_number: int) -> ParsedBankImportRow:
    if len(values) > MAX_BANK_IMPORT_COLUMNS:
        raise _failure(BankImportFailureReason.COLUMN_LIMIT, row_number=row_number)
    if len(values) != len(_CANONICAL_HEADER):
        raise _failure(BankImportFailureReason.INVALID_CSV, row_number=row_number)
    fields = tuple(
        _validate_cell(value, row_number=row_number, field=field)
        for value, field in zip(
            values,
            (
                BankImportField.OCCURRED_AT,
                BankImportField.TYPE,
                BankImportField.AMOUNT,
                BankImportField.CURRENCY,
                BankImportField.DESCRIPTION,
                BankImportField.ACCOUNT_REFERENCE,
                BankImportField.REFERENCE,
            ),
            strict=True,
        )
    )
    occurred_at, kind, amount, currency, description, account_reference, reference = fields
    try:
        safe_currency = validate_bank_import_currency(currency)
    except ValueError:
        raise _failure(
            BankImportFailureReason.INVALID_FIELD,
            row_number=row_number,
            field=BankImportField.CURRENCY,
        ) from None
    try:
        safe_description = normalize_bank_import_text(
            description,
            field_name="description",
            allow_empty=True,
        )
    except ValueError:
        raise _failure(
            BankImportFailureReason.INVALID_FIELD,
            row_number=row_number,
            field=BankImportField.DESCRIPTION,
        ) from None
    try:
        safe_account_reference = normalize_bank_import_text(
            account_reference,
            field_name="account_reference",
            allow_empty=False,
        )
    except ValueError:
        raise _failure(
            BankImportFailureReason.INVALID_FIELD,
            row_number=row_number,
            field=BankImportField.ACCOUNT_REFERENCE,
        ) from None
    try:
        safe_reference = normalize_bank_import_text(
            reference,
            field_name="reference",
            allow_empty=True,
        )
    except ValueError:
        raise _failure(
            BankImportFailureReason.INVALID_FIELD,
            row_number=row_number,
            field=BankImportField.REFERENCE,
        ) from None
    try:
        return ParsedBankImportRow(
            occurred_at=_parse_timestamp(occurred_at, row_number=row_number),
            kind=_parse_kind(kind, row_number=row_number),
            amount_minor=_parse_amount(amount, row_number=row_number),
            currency=safe_currency,
            description=safe_description,
            source_account_reference=safe_account_reference,
            source_reference=safe_reference or None,
        )
    except BankImportValidationError:
        raise
    except ValueError:
        raise _failure(
            BankImportFailureReason.INVALID_FIELD,
            row_number=row_number,
            field=BankImportField.OCCURRED_AT,
        ) from None


class StrictBankCsvParser:
    """Parse one fixed CSV profile without encoding or dialect heuristics."""

    __slots__ = ()

    def parse(
        self,
        content: bytes,
        *,
        profile: BankImportProfile,
        encoding: BankImportEncoding | None,
    ) -> ParsedBankImport:
        if profile is not BankImportProfile.CANONICAL_V1:
            raise _failure(BankImportFailureReason.UNSUPPORTED_PROFILE)
        if encoding is not None and not isinstance(encoding, BankImportEncoding):
            raise _failure(BankImportFailureReason.UNSUPPORTED_ENCODING)
        if type(content) is not bytes or not content:
            raise _failure(BankImportFailureReason.EMPTY_FILE)
        if len(content) > MAX_BANK_IMPORT_BYTES:
            raise _failure(BankImportFailureReason.FILE_TOO_LARGE)
        text, resolved_encoding = _decode(content, encoding)
        reader = csv.reader(
            io.StringIO(text, newline=""),
            delimiter=",",
            quotechar='"',
            strict=True,
        )
        try:
            try:
                header = next(reader)
            except StopIteration:
                raise _failure(BankImportFailureReason.EMPTY_FILE) from None
            if len(header) > MAX_BANK_IMPORT_COLUMNS:
                raise _failure(
                    BankImportFailureReason.COLUMN_LIMIT,
                    row_number=1,
                    field=BankImportField.HEADER,
                )
            if tuple(header) != _CANONICAL_HEADER:
                raise _failure(
                    BankImportFailureReason.HEADER_MISMATCH,
                    row_number=1,
                    field=BankImportField.HEADER,
                )
            rows: list[ParsedBankImportRow] = []
            for row_number, values in enumerate(reader, start=2):
                if len(rows) >= MAX_BANK_IMPORT_ROWS:
                    raise _failure(
                        BankImportFailureReason.ROW_LIMIT,
                        row_number=row_number,
                    )
                rows.append(_parse_row(values, row_number=row_number))
        except BankImportValidationError:
            raise
        except csv.Error:
            raise _failure(BankImportFailureReason.INVALID_CSV) from None
        if not rows:
            raise _failure(BankImportFailureReason.EMPTY_FILE)
        return ParsedBankImport(
            profile=profile,
            encoding=resolved_encoding,
            rows=tuple(rows),
        )


__all__ = ["StrictBankCsvParser"]
