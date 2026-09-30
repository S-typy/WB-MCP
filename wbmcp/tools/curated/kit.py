"""Общий набор для курируемых инструментов: вызов операции, владелец выгрузки, страницы."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from wbmcp.app import AppContext
from wbmcp.auth import AccessDeniedError
from wbmcp.catalog.models import Operation
from wbmcp.client.paging import collect_pages
from wbmcp.sellers import UnknownSellerError
from wbmcp.tools.confirm import Confirmer
from wbmcp.tools.runner import finish_payload, run_operation

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, open_world_hint=True)


def data_of(response: dict[str, Any]) -> Any:
    """Поле ``data`` ответа WB или пустой словарь."""
    return response.get("data") or {}


class Kit:
    """Всё, что нужно инструменту: приложение, подтверждения и пара удобных обёрток."""

    def __init__(self, app: AppContext, confirmer: Confirmer) -> None:
        """Инструменты держат ссылку на набор и зовут его методы."""
        self.app = app
        self.confirmer = confirmer

    def op(self, method: str, path: str) -> Operation:
        """Операция каталога по маршруту."""
        return self.app.catalog.by_route(method, path)

    async def call(self, method: str, path: str, arguments: dict[str, Any], *, tool: str,
                   ctx: Any, path_params: dict[str, Any] | None = None, priority: int = 0,
                   compact: bool = True) -> dict[str, Any]:
        """Вызвать операцию по маршруту со всеми проверками runner-а."""
        return await run_operation(self.app, self.op(method, path), arguments, tool_name=tool,
                                   ctx=ctx, confirmer=self.confirmer, path_params=path_params,
                                   priority=priority, compact=compact)

    async def owner_of(self, seller: str | None) -> str:
        """Алиас кабинета для владельца выгрузки: тот же, что разрешит run_operation."""
        try:
            return (await self.app.seller_for(seller, self.app.principal())).alias
        except (UnknownSellerError, AccessDeniedError) as exc:
            raise ToolError(str(exc)) from exc

    def finish(self, payload: dict[str, Any], select: str | None, *, tool: str,
               operation: str, seller: str, key: str) -> dict[str, Any]:
        """Проекция select и вынос большого списка в файл."""
        return finish_payload(self.app, payload, {"select": select}, tool_name=tool,
                              operation=operation, seller=seller, key=key)

    async def collect_next(self, fetch: Callable[[Any], Awaitable[dict[str, Any]]], *,
                           items_key: str, next_key: str = "next", max_pages: int,
                           start: Any = 0) -> tuple[list[Any], int, bool]:
        """Страницы по схеме ``next``: WB отдаёт курсор следующей страницы в ответе."""
        return await collect_pages(
            fetch,
            items_of=lambda r: data_of(r).get(items_key) or [],
            next_of=lambda r: data_of(r).get(next_key) or None,
            max_pages=max_pages, start=start,
        )

    async def collect_offset(self, fetch: Callable[[int], Awaitable[dict[str, Any]]], *,
                             items_of: Callable[[dict[str, Any]], list[Any]], limit: int,
                             max_pages: int, start: int = 0) -> tuple[list[Any], int, bool]:
        """Страницы по схеме offset/limit (или skip/take): конец, когда пришло меньше limit."""
        items: list[Any] = []
        offset = start
        pages = 0
        while pages < max_pages:
            response = await fetch(offset)
            pages += 1
            page = items_of(response)
            items.extend(page)
            if len(page) < limit:
                return items, pages, False
            offset += limit
        return items, pages, True
