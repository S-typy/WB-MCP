"""Курируемые инструменты: задачи агента, собранные из нескольких операций.

Здесь нет ничего, чего нельзя сделать через wb_request, но здесь спрятаны
пагинация, ожидание отчётов и формы тел, которые иначе пришлось бы каждый раз
вспоминать заново.
"""

from __future__ import annotations

import uuid
from typing import Any

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from wbmcp.app import AppContext
from wbmcp.auth import AccessDeniedError
from wbmcp.client.paging import collect_pages
from wbmcp.client.reports import ReportFailedError, ReportTimeoutError, wait_until
from wbmcp.sellers import UnknownSellerError
from wbmcp.tools.confirm import Confirmer
from wbmcp.tools.runner import finish_payload, run_operation

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, open_world_hint=True)

TASK_REPORTS = {
    "warehouse_remains": "/api/v1/warehouse_remains",
    "paid_storage": "/api/v1/paid_storage",
    "acceptance_report": "/api/v1/acceptance_report",
}
DONE_STATUSES = {"done", "success", "succeeded", "ready"}
FAILED_STATUSES = {"error", "fail", "failed", "canceled", "cancelled"}


def _status_of(response: dict[str, Any]) -> str:
    data = response.get("data") or {}
    if isinstance(data, list):
        data = data[0] if data else {}
    return str(data.get("status") or "").lower()


