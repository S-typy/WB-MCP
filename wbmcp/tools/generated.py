"""Сгенерированные инструменты: по одному на операцию выбранных профилей."""

from __future__ import annotations

from typing import Any

from mcp.types import Tool as MCPTool
from mcp.types import ToolAnnotations

from wbmcp.app import AppContext
from wbmcp.catalog import Catalog
from wbmcp.catalog.models import Operation
from wbmcp.tools.confirm import Confirmer, is_dangerous
from wbmcp.tools.runner import run_operation

MAX_DESCRIPTION = 1200


def tool_description(op: Operation) -> str:
    """Описание инструмента: summary, метод и путь, допуск, лимит, заметки."""
    lines = [op.summary or op.id, f"{op.method} {op.path} (раздел {op.section})"]
    if op.token_types:
        lines.append("только для токенов: " + ", ".join(op.token_types))
    if op.read_only is False:
        lines.append("меняет данные кабинета; есть dry_run и force")
    if op.limit:
        row = op.limit.rows[0]
        shared = " (общий bucket группы методов)" if op.limit.key.startswith("group:") else ""
        lines.append(f"лимит: {row.limit} за {row.period_s:g} с, всплеск {row.burst}{shared}")
    if "jam" in op.flags:
        lines.append("нужна подписка Джем")
    if "async-report" in op.flags:
        lines.append("часть асинхронного отчёта: задание, статус, скачивание")
    if op.deprecated:
        lines.append("метод устаревший")
    if is_dangerous(op):
        lines.append("опасное действие: запросит подтверждение")
    text = "\n".join(lines)
    body = op.description.split("Лимит запросов")[0].strip()
    if body:
        text += "\n\n" + body[: max(0, MAX_DESCRIPTION - len(text))]
    return text


def build_tool(catalog: Catalog, op: Operation) -> MCPTool:
    """Описание инструмента MCP для операции."""
    return MCPTool(
        name=op.tool_name,
        title=op.summary or op.id,
        description=tool_description(op),
        input_schema=catalog.input_schema(op),
        annotations=ToolAnnotations(
            read_only_hint=not op.mutating,
            destructive_hint=op.mutating and is_dangerous(op),
            idempotent_hint=not op.mutating,
            open_world_hint=True,
        ),
    )


class GeneratedTools:
    """Набор сгенерированных инструментов по профилям."""

    def __init__(self, app: AppContext, confirmer: Confirmer) -> None:
        """Список строится один раз по профилям из настроек."""
        self._app = app
        self._confirmer = confirmer
        profiles = app.config.server.profiles
        self._ops = {op.tool_name: op for op in app.catalog.for_profiles(profiles)}
        self._tools = [build_tool(app.catalog, op) for op in self._ops.values()]

    def __len__(self) -> int:
        """Сколько инструментов выдаётся."""
        return len(self._tools)

    def list_tools(self) -> list[MCPTool]:
        """Инструменты для list_tools."""
        return list(self._tools)

    def owns(self, name: str) -> bool:
        """Наш ли это инструмент."""
        return name in self._ops

    async def call(self, name: str, arguments: dict[str, Any], ctx: Any) -> dict[str, Any]:
        """Выполнить операцию инструмента."""
        op = self._ops[name]
        return await run_operation(self._app, op, arguments, tool_name=name, ctx=ctx,
                                   confirmer=self._confirmer)
