from dataclasses import dataclass, field
from string import ascii_letters, digits
from typing import Protocol
from uuid import UUID

from finbot.application.dto import DraftRef
from finbot.application.queries.transactions import TransactionDetails

MAX_CSV_EXPORT_ROWS = 10_000
MAX_CSV_EXPORT_BYTES = 16 * 1024 * 1024
_SAFE_FILENAME_CHARACTERS = frozenset(ascii_letters + digits + "-_.")


class CsvExportReader(Protocol):
    async def export(
        self,
        owner_id: UUID,
        *,
        limit: int,
    ) -> list[TransactionDetails]: ...


@dataclass(frozen=True, slots=True)
class CsvExportReceiptSnapshot:
    suspended_draft: DraftRef | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.suspended_draft is not None and not isinstance(self.suspended_draft, DraftRef):
            raise TypeError("Suspended export draft must be an exact reference")


@dataclass(frozen=True, slots=True)
class GeneratedCsvExport:
    content: bytes = field(repr=False)
    filename: str = field(repr=False)
    row_count: int = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes):
            raise TypeError("CSV export content must be bytes")
        if len(self.content) > MAX_CSV_EXPORT_BYTES:
            raise ValueError("CSV export is too large")
        if (
            not 1 <= len(self.filename) <= 128
            or not self.filename.endswith(".csv")
            or not self.filename.isascii()
            or self.filename.startswith(".")
            or any(character not in _SAFE_FILENAME_CHARACTERS for character in self.filename)
        ):
            raise ValueError("CSV export filename is invalid")
        if not 1 <= self.row_count <= MAX_CSV_EXPORT_ROWS:
            raise ValueError("CSV export row count is invalid")


class CsvExportTooLargeError(ValueError):
    """The owner-scoped export exceeds the bounded in-memory delivery contract."""
