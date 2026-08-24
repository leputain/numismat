from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

import pytest

from finbot.adapters.http.finance.cursor import (
    InvalidTransactionCursorError,
    TransactionCursorCodec,
)
from finbot.application.dto import (
    DeletedTransactionCursor,
    TransactionCursor,
    TransactionListFilters,
)
from finbot.domain.transactions import TransactionType

KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA"
OWNER_ID = UUID("018f0000-0000-7000-8000-000000000001")
OTHER_OWNER_ID = UUID("018f0000-0000-7000-8000-000000000002")
LEGACY_ACTIVE_CURSOR = (
    "AQAGWOrsa9ZOAY8AAAAAcACAAAAAAAAAAyK26PL3KlDmVqE8KmRnZ_Abvq3igSB7Ug13aFzrpONV"
)


def _cursor() -> TransactionCursor:
    return TransactionCursor(
        occurred_at=datetime(2026, 8, 13, 10, 11, 12, 345678, tzinfo=UTC),
        transaction_id=UUID("018f0000-0000-7000-8000-000000000003"),
    )


def test_cursor_codec_is_canonical_deterministic_and_owner_bound() -> None:
    codec = TransactionCursorCodec(KEY)
    encoded = codec.encode(OWNER_ID, _cursor())

    assert len(encoded) == 76
    assert "=" not in encoded
    assert codec.encode(OWNER_ID, _cursor()) == encoded
    assert codec.decode(OWNER_ID, encoded) == _cursor()
    assert "2026" not in repr(codec.decode(OWNER_ID, encoded))
    with pytest.raises(InvalidTransactionCursorError):
        codec.decode(OTHER_OWNER_ID, encoded)


def test_active_and_deleted_cursor_domains_are_not_interchangeable() -> None:
    codec = TransactionCursorCodec(KEY)
    active = codec.encode(OWNER_ID, _cursor())
    deleted_cursor = DeletedTransactionCursor(
        deleted_at=_cursor().occurred_at,
        transaction_id=_cursor().transaction_id,
    )
    deleted = codec.encode_deleted(OWNER_ID, deleted_cursor)

    assert codec.decode_deleted(OWNER_ID, deleted) == deleted_cursor
    with pytest.raises(InvalidTransactionCursorError):
        codec.decode_deleted(OWNER_ID, active)
    with pytest.raises(InvalidTransactionCursorError):
        codec.decode(OWNER_ID, deleted)


def test_active_cursor_is_bound_to_canonical_filter_fingerprint() -> None:
    codec = TransactionCursorCodec(KEY)
    start = datetime(2026, 8, 1, tzinfo=UTC)
    account_id = UUID("018f0000-0000-7000-8000-000000000004")
    filters = TransactionListFilters(
        start=start,
        end=datetime(2026, 9, 1, tzinfo=UTC),
        kind=TransactionType.EXPENSE,
        account_id=account_id,
        currency="RUB",
    )
    equivalent = TransactionListFilters(
        currency="RUB",
        account_id=account_id,
        kind=TransactionType.EXPENSE,
        end=datetime(2026, 9, 1, tzinfo=UTC),
        start=start,
    )
    encoded = codec.encode(OWNER_ID, _cursor(), filters=filters)

    assert len(encoded) == 76
    assert codec.decode(OWNER_ID, encoded, filters=equivalent) == _cursor()
    for other_filters in (
        None,
        TransactionListFilters(
            start=start,
            end=datetime(2026, 9, 1, tzinfo=UTC),
            kind=TransactionType.INCOME,
            account_id=account_id,
            currency="RUB",
        ),
        TransactionListFilters(
            start=start,
            end=datetime(2026, 9, 1, tzinfo=UTC),
            kind=TransactionType.EXPENSE,
            account_id=account_id,
            currency="USD",
        ),
    ):
        with pytest.raises(InvalidTransactionCursorError):
            codec.decode(OWNER_ID, encoded, filters=other_filters)


def test_empty_filters_preserve_unfiltered_cursor_compatibility() -> None:
    codec = TransactionCursorCodec(KEY)
    legacy = codec.encode(OWNER_ID, _cursor())

    assert legacy == LEGACY_ACTIVE_CURSOR
    assert codec.encode(OWNER_ID, _cursor(), filters=TransactionListFilters()) == legacy
    assert (
        codec.decode(
            OWNER_ID,
            legacy,
            filters=TransactionListFilters(),
        )
        == _cursor()
    )


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value[:-1] + ("A" if value[-1] != "A" else "B"),
        lambda value: value + "=",
        lambda value: value[:-1],
        lambda value: "!" + value[1:],
        lambda value: value.lower(),
    ],
)
def test_cursor_codec_rejects_tampered_or_noncanonical_values(
    mutator: Callable[[str], str],
) -> None:
    codec = TransactionCursorCodec(KEY)
    encoded = codec.encode(OWNER_ID, _cursor())

    with pytest.raises(InvalidTransactionCursorError):
        codec.decode(OWNER_ID, mutator(encoded))


@pytest.mark.parametrize("key", ["short", KEY + "=", "!" * 43])
def test_cursor_codec_rejects_noncanonical_security_keys(key: str) -> None:
    with pytest.raises(ValueError, match="invalid HTTP cursor key"):
        TransactionCursorCodec(key)
