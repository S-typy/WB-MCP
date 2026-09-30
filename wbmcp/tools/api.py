"""Инструменты работы с каталогом API: поиск, описание, сырой вызов."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from wbmcp.app import AppContext
from wbmcp.catalog import UnknownOperationError
from wbmcp.tools.confirm import Confirmer
from wbmcp.tools.runner import extract_path_params, run_operation

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)


def register(server: Any, app: AppContext, confirmer: Confirmer) -> None:
    """Зарегистрировать инструменты каталога на сервере."""

    async def wb_api_search(
        query: str = "",
        section: str | None = None,
        category: str | None = None,
        method: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Найти операции WB API по словам из названия, пути, тегов или описания.

        Пустой query с section выдаёт весь раздел. Разделы: orders-fbs, orders-dbw, dbs,
        in-store-pickup, orders-fbw, item-management, promotion, analytics, reports,
        customer-communication, documents-and-accounting, api-information, rates.
        """
        hits = app.catalog.search(query, section=section, category=category, method=method,
                                  limit=limit)
        return {
            "snapshot": app.catalog.snapshot,
            "total_operations": len(app.catalog),
            "sections": app.catalog.sections(),
            "found": [op.brief() for op in hits],
        }

    async def wb_api_describe(operation: str) -> dict[str, Any]:
        """Полная карточка операции: допуск, лимиты, параметры, тело, ответы, схема аргументов.

        operation — operationId, имя инструмента wb_op_<id> или «METHOD /path».
        """
        try:
            if " " in operation.strip():
                method, path = operation.split(None, 1)
                op = app.catalog.match_route(method, path.strip())
            else:
                op = app.catalog.get(operation.strip())
        except UnknownOperationError as exc:
            raise ToolError(str(exc)) from exc
        return app.catalog.describe(op.id)

    async def wb_request(
        method: str,
        path: str,
        seller: str | None = None,
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        body: Any = None,
        dry_run: bool = False,
        force: bool = False,
        fresh: bool = False,
        select: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Вызвать любой метод WB API по методу и пути с проверкой допуска и лимитами.

        Путь — реальный, с подставленными параметрами (например
        /api/v2/nm-report/downloads/file/123). Операция ищется в каталоге, поэтому
        неизвестный путь отклоняется. Для записей доступны dry_run и force, для чтений
        с настроенным кэшем — fresh (запросить WB заново). select — проекция ответа
        выражением JMESPath. Ответ больше порога уходит в файл целиком, в ответе остаётся
        образец и id для wb_result.
        """
        try:
            op = app.catalog.match_route(method, path.strip())
        except UnknownOperationError as exc:
            raise ToolError(str(exc)) from exc
        known_query = {p.name for p in op.query_params}
        arguments: dict[str, Any] = {"seller": seller, "body": body, "dry_run": dry_run,
                                     "force": force, "fresh": fresh, "select": select}
        # query и headers идут в запрос отдельно: имя вроде dry_run внутри них — это
        # параметр WB, а не наш флаг, подменить управление им нельзя.
        unknown_query = sorted(set(query or {}) - known_query)
        result = await run_operation(
            app, op, arguments, tool_name="wb_request", ctx=ctx, confirmer=confirmer,
            path_params=extract_path_params(op.path, path.strip()),
            query=query, headers=headers,
        )
        if unknown_query:
            result.setdefault("notes", []).append(
                f"параметры запроса не описаны в спецификации: {unknown_query}"
            )
        return result

    server.add_tool(wb_api_search, annotations=READ_ONLY)
    server.add_tool(wb_api_describe, annotations=READ_ONLY)
    server.add_tool(
        wb_request,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                    open_world_hint=True),
    )
