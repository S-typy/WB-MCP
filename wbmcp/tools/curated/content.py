"""Карточки товаров: список с пагинацией и редактирование."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from wbmcp.client.paging import collect_pages
from wbmcp.tools.curated.kit import READ, WRITE, Kit, data_of

MAX_CARDS_PER_UPDATE = 3000


def register(server: Any, kit: Kit) -> None:
    """Инструменты карточек."""

    async def wb_cards(
        seller: str | None = None,
        text_search: str | None = None,
        with_photo: int = -1,
        limit: int = 100,
        max_pages: int = 10,
        locale: str | None = None,
        fresh: bool = False,
        select: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Карточки товаров с автоматической пагинацией по курсору updatedAt/nmID.

        Страницы кэшируются (по умолчанию на 5 минут), повторный запрос всего каталога
        не тратит квоту; fresh=true запрашивает WB заново.
        """
        owner = await kit.owner_of(seller)

        async def fetch(cursor: Any) -> dict[str, Any]:
            body_cursor: dict[str, Any] = {"limit": limit}
            if cursor:
                body_cursor.update(cursor)
            settings: dict[str, Any] = {"cursor": body_cursor, "filter": {"withPhoto": with_photo}}
            if text_search:
                settings["filter"]["textSearch"] = text_search
            arguments: dict[str, Any] = {"seller": seller, "body": {"settings": settings},
                                         "fresh": fresh}
            if locale:
                arguments["locale"] = locale
            return await kit.call("POST", "/content/v2/get/cards/list", arguments,
                                  tool="wb_cards", ctx=ctx, compact=False)

        def next_of(response: dict[str, Any]) -> Any:
            cursor = data_of(response).get("cursor") or {}
            if cursor.get("total", 0) < limit:
                return None
            return {"updatedAt": cursor.get("updatedAt"), "nmID": cursor.get("nmID")}

        cards, pages, capped = await collect_pages(
            fetch, items_of=lambda r: data_of(r).get("cards") or [], next_of=next_of,
            max_pages=max_pages,
        )
        payload = {"cards": cards, "count": len(cards), "pages": pages, "capped": capped}
        return kit.finish(payload, select, tool="wb_cards",
                          operation=kit.op("POST", "/content/v2/get/cards/list").id,
                          seller=owner, key="cards")

    async def wb_card_update(
        cards: list[dict[str, Any]],
        seller: str | None = None,
        dry_run: bool = False,
        force: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Редактировать карточки товаров: cards — полные карточки, как их отдаёт wb_cards.

        WB заменяет карточку целиком, поэтому передавайте все поля, а не только изменённые
        (nmID, vendorCode, brand, title, description, dimensions, characteristics, sizes).
        До 3000 карточек за вызов; dry_run показывает запрос без отправки.
        """
        if not cards:
            raise ToolError("cards пуст: нужен список карточек с nmID и vendorCode")
        if len(cards) > MAX_CARDS_PER_UPDATE:
            raise ToolError(f"за один вызов можно изменить не больше {MAX_CARDS_PER_UPDATE} "
                            f"карточек, получено {len(cards)}")
        missing = [i for i, card in enumerate(cards)
                   if not isinstance(card, dict) or "nmID" not in card]
        if missing:
            raise ToolError(f"у карточек с индексами {missing[:10]} нет nmID")
        return await kit.call("POST", "/content/v2/cards/update",
                              {"seller": seller, "body": cards, "dry_run": dry_run,
                               "force": force},
                              tool="wb_card_update", ctx=ctx)

    server.add_tool(wb_cards, annotations=READ)
    server.add_tool(wb_card_update, annotations=WRITE)
