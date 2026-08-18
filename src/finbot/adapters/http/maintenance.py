from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import cast

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finbot.adapters.database.repositories.http_idempotency import (
    SqlAlchemyHttpIdempotencyRepository,
)
from finbot.adapters.database.repositories.web_sessions import (
    SqlAlchemyWebSessionRepository,
)

HTTP_SECURITY_CLEANUP_INITIAL_DELAY_SECONDS = 60
HTTP_SECURITY_CLEANUP_INTERVAL_SECONDS = 15 * 60
HTTP_SECURITY_CLEANUP_TIMEOUT_SECONDS = 10
HTTP_SECURITY_CLEANUP_BATCH_SIZE = 500

# A database-scoped constant serializes maintenance without putting an owner or
# Telegram identifier into SQL. Transaction scope prevents pooled-lock leakage.
_HTTP_SECURITY_CLEANUP_ADVISORY_LOCK = 0x46494E48545450
_LOCK_TIMEOUT_SQL = text("SET LOCAL lock_timeout = '1s'")
_STATEMENT_TIMEOUT_SQL = text("SET LOCAL statement_timeout = '5s'")


class HttpSecurityMaintenance:
    """Bounded periodic cleanup for short-lived HTTP security state."""

    __slots__ = ("_initial_delay", "_interval", "_logger", "_sessions", "_timeout")

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        initial_delay: int = HTTP_SECURITY_CLEANUP_INITIAL_DELAY_SECONDS,
        interval: int = HTTP_SECURITY_CLEANUP_INTERVAL_SECONDS,
        timeout: int = HTTP_SECURITY_CLEANUP_TIMEOUT_SECONDS,
    ) -> None:
        if type(initial_delay) is not int or initial_delay < 0:
            raise ValueError("maintenance initial delay must be a non-negative integer")
        if type(interval) is not int or interval < 1:
            raise ValueError("maintenance interval must be a positive integer")
        if type(timeout) is not int or timeout < 1:
            raise ValueError("maintenance timeout must be a positive integer")
        self._sessions = sessions
        self._initial_delay = initial_delay
        self._interval = interval
        self._timeout = timeout
        self._logger = logging.getLogger("finbot.http.maintenance")

    async def cleanup_once(self) -> bool:
        """Delete at most one batch per table; return whether this runner held the lock."""

        async with self._sessions.begin() as session:
            await session.execute(_LOCK_TIMEOUT_SQL)
            await session.execute(_STATEMENT_TIMEOUT_SQL)
            lock_acquired = await session.scalar(
                select(func.pg_try_advisory_xact_lock(_HTTP_SECURITY_CLEANUP_ADVISORY_LOCK))
            )
            if lock_acquired is not True:
                return False
            now = cast(datetime | None, await session.scalar(select(func.now())))
            if now is None:  # pragma: no cover - PostgreSQL always returns transaction time
                raise RuntimeError("database clock is unavailable")
            await SqlAlchemyWebSessionRepository(session).cleanup(
                now=now,
                limit=HTTP_SECURITY_CLEANUP_BATCH_SIZE,
            )
            await SqlAlchemyHttpIdempotencyRepository(session).cleanup(
                now=now,
                limit=HTTP_SECURITY_CLEANUP_BATCH_SIZE,
            )
        return True

    async def run(self) -> None:
        """Run until the ASGI lifespan cancels this task."""

        await asyncio.sleep(self._initial_delay)
        while True:
            try:
                async with asyncio.timeout(self._timeout):
                    lock_acquired = await self.cleanup_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.error(
                    "http_security_cleanup_completed",
                    extra={"result": "error"},
                )
            else:
                self._logger.info(
                    "http_security_cleanup_completed",
                    extra={"result": "success" if lock_acquired else "ignored"},
                )
            await asyncio.sleep(self._interval)


__all__ = [
    "HTTP_SECURITY_CLEANUP_BATCH_SIZE",
    "HTTP_SECURITY_CLEANUP_INITIAL_DELAY_SECONDS",
    "HTTP_SECURITY_CLEANUP_INTERVAL_SECONDS",
    "HTTP_SECURITY_CLEANUP_TIMEOUT_SECONDS",
    "HttpSecurityMaintenance",
]
