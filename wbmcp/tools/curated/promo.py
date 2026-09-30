"""Реклама: список кампаний и статистика за период."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from wbmcp.tools.curated.kit import READ, Kit, data_of
from wbmcp.tools.curated.orders import chunks

MAX_IDS_PER_CALL = 50
DEFAULT_DAYS = 7


def register(server: Any, kit: Kit) -> None:
    """Инструменты рекламы."""

    async def wb_adverts(
        seller: str | None = None,
        ids: list[int] | None = None,
        statuses: list[int] | None = None,
        payment_type: str | None = None,
        fresh: bool = False,
        select: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Рекламные кампании кабинета: без фильтров все, иначе по id, статусам или типу оплаты.

        Статусы WB: 4 готова к запуску, 7 завершена, 8 отказался, 9 идут показы, 11 на паузе.
        payment_type — cpm или cpc.
        """
        arguments: dict[str, Any] = {"seller": seller, "fresh": fresh, "select": select}
        if ids:
            arguments["ids"] = ",".join(str(i) for i in ids)
        if statuses:
            arguments["statuses"] = ",".join(str(s) for s in statuses)
        if payment_type:
            arguments["payment_type"] = payment_type
        return await kit.call("GET", "/api/advert/v2/adverts", arguments, tool="wb_adverts",
                              ctx=ctx)

    async def wb_adverts_stats(
        ids: list[int],
        seller: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        select: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Статистика кампаний за период по дням: показы, клики, CTR, затраты, заказы.

        Даты в формате YYYY-MM-DD, по умолчанию последняя неделя. Список id режется
        на пакеты по 50, ответы склеиваются в один список.
        """
        if not ids:
            raise ToolError("ids пуст: нужен список id кампаний")
        today = datetime.now(UTC).date()
        end = date_to or today.isoformat()
        begin = date_from or (today - timedelta(days=DEFAULT_DAYS)).isoformat()
        owner = await kit.owner_of(seller)
        stats: list[Any] = []
        for part in chunks(list(ids), MAX_IDS_PER_CALL):
            result = await kit.call(
                "GET", "/adv/v3/fullstats",
                {"seller": seller, "ids": ",".join(str(i) for i in part), "beginDate": begin,
                 "endDate": end},
                tool="wb_adverts_stats", ctx=ctx, compact=False,
            )
            data = data_of(result)
            stats.extend(data if isinstance(data, list) else [])
        payload = {"stats": stats, "count": len(stats), "date_from": begin, "date_to": end}
        return kit.finish(payload, select, tool="wb_adverts_stats",
                          operation=kit.op("GET", "/adv/v3/fullstats").id, seller=owner,
                          key="stats")

    server.add_tool(wb_adverts, annotations=READ)
    server.add_tool(wb_adverts_stats, annotations=READ)
