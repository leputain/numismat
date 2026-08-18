from datetime import UTC, datetime
from typing import Any, cast

from mcp import Client

from finbot.adapters.mcp.schemas import McpDashboard, McpPeriodTotals
from finbot.adapters.mcp.server import build_mcp_server
from finbot.adapters.mcp.service import McpFinanceQueryService


class _DashboardService:
    async def dashboard(self) -> McpDashboard:
        period = McpPeriodTotals(
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 8, 2, tzinfo=UTC),
            totals=(),
        )
        return McpDashboard(
            current_period=period,
            comparable_period=period,
            top_categories=(),
            recent_transactions=(),
        )


async def test_mcp_surface_is_ownerless_read_only_and_structured() -> None:
    service = cast(McpFinanceQueryService, cast(Any, _DashboardService()))
    server = build_mcp_server(service)

    async with Client(server) as client:
        discovered = await client.list_tools()
        tools = {tool.name: tool for tool in discovered.tools}
        assert set(tools) == {
            "finance_dashboard",
            "finance_get_transaction",
            "finance_list_accounts",
            "finance_list_categories",
            "finance_list_transactions",
            "finance_period_report",
        }
        for tool in tools.values():
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False
            assert "owner" not in tool.input_schema.get("properties", {})
        assert (
            tools["finance_list_transactions"].input_schema["properties"]["page"]["maximum"] == 200
        )

        result = await client.call_tool("finance_dashboard", {})

    assert result.is_error is False
    assert result.structured_content == {
        "current_period": {
            "end": "2026-08-02T00:00:00Z",
            "start": "2026-08-01T00:00:00Z",
            "totals": [],
        },
        "comparable_period": {
            "end": "2026-08-02T00:00:00Z",
            "start": "2026-08-01T00:00:00Z",
            "totals": [],
        },
        "recent_transactions": [],
        "top_categories": [],
    }
