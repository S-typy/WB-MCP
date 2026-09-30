"""Пробный прогон: несколько безопасных вызовов, чтобы проверить токен и лимиты."""

from __future__ import annotations

import json
from typing import Any

from wbmcp.app import AppContext
from wbmcp.server import WbMcpServer
from wbmcp.tools.confirm import AutoConfirmer

STEPS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("wb_sellers", {}),
    ("wb_ping", {}),
    ("wb_seller_info", {}),
    ("wb_news", {}),
    ("wb_quota", {}),
)
# В песочнице нет common-api: справка о продавце и новости заменяются складами и заказами.
SANDBOX_STEPS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("wb_sellers", {}),
    ("wb_ping", {}),
    ("wb_warehouses", {}),
    ("wb_orders_new", {}),
    ("wb_quota", {}),
)


async def run_smoke(app: AppContext, seller: str | None = None, *, printer=print) -> int:
    """Выполнить шаги по очереди; код 0, если все прошли."""
    server = WbMcpServer(app, confirmer=AutoConfirmer(False))
    failures = 0
    chosen = await app.sellers.resolve(seller)
    steps = SANDBOX_STEPS if chosen.info.sandbox else STEPS
    if chosen.info.sandbox:
        printer("токен тестовый: шаги для песочницы, справка о продавце и новости пропущены")
    for name, arguments in steps:
        call_args = dict(arguments)
        if seller and name != "wb_sellers":
            call_args["seller"] = seller
        try:
            result = await server.call_tool(name, call_args)
            payload = result.structured_content
            text = json.dumps(payload, ensure_ascii=False, default=str)
            printer(f"OK   {name}: {text[:400]}")
        except Exception as exc:  # noqa: BLE001 - отчёт нужен по каждому шагу
            failures += 1
            printer(f"FAIL {name}: {exc}")
    await app.aclose()
    printer(f"итого: {len(steps) - failures} из {len(steps)} шагов прошли")
    return 0 if failures == 0 else 1
