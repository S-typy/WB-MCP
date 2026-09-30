"""Заказы FBS: новые задания, весь список, статусы, стикеры, поставки."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from wbmcp.tools.curated.kit import READ, Kit, data_of

STICKER_TYPES = ("png", "svg", "zplv", "zplh")
STICKER_SIZES = ((58, 40), (40, 30))
MAX_STATUS_IDS = 1000
MAX_STICKER_IDS = 100


def chunks(items: list[Any], size: int) -> list[list[Any]]:
    """Порезать список на куски не длиннее size."""
    return [items[i:i + size] for i in range(0, len(items), size)]


def register(server: Any, kit: Kit) -> None:
    """Инструменты заказов FBS."""

    async def wb_orders_new(seller: str | None = None, fresh: bool = False,
                            select: str | None = None,
                            ctx: Context | None = None) -> dict[str, Any]:
        """Новые сборочные задания FBS, которые ещё не взяты в работу."""
        return await kit.call("GET", "/api/v3/orders/new",
                              {"seller": seller, "fresh": fresh, "select": select},
                              tool="wb_orders_new", ctx=ctx)

    async def wb_orders(
        seller: str | None = None,
        date_from: int | None = None,
        date_to: int | None = None,
        limit: int = 1000,
        max_pages: int = 20,
        fresh: bool = False,
        select: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Все сборочные задания FBS за период с автоматической пагинацией по next.

        date_from и date_to — unix time в секундах. Страницы собираются в один список;
        select (JMESPath) применяется к нему, большой список уходит в файл с образцом в ответе.
        """
        owner = await kit.owner_of(seller)

        async def fetch(cursor: Any) -> dict[str, Any]:
            arguments: dict[str, Any] = {"seller": seller, "limit": limit, "next": cursor or 0,
                                         "dateFrom": date_from, "dateTo": date_to,
                                         "fresh": fresh}
            return await kit.call("GET", "/api/v3/orders", arguments, tool="wb_orders", ctx=ctx,
                                  compact=False)

        orders, pages, capped = await kit.collect_next(fetch, items_key="orders",
                                                       max_pages=max_pages)
        payload = {"orders": orders, "count": len(orders), "pages": pages, "capped": capped}
        return kit.finish(payload, select, tool="wb_orders",
                          operation=kit.op("GET", "/api/v3/orders").id, seller=owner,
                          key="orders")

    async def wb_order_status(
        order_ids: list[int],
        seller: str | None = None,
        history: bool = False,
        select: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Статусы сборочных заданий FBS по их id: статус продавца и статус WB.

        Список режется на пакеты по 1000 id, ответы склеиваются. history=true вместо
        текущих статусов отдаёт историю смены статусов.
        """
        if not order_ids:
            raise ToolError("order_ids пуст: нужен список id сборочных заданий")
        owner = await kit.owner_of(seller)
        path = "/api/v3/orders/status/history" if history else "/api/v3/orders/status"
        orders: list[Any] = []
        for part in chunks(list(order_ids), MAX_STATUS_IDS):
            result = await kit.call("POST", path, {"seller": seller, "body": {"orders": part}},
                                    tool="wb_order_status", ctx=ctx, compact=False)
            orders.extend(data_of(result).get("orders") or [])
        payload = {"orders": orders, "count": len(orders)}
        return kit.finish(payload, select, tool="wb_order_status",
                          operation=kit.op("POST", path).id, seller=owner, key="orders")

    async def wb_stickers(
        order_ids: list[int],
        seller: str | None = None,
        kind: str = "png",
        width: int = 58,
        height: int = 40,
        select: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Стикеры сборочных заданий FBS: kind — png, svg, zplv или zplh; размер 58x40 или 40x30.

        Файл стикера приходит в поле file в base64. Список режется на пакеты по 100 id.
        """
        if not order_ids:
            raise ToolError("order_ids пуст: нужен список id сборочных заданий")
        if kind not in STICKER_TYPES:
            raise ToolError(f"kind должен быть одним из: {', '.join(STICKER_TYPES)}")
        if (width, height) not in STICKER_SIZES:
            sizes = ", ".join(f"{w}x{h}" for w, h in STICKER_SIZES)
            raise ToolError(f"размер стикера {width}x{height} не поддерживается; есть {sizes}")
        owner = await kit.owner_of(seller)
        stickers: list[Any] = []
        for part in chunks(list(order_ids), MAX_STICKER_IDS):
            result = await kit.call(
                "POST", "/api/v3/orders/stickers",
                {"seller": seller, "body": {"orders": part}, "type": kind, "width": width,
                 "height": height},
                tool="wb_stickers", ctx=ctx, compact=False,
            )
            stickers.extend(data_of(result).get("stickers") or [])
        payload = {"stickers": stickers, "count": len(stickers), "kind": kind,
                   "size": f"{width}x{height}"}
        return kit.finish(payload, select, tool="wb_stickers",
                          operation=kit.op("POST", "/api/v3/orders/stickers").id, seller=owner,
                          key="stickers")

    async def wb_supplies(
        seller: str | None = None,
        limit: int = 1000,
        max_pages: int = 20,
        fresh: bool = False,
        select: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Поставки FBS (список для сборочных заданий) с автоматической пагинацией по next."""
        owner = await kit.owner_of(seller)

        async def fetch(cursor: Any) -> dict[str, Any]:
            return await kit.call("GET", "/api/v3/supplies",
                                  {"seller": seller, "limit": limit, "next": cursor or 0,
                                   "fresh": fresh},
                                  tool="wb_supplies", ctx=ctx, compact=False)

        supplies, pages, capped = await kit.collect_next(fetch, items_key="supplies",
                                                         max_pages=max_pages)
        payload = {"supplies": supplies, "count": len(supplies), "pages": pages,
                   "capped": capped}
        return kit.finish(payload, select, tool="wb_supplies",
                          operation=kit.op("GET", "/api/v3/supplies").id, seller=owner,
                          key="supplies")

    for fn in (wb_orders_new, wb_orders, wb_order_status, wb_stickers, wb_supplies):
        server.add_tool(fn, annotations=READ)
