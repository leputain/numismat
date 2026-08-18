from __future__ import annotations

import io
from collections.abc import Buffer
from dataclasses import dataclass, field
from uuid import UUID

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finbot.adapters.database.models import Account, User
from finbot.domain.bank_imports import MAX_BANK_IMPORT_BYTES

_CSV_MIME_TYPES = frozenset(
    {
        "application/csv",
        "application/vnd.ms-excel",
        "text/comma-separated-values",
        "text/csv",
    }
)


class _BankCsvDownloadError(ValueError):
    """Internal sentinel whose message never crosses the adapter boundary."""


class _BoundedBankCsvBuffer(io.BytesIO):
    def write(self, data: Buffer, /) -> int:
        size = memoryview(data).nbytes
        if self.tell() + size > MAX_BANK_IMPORT_BYTES:
            raise _BankCsvDownloadError("bank CSV exceeds the bounded limit")
        return super().write(data)


def is_bank_csv_document(message: Message) -> bool:
    document = message.document
    if document is None:
        return False
    mime_type = (document.mime_type or "").strip().casefold()
    # Telegram's MIME metadata is not authoritative; the suffix is only an
    # ingress routing hint. The strict parser validates the actual bytes later.
    return mime_type in _CSV_MIME_TYPES or (document.file_name or "").casefold().endswith(".csv")


class BoundedTelegramBankCsvDownloader:
    """Download at most 2 MiB without retaining the Telegram filename."""

    __slots__ = ()

    async def __call__(self, bot: Bot, message: Message) -> bytes | None:
        document = message.document
        if document is None or not is_bank_csv_document(message):
            return None
        file_size = document.file_size
        if file_size is not None and (
            isinstance(file_size, bool) or file_size <= 0 or file_size > MAX_BANK_IMPORT_BYTES
        ):
            return None
        try:
            downloaded = await bot.download(
                document.file_id,
                destination=_BoundedBankCsvBuffer(),
                timeout=20,
            )
            if downloaded is None:
                return None
            if isinstance(downloaded, io.BytesIO):
                content = downloaded.getvalue()
            else:
                content = downloaded.read()
            if type(content) is not bytes or not 1 <= len(content) <= MAX_BANK_IMPORT_BYTES:
                return None
            return content
        except _BankCsvDownloadError, TelegramBadRequest:
            return None


@dataclass(frozen=True, slots=True, repr=False)
class TelegramBankImportAccount:
    owner_id: UUID = field(repr=False)
    account_id: UUID = field(repr=False)
    account_version: int = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, UUID) or not isinstance(self.account_id, UUID):
            raise TypeError("Telegram bank import account ownership is invalid")
        if (
            isinstance(self.account_version, bool)
            or not isinstance(self.account_version, int)
            or not 1 <= self.account_version <= 2**31 - 1
        ):
            raise ValueError("Telegram bank import account version is invalid")


class TelegramBankImportAccountResolver:
    """Resolve the server-authoritative active default account before download."""

    __slots__ = ("_sessions",)

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def __call__(self, owner_telegram_user_id: int) -> TelegramBankImportAccount | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(User.id, Account.id, Account.version)
                    .join(
                        Account,
                        (Account.id == User.default_account_id) & (Account.user_id == User.id),
                    )
                    .where(
                        User.telegram_user_id == owner_telegram_user_id,
                        Account.archived_at.is_(None),
                    )
                    .limit(1)
                )
            ).one_or_none()
        if row is None:
            return None
        owner_id, account_id, version = row._t
        return TelegramBankImportAccount(owner_id, account_id, version)


__all__ = [
    "BoundedTelegramBankCsvDownloader",
    "TelegramBankImportAccount",
    "TelegramBankImportAccountResolver",
    "is_bank_csv_document",
]
