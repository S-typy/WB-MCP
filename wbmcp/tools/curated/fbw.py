"""Поставки FBW: список поставок и план приёмки (коэффициенты и опции)."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from wbmcp.tools.curated.kit import READ, Kit, data_of

SUPPLIES_PAGE = 1000


def register(server: Any, kit: Kit) -> None:
    """Инструменты поставок FBW."""

    async def wb_fbw_supplies(
        seller: str | None = None,
        filters: dict[str, Any] | None = None,
        limit: int = SUPPLIES_PAGE,
        max_pages: int = 10,
        fresh: bool = False,
        select: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Поставки FBW (на склады WB) с автоматической пагинацией по offset.

        filters — тело фильтра как в POST /api/v1/supplies: dates (from, till, type),
        statusIDs и прочее; без filters — все поставки.
        """
        owner = await kit.owner_of(seller)

        async def fetch(offset: int) -> dict[str, Any]:
            return await kit.call("POST", "/api/v1/supplies",
                                  {"seller": seller, "body": filters or {}, "limit": limit,
                                   "offset": offset, "fresh": fresh},
                                  tool="wb_fbw_supplies", ctx=ctx, compact=False)

        supplies, pages, capped = await kit.collect_offset(
            fetch, items_of=lambda r: data_of(r) if isinstance(data_of(r), list) else [],
            limit=limit, max_pages=max_pages,
        )
        payload = {"supplies": supplies, "count": len(supplies), "pages": pages,
                   "capped": capped}
        return kit.finish(payload, select, tool="wb_fbw_supplies",
                          operation=kit.op("POST", "/api/v1/supplies").id, seller=owner,
                          key="supplies")

    async def wb_fbw_plan(
        seller: str | None = None,
        warehouse_ids: list[int] | None = None,
        goods: list[dict[str, Any]] | None = None,
        fresh: bool = False,
        select: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """План поставки FBW: коэффициенты приёмки по складам и опции для товаров.

        warehouse_ids — какие склады смотреть (без них все). goods — список
        {barcode, quantity}: для них WB скажет, на какие склады и каким типом поставки
        принимает. Коэффициент 0 значит бесплатную приёмку, -1 — приёмка недоступна.
        """
        owner = await kit.owner_of(seller)
        coeff_args: dict[str, Any] = {"seller": seller, "fresh": fresh}
        if warehouse_ids:
            coeff_args["warehouseIDs"] = ",".join(str(w) for w in warehouse_ids)
        coefficients = await kit.call("GET", "/api/tariffs/v1/acceptance/coefficients",
                                      coeff_args, tool="wb_fbw_plan", ctx=ctx, compact=False)
        payload: dict[str, Any] = {"coefficients": data_of(coefficients)}
        if goods:
            bad = [g for g in goods if not isinstance(g, dict) or "barcode" not in g
                   or "quantity" not in g]
            if bad:
                raise ToolError("каждый элемент goods — объект с barcode и quantity")
            options_args: dict[str, Any] = {"seller": seller, "body": goods}
            if warehouse_ids and len(warehouse_ids) == 1:
                options_args["warehouseID"] = warehouse_ids[0]
            options = await kit.call("POST", "/api/v1/acceptance/options", options_args,
                                     tool="wb_fbw_plan", ctx=ctx, compact=False)
            payload["options"] = data_of(options)
        return kit.finish(payload, select, tool="wb_fbw_plan",
                          operation=kit.op("GET", "/api/tariffs/v1/acceptance/coefficients").id,
                          seller=owner, key="coefficients")

    server.add_tool(wb_fbw_supplies, annotations=READ)
    server.add_tool(wb_fbw_plan, annotations=READ)
