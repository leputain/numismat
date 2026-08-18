from __future__ import annotations

import asyncio
import logging
import sys

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from finbot.adapters.database.repositories.mcp_queries import (
    SqlAlchemyMcpQueryUnitOfWorkFactory,
)
from finbot.adapters.mcp.config import McpSettings
from finbot.adapters.mcp.server import build_mcp_server
from finbot.adapters.mcp.service import McpFinanceQueryService
from finbot.observability.logging import JsonFormatter


def _configure_stderr(level: str) -> None:
    """Keep privacy-safe process logs away from the stdio protocol stream."""

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for logger_name in ("mcp", "sqlalchemy", "psycopg"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def main() -> int:
    settings = McpSettings.from_secret_or_env()
    _configure_stderr(settings.log_level)
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    service = McpFinanceQueryService(
        SqlAlchemyMcpQueryUnitOfWorkFactory(
            sessions,
            settings.owner_telegram_user_id,
        )
    )
    server = build_mcp_server(service)
    try:
        server.run(transport="stdio")
    finally:
        asyncio.run(engine.dispose())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
