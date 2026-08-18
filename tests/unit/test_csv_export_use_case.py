from datetime import UTC, datetime
from uuid import UUID

import pytest

import finbot.application.use_cases.export as export_use_case
from finbot.application.export import (
    MAX_CSV_EXPORT_ROWS,
    CsvExportTooLargeError,
    GeneratedCsvExport,
)
from finbot.application.queries.transactions import TransactionDetails
from finbot.application.use_cases.export import GenerateCsvExport

OWNER_ID = UUID("00000000-0000-7000-8000-000000000101")
TRANSACTION_ID = UUID("00000000-0000-7000-8000-000000000301")
ACCOUNT_ID = UUID("00000000-0000-7000-8000-000000000401")
CATEGORY_ID = UUID("00000000-0000-7000-8000-000000000501")


class _Reader:
    def __init__(self, rows: list[TransactionDetails]) -> None:
        self.rows = rows
        self.calls: list[tuple[UUID, int]] = []

    async def export(
        self,
        owner_id: UUID,
        *,
        limit: int,
    ) -> list[TransactionDetails]:
        self.calls.append((owner_id, limit))
        return self.rows[:limit]


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 8, 13, 12, tzinfo=UTC)


def _row(
    *,
    account_name: str = '=WEBSERVICE("https://invalid")',
    category_name: str = "+cmd",
    description: str = "@SUM(A1:A2)",
) -> TransactionDetails:
    return TransactionDetails(
        TRANSACTION_ID,
        "expense",
        12_345,
        "RUB",
        ACCOUNT_ID,
        account_name,
        CATEGORY_ID,
        category_name,
        "",
        datetime(2026, 8, 13, 9, tzinfo=UTC),
        description,
        "manual",
        None,
        1,
    )


@pytest.mark.asyncio
async def test_generate_export_is_bounded_and_preserves_spreadsheet_injection_protection() -> None:
    reader = _Reader([_row()])

    generated = await GenerateCsvExport(reader, _Clock())(OWNER_ID, "UTC")

    assert generated is not None
    assert reader.calls == [(OWNER_ID, MAX_CSV_EXPORT_ROWS + 1)]
    assert generated.filename == "finbot-all-2026-08-13.csv"
    assert generated.row_count == 1
    decoded = generated.content.decode("utf-8-sig")
    assert "'=WEBSERVICE" in decoded
    assert "'+cmd" in decoded
    assert "'@SUM" in decoded
    rendered = repr(generated)
    assert "WEBSERVICE" not in rendered
    assert "finbot-all" not in rendered
    assert "row_count" not in rendered


@pytest.mark.asyncio
async def test_empty_export_returns_no_document() -> None:
    generated = await GenerateCsvExport(_Reader([]), _Clock())(OWNER_ID, "UTC")

    assert generated is None


@pytest.mark.asyncio
async def test_row_limit_fails_closed_before_building_csv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row(account_name="safe", category_name="safe", description="safe")

    def forbidden_build(*args: object, **kwargs: object) -> bytes:
        del args, kwargs
        raise AssertionError("oversized row set reached CSV materialization")

    monkeypatch.setattr(export_use_case, "build_csv", forbidden_build)

    with pytest.raises(CsvExportTooLargeError):
        await GenerateCsvExport(
            _Reader([row] * (MAX_CSV_EXPORT_ROWS + 1)),
            _Clock(),
        )(OWNER_ID, "UTC")


@pytest.mark.asyncio
async def test_byte_limit_fails_closed_without_exposing_generated_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(export_use_case, "MAX_CSV_EXPORT_BYTES", 3)
    monkeypatch.setattr(export_use_case, "build_csv", lambda *_args: b"private")

    with pytest.raises(CsvExportTooLargeError) as raised:
        await GenerateCsvExport(_Reader([_row()]), _Clock())(OWNER_ID, "UTC")

    assert "private" not in repr(raised.value)


@pytest.mark.parametrize(
    "filename",
    ["../escape.csv", "subdir/export.csv", ".hidden.csv", "export.txt", "é.csv"],
)
def test_generated_export_rejects_unsafe_filenames(filename: str) -> None:
    with pytest.raises(ValueError, match="filename"):
        GeneratedCsvExport(b"safe", filename, 1)
