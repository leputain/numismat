from datetime import datetime
from typing import Protocol
from uuid import UUID

from finbot.application.export import (
    MAX_CSV_EXPORT_BYTES,
    MAX_CSV_EXPORT_ROWS,
    CsvExportReader,
    CsvExportTooLargeError,
    GeneratedCsvExport,
)
from finbot.application.services.csv_export import build_csv


class CsvExportClock(Protocol):
    def now(self) -> datetime: ...


class GenerateCsvExport:
    """Generate one bounded owner export in memory without persistence or logging."""

    __slots__ = ("_clock", "_reader")

    def __init__(self, reader: CsvExportReader, clock: CsvExportClock) -> None:
        self._reader = reader
        self._clock = clock

    async def __call__(self, owner_id: UUID, timezone: str) -> GeneratedCsvExport | None:
        rows = await self._reader.export(owner_id, limit=MAX_CSV_EXPORT_ROWS + 1)
        if not rows:
            return None
        if len(rows) > MAX_CSV_EXPORT_ROWS:
            raise CsvExportTooLargeError
        content = build_csv(rows, timezone)
        if len(content) > MAX_CSV_EXPORT_BYTES:
            raise CsvExportTooLargeError
        generated = self._clock.now().date().isoformat()
        return GeneratedCsvExport(
            content,
            f"finbot-all-{generated}.csv",
            len(rows),
        )
