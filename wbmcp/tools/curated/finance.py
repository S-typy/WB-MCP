"""Деньги и тарифы: баланс продавца, тарифы WB."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from wbmcp.tools.curated.kit import READ, Kit

TARIFFS = {
    "commission": "/api/v1/tariffs/commission",
    "box": "/api/v1/tariffs/box",
    "pallet": "/api/v1/tariffs/pallet",
    "return": "/api/v1/tariffs/return",
}


def register(server: Any, kit: Kit) -> None:
    """Инструменты финансов и тарифов."""

    async def wb_balance(seller: str | None = None, fresh: bool = False,
                         ctx: Context | None = None) -> dict[str, Any]:
        """Баланс продавца: текущий, доступный к выводу, валюта (нужен scope finance)."""
        return await kit.call("GET", "/api/v1/account/balance", {"seller": seller, "fresh": fresh},
                              tool="wb_balance", ctx=ctx)

    async def wb_tariffs(
        kind: str = "commission",
        seller: str | None = None,
        date: str | None = None,
        locale: str | None = None,
        fresh: bool = False,
        select: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Тарифы WB: комиссии по категориям, короба, монопаллеты или возвраты.

        kind — commission, box, pallet или return. Для box, pallet и return нужна дата
        YYYY-MM-DD, по умолчанию сегодня. Ответы кэшируются на час.
        """
        if kind not in TARIFFS:
            raise ToolError(f"kind должен быть одним из: {', '.join(TARIFFS)}")
        arguments: dict[str, Any] = {"seller": seller, "fresh": fresh, "select": select}
        if kind == "commission":
            if locale:
                arguments["locale"] = locale
        else:
            arguments["date"] = date or datetime.now(UTC).date().isoformat()
        return await kit.call("GET", TARIFFS[kind], arguments, tool="wb_tariffs", ctx=ctx)

    server.add_tool(wb_balance, annotations=READ)
    server.add_tool(wb_tariffs, annotations=READ)
