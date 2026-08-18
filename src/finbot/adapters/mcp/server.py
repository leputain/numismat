from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

from pydantic import Field

from finbot import __version__
from finbot.adapters.mcp.schemas import (
    McpAccountCatalog,
    McpCategoryCatalog,
    McpDashboard,
    McpPeriodReport,
    McpTransaction,
    McpTransactionPage,
)
from finbot.adapters.mcp.service import MAX_MCP_TRANSACTION_PAGE, McpFinanceQueryService
from finbot.application.errors import ApplicationError
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

_LOGGER = logging.getLogger("finbot.mcp")
_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

PageNumber = Annotated[int, Field(strict=True, ge=0, le=MAX_MCP_TRANSACTION_PAGE)]
PageSize = Annotated[int, Field(strict=True, ge=1, le=50)]
StrictBoolean = Annotated[bool, Field(strict=True)]
IsoDate = Annotated[
    str,
    Field(strict=True, min_length=10, max_length=10, pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"),
]
CanonicalUuid = Annotated[
    str,
    Field(
        strict=True,
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    ),
]


async def _execute[ResultT](operation: Callable[[], Awaitable[ResultT]]) -> ResultT:
    try:
        result = await operation()
    except ApplicationError as exc:
        _LOGGER.info("mcp_tool_completed", extra={"result": "rejected"})
        raise ToolError(str(exc)) from None
    except Exception:
        _LOGGER.error("mcp_tool_completed", extra={"result": "error"})
        raise ToolError("Локальный запрос не выполнен") from None
    _LOGGER.info("mcp_tool_completed", extra={"result": "success"})
    return result


def build_mcp_server(service: McpFinanceQueryService) -> MCPServer[None]:
    """Build a local stdio server with no owner or write parameters."""

    server: MCPServer[None] = MCPServer(
        name="numismat-readonly",
        title="Numismat read-only finance",
        description="Bounded read-only access to one locally configured Numismat owner.",
        instructions=(
            "Every tool is read-only and bound to the owner configured by the local process. "
            "All money values are integer minor units encoded as decimal strings. "
            "Never infer a write capability from these tools."
        ),
        version=__version__,
        log_level="ERROR",
    )

    @server.tool(
        name="finance_dashboard",
        description="Return the current month-to-date dashboard and comparable prior period.",
        annotations=_READ_ONLY,
        structured_output=True,
    )
    async def finance_dashboard() -> McpDashboard:
        return await _execute(service.dashboard)

    @server.tool(
        name="finance_period_report",
        description=(
            "Return a bounded owner-timezone report for inclusive ISO dates, at most 366 days."
        ),
        annotations=_READ_ONLY,
        structured_output=True,
    )
    async def finance_period_report(start: IsoDate, end: IsoDate) -> McpPeriodReport:
        return await _execute(lambda: service.report(start, end))

    @server.tool(
        name="finance_list_transactions",
        description="Return one bounded page of active transactions or deleted transactions.",
        annotations=_READ_ONLY,
        structured_output=True,
    )
    async def finance_list_transactions(
        page: PageNumber = 0,
        page_size: PageSize = 20,
        deleted: StrictBoolean = False,
    ) -> McpTransactionPage:
        return await _execute(
            lambda: service.list_transactions(
                page=page,
                page_size=page_size,
                deleted=deleted,
            )
        )

    @server.tool(
        name="finance_get_transaction",
        description="Return one owner-scoped transaction by canonical UUID.",
        annotations=_READ_ONLY,
        structured_output=True,
    )
    async def finance_get_transaction(transaction_id: CanonicalUuid) -> McpTransaction:
        return await _execute(lambda: service.get_transaction(transaction_id))

    @server.tool(
        name="finance_list_accounts",
        description="Return the complete bounded active or archived account catalog.",
        annotations=_READ_ONLY,
        structured_output=True,
    )
    async def finance_list_accounts(
        archived: StrictBoolean = False,
    ) -> McpAccountCatalog:
        return await _execute(lambda: service.list_accounts(archived=archived))

    @server.tool(
        name="finance_list_categories",
        description="Return the complete bounded category catalog, optionally filtered by type.",
        annotations=_READ_ONLY,
        structured_output=True,
    )
    async def finance_list_categories(
        kind: Literal["expense", "income"] | None = None,
        archived: StrictBoolean = False,
    ) -> McpCategoryCatalog:
        return await _execute(lambda: service.list_categories(kind=kind, archived=archived))

    return server


__all__ = ["build_mcp_server"]
