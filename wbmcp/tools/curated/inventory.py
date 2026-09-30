"""Склады, цены и остатки FBS."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from wbmcp.client.reports import ReportFailedError, ReportTimeoutError, wait_until
from wbmcp.tools.curated.kit import READ, WRITE, Kit, data_of

# Статусы загрузки цен из истории: 3 — без ошибок, 4 — отменена, 5 и 6 — есть ошибки в товарах.
UPLOAD_DONE = {3, 5, 6}
UPLOAD_CANCELLED = 4
UPLOAD_WITH_ERRORS = {5, 6}
GOODS_PAGE = 1000


def register(server: Any, kit: Kit) -> None:
    """Инструменты складов, цен и остатков."""

    async def wb_warehouses(seller: str | None = None, fresh: bool = False,
                            select: str | None = None,
                            ctx: Context | None = None) -> dict[str, Any]:
        """Склады продавца (для остатков FBS); ответ кэшируется на час."""
        return await kit.call("GET", "/api/v3/warehouses",
                              {"seller": seller, "fresh": fresh, "select": select},
                              tool="wb_warehouses", ctx=ctx)

    async def upload_outcome(seller: str | None, upload_id: int, ctx: Any,
                             timeout_s: float, poll_interval_s: float) -> dict[str, Any]:
        """Дождаться обработки загрузки цен и собрать ошибки по товарам, если они есть."""

        async def poll() -> dict[str, Any]:
            try:
                buffered = await kit.call("GET", "/api/v2/buffer/tasks",
                                          {"seller": seller, "uploadID": upload_id},
                                          tool="wb_prices_set", ctx=ctx, priority=1)
            except ToolError:
                buffered = {}   # в буфере уже нет, значит смотрим историю
            if (data_of(buffered).get("data") or {}).get("status") == 1:
                return {"pending": True}
            history = await kit.call("GET", "/api/v2/history/tasks",
                                     {"seller": seller, "uploadID": upload_id},
                                     tool="wb_prices_set", ctx=ctx, priority=1)
            return data_of(history).get("data") or {}

        def status_of(state: dict[str, Any]) -> Any:
            return state.get("status")

        try:
            state = await wait_until(
                poll, done=lambda s: status_of(s) in UPLOAD_DONE,
                failed=lambda s: "отменена" if status_of(s) == UPLOAD_CANCELLED else None,
                interval_s=poll_interval_s, timeout_s=timeout_s,
            )
        except (ReportFailedError, ReportTimeoutError) as exc:
            raise ToolError(f"{exc} (uploadID {upload_id})") from exc
        outcome: dict[str, Any] = {"upload_id": upload_id, "status": state}
        if status_of(state) in UPLOAD_WITH_ERRORS:
            goods: list[Any] = []

            async def fetch(offset: int) -> dict[str, Any]:
                return await kit.call("GET", "/api/v2/history/goods/task",
                                      {"seller": seller, "uploadID": upload_id,
                                       "limit": GOODS_PAGE, "offset": offset},
                                      tool="wb_prices_set", ctx=ctx, compact=False)

            goods, _, _ = await kit.collect_offset(
                fetch, items_of=lambda r: (data_of(r).get("data") or {}).get("historyGoods")
                or [], limit=GOODS_PAGE, max_pages=20,
            )
            outcome["goods_with_errors"] = [g for g in goods if g.get("errorText")]
        return outcome

    async def wb_prices_set(
        seller: str | None = None,
        data: list[dict[str, Any]] | None = None,
        wait: bool = True,
        timeout_s: float = 300.0,
        poll_interval_s: float = 5.0,
        dry_run: bool = False,
        force: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Загрузить цены и скидки: data — список {nmID, price, discount}.

        WB обрабатывает загрузку асинхронно. С wait=true инструмент ждёт итог и, если у части
        товаров ошибки, возвращает их список; wait=false отдаёт только id загрузки.
        """
        if not data:
            raise ToolError("data пуст: нужен список объектов nmID, price, discount")
        arguments = {"seller": seller, "body": {"data": data}, "dry_run": dry_run,
                     "force": force}
        created = await kit.call("POST", "/api/v2/upload/task", arguments, tool="wb_prices_set",
                                 ctx=ctx)
        if dry_run or not wait:
            return created
        upload_id = (data_of(created).get("data") or {}).get("id")
        if not upload_id:
            return created
        outcome = await upload_outcome(seller, upload_id, ctx, timeout_s, poll_interval_s)
        return {**created, "outcome": outcome}

    async def wb_stocks_set(
        warehouse_id: int,
        seller: str | None = None,
        stocks: list[dict[str, Any]] | None = None,
        dry_run: bool = False,
        force: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Обновить остатки на складе продавца: stocks — список {sku, amount}."""
        if not stocks:
            raise ToolError("stocks пуст: нужен список объектов sku, amount")
        arguments = {"seller": seller, "body": {"stocks": stocks}, "dry_run": dry_run,
                     "force": force}
        return await kit.call("PUT", "/api/v3/stocks/{warehouseId}", arguments,
                              tool="wb_stocks_set", ctx=ctx,
                              path_params={"warehouseId": warehouse_id})

    server.add_tool(wb_warehouses, annotations=READ)
    server.add_tool(wb_prices_set, annotations=WRITE)
    server.add_tool(wb_stocks_set, annotations=WRITE)