def register(server: Any, app: AppContext, confirmer: Confirmer) -> None:
    """Зарегистрировать курируемые инструменты."""

    async def call(op_method: str, op_path: str, arguments: dict[str, Any], *, tool: str,
                   ctx: Any, path_params: dict[str, Any] | None = None,
                   priority: int = 0, compact: bool = True) -> dict[str, Any]:
        op = app.catalog.by_route(op_method, op_path)
        return await run_operation(app, op, arguments, tool_name=tool, ctx=ctx,
                                   confirmer=confirmer, path_params=path_params,
                                   priority=priority, compact=compact)

    async def owner_of(seller: str | None) -> str:
        """Алиас кабинета для владельца выгрузки: тот же, что разрешит run_operation."""
        try:
            return (await app.seller_for(seller, app.principal())).alias
        except (UnknownSellerError, AccessDeniedError) as exc:
            raise ToolError(str(exc)) from exc

    async def wb_orders_new(seller: str | None = None, fresh: bool = False,
                            select: str | None = None,
                            ctx: Context | None = None) -> dict[str, Any]:
        """Новые сборочные задания FBS, которые ещё не взяты в работу."""
        return await call("GET", "/api/v3/orders/new",
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
        owner = await owner_of(seller)
        orders_op = app.catalog.by_route("GET", "/api/v3/orders")

        async def fetch(cursor: Any) -> dict[str, Any]:
            arguments: dict[str, Any] = {"seller": seller, "limit": limit, "next": cursor or 0,
                                         "dateFrom": date_from, "dateTo": date_to,
                                         "fresh": fresh}
            return await call("GET", "/api/v3/orders", arguments, tool="wb_orders", ctx=ctx,
                              compact=False)

        orders, pages, capped = await collect_pages(
            fetch,
            items_of=lambda r: (r.get("data") or {}).get("orders") or [],
            next_of=lambda r: (r.get("data") or {}).get("next") or None,
            max_pages=max_pages,
        )
        payload = {"orders": orders, "count": len(orders), "pages": pages, "capped": capped}
        return finish_payload(app, payload, {"select": select}, tool_name="wb_orders",
                              operation=orders_op.id, seller=owner, key="orders")

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
        owner = await owner_of(seller)
        cards_op = app.catalog.by_route("POST", "/content/v2/get/cards/list")

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
            return await call("POST", "/content/v2/get/cards/list", arguments, tool="wb_cards",
                              ctx=ctx, compact=False)

        def next_of(response: dict[str, Any]) -> Any:
            data = response.get("data") or {}
            cursor = data.get("cursor") or {}
            if cursor.get("total", 0) < limit:
                return None
            return {"updatedAt": cursor.get("updatedAt"), "nmID": cursor.get("nmID")}

        cards, pages, capped = await collect_pages(
            fetch, items_of=lambda r: (r.get("data") or {}).get("cards") or [],
            next_of=next_of, max_pages=max_pages,
        )
        payload = {"cards": cards, "count": len(cards), "pages": pages, "capped": capped}
        return finish_payload(app, payload, {"select": select}, tool_name="wb_cards",
                              operation=cards_op.id, seller=owner, key="cards")

    async def wb_feedbacks(
        seller: str | None = None,
        is_answered: bool = False,
        take: int = 100,
        skip: int = 0,
        nm_id: int | None = None,
        order: str | None = None,
        date_from: int | None = None,
        fresh: bool = False,
        select: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Отзывы покупателей: по умолчанию неотвеченные."""
        arguments: dict[str, Any] = {"seller": seller, "isAnswered": is_answered, "take": take,
                                     "skip": skip, "nmId": nm_id, "order": order,
                                     "dateFrom": date_from, "fresh": fresh, "select": select}
        return await call("GET", "/api/v1/feedbacks", arguments, tool="wb_feedbacks", ctx=ctx)

    async def wb_warehouses(seller: str | None = None, fresh: bool = False,
                            select: str | None = None,
                            ctx: Context | None = None) -> dict[str, Any]:
        """Склады продавца (для остатков FBS); ответ кэшируется на час."""
        return await call("GET", "/api/v3/warehouses",
                          {"seller": seller, "fresh": fresh, "select": select},
                          tool="wb_warehouses", ctx=ctx)

    async def wb_prices_set(
        seller: str | None = None,
        data: list[dict[str, Any]] | None = None,
        dry_run: bool = False,
        force: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Загрузить цены и скидки: data — список {nmID, price, discount}."""
        if not data:
            raise ToolError("data пуст: нужен список объектов nmID, price, discount")
        arguments = {"seller": seller, "body": {"data": data}, "dry_run": dry_run,
                     "force": force}
        return await call("POST", "/api/v2/upload/task", arguments, tool="wb_prices_set",
                          ctx=ctx)

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
        return await call("PUT", "/api/v3/stocks/{warehouseId}", arguments,
                          tool="wb_stocks_set", ctx=ctx,
                          path_params={"warehouseId": warehouse_id})

    async def wb_report(
        kind: str,
        seller: str | None = None,
        params: dict[str, Any] | None = None,
        wait: bool = True,
        timeout_s: float = 600.0,
        poll_interval_s: float = 5.0,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Асинхронный отчёт целиком: создать задание, дождаться, скачать.

        kind: warehouse_remains, paid_storage, acceptance_report (params — параметры
        запроса, например dateFrom/dateTo) или nm_report (params — тело запроса
        POST /api/v2/nm-report/downloads; id можно не задавать). Файлы отчётов
        сохраняются на диск, путь в ответе.
        """
        params = dict(params or {})
        if kind in TASK_REPORTS:
            base = TASK_REPORTS[kind]
            created = await call("GET", base, {"seller": seller, **params}, tool="wb_report",
                                 ctx=ctx)
            task_id = (created.get("data") or {}).get("data", {}).get("taskId")
            if not task_id:
                raise ToolError(f"WB не вернул taskId: {created.get('data')}")
            if not wait:
                return {"kind": kind, "task_id": task_id, "created": created}

            async def poll() -> dict[str, Any]:
                result = await call("GET", base + "/tasks/{task_id}/status", {"seller": seller},
                                    tool="wb_report", ctx=ctx, path_params={"task_id": task_id},
                                    priority=1)
                return result.get("data") or {}

            try:
                status = await wait_until(
                    poll, done=lambda r: _status_of(r) in DONE_STATUSES,
                    failed=lambda r: _status_of(r) if _status_of(r) in FAILED_STATUSES else None,
                    interval_s=poll_interval_s, timeout_s=timeout_s,
                )
            except (ReportFailedError, ReportTimeoutError) as exc:
                raise ToolError(f"{exc} (task_id {task_id})") from exc
            download = await call("GET", base + "/tasks/{task_id}/download", {"seller": seller},
                                  tool="wb_report", ctx=ctx, path_params={"task_id": task_id})
            return {"kind": kind, "task_id": task_id, "status": status, "download": download}
        if kind == "nm_report":
            report_id = params.get("id") or str(uuid.uuid4())
            params["id"] = report_id
            created = await call("POST", "/api/v2/nm-report/downloads",
                                 {"seller": seller, "body": params}, tool="wb_report", ctx=ctx)
            if not wait:
                return {"kind": kind, "download_id": report_id, "created": created}

            async def poll_list() -> dict[str, Any]:
                result = await call("GET", "/api/v2/nm-report/downloads",
                                    {"seller": seller, "filter[downloadIds]": [report_id]},
                                    tool="wb_report", ctx=ctx, priority=1)
                return result.get("data") or {}

            try:
                status = await wait_until(
                    poll_list, done=lambda r: _status_of(r) in DONE_STATUSES,
                    failed=lambda r: _status_of(r) if _status_of(r) in FAILED_STATUSES else None,
                    interval_s=poll_interval_s, timeout_s=timeout_s,
                )
            except (ReportFailedError, ReportTimeoutError) as exc:
                raise ToolError(f"{exc} (download_id {report_id})") from exc
            download = await call("GET", "/api/v2/nm-report/downloads/file/{downloadId}",
                                  {"seller": seller}, tool="wb_report", ctx=ctx,
                                  path_params={"downloadId": report_id})
            return {"kind": kind, "download_id": report_id, "status": status,
                    "download": download}
        raise ToolError(f"неизвестный вид отчёта {kind!r}; есть: "
                        f"{', '.join([*TASK_REPORTS, 'nm_report'])}")

    for fn, hints in (
        (wb_orders_new, READ), (wb_orders, READ), (wb_cards, READ), (wb_feedbacks, READ),
        (wb_warehouses, READ), (wb_prices_set, WRITE), (wb_stocks_set, WRITE),
        (wb_report, READ),
    ):
        server.add_tool(fn, annotations=hints)
