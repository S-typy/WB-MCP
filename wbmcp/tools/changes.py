"""Новости WB API, сопоставленные с каталогом: какие наши операции затронуты."""

from __future__ import annotations

import re
from typing import Any

from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations

from wbmcp.app import AppContext
from wbmcp.catalog import Catalog, UnknownOperationError
from wbmcp.catalog.models import Operation
from wbmcp.tools.confirm import Confirmer
from wbmcp.tools.runner import run_operation

_PATH = re.compile(r"/(?:api|content|adv|public|ping)(?:/[A-Za-z0-9_.{}%-]+)*")
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_API_WORD = re.compile(r"\bAPI\b|\bАПИ\b|dev\.wildberries\.ru|openapi", re.I)
_TAGS = re.compile(r"<[^>]+>")


def _excerpt(text: str, width: int = 240) -> str:
    """Кусок текста вокруг первого упоминания API, без HTML."""
    plain = re.sub(r"\s+", " ", _TAGS.sub(" ", text)).strip()
    match = _API_WORD.search(plain)
    start = max(0, (match.start() if match else 0) - width // 2)
    return plain[start:start + width]


def collect_strings(value: Any) -> list[str]:
    """Все строки из вложенной структуры JSON."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in collect_strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in collect_strings(v)]
    return []


def match_operations(catalog: Catalog, text: str) -> list[Operation]:
    """Операции каталога, пути которых упомянуты в тексте."""
    found: dict[str, Operation] = {}
    for candidate in _PATH.findall(text):
        path = candidate.rstrip(".,;:)")
        for method in _METHODS:
            try:
                op = catalog.match_route(method, path)
            except UnknownOperationError:
                continue
            found.setdefault(op.id, op)
    return list(found.values())


def news_items(data: Any) -> list[dict[str, Any]]:
    """Список новостей из ответа WB, какой бы обёрткой он ни был."""
    if isinstance(data, dict):
        for key in ("data", "news", "items"):
            if isinstance(data.get(key), list):
                return [i for i in data[key] if isinstance(i, dict)]
        return [data] if data else []
    if isinstance(data, list):
        return [i for i in data if isinstance(i, dict)]
    return []


def register(server: Any, app: AppContext, confirmer: Confirmer) -> None:
    """Зарегистрировать инструмент wb_changes."""

    async def wb_changes(seller: str | None = None, from_date: str | None = None,
                         ctx: Context | None = None) -> dict[str, Any]:
        """Новости WB с привязкой к операциям каталога.

        Лента /api/communications/v2/news — общие новости продавца, про API там пишут редко.
        Ответ делится на три части: новости с путями методов из снимка, новости с упоминанием
        API без путей и остальные (только id и дата). Полный ответ WB в поле raw.
        """
        op = app.catalog.by_route("GET", "/api/communications/v2/news")
        from wbmcp.tools.core import default_news_from

        arguments: dict[str, Any] = {"seller": seller, "from": from_date or default_news_from()}
        result = await run_operation(app, op, arguments, tool_name="wb_changes", ctx=ctx,
                                     confirmer=confirmer)
        items = news_items(result.get("data"))
        matched: list[dict[str, Any]] = []
        api_mentions: list[dict[str, Any]] = []
        other: list[dict[str, Any]] = []
        for item in items:
            text = " ".join(collect_strings(item))
            ops = match_operations(app.catalog, text)
            card = {k: item.get(k) for k in ("id", "title", "date", "publishDate") if k in item}
            if ops:
                card["operations"] = [
                    {"id": o.id, "method": o.method, "path": o.path, "section": o.section,
                     "deprecated": o.deprecated} for o in ops
                ]
                matched.append(card)
            elif _API_WORD.search(text):
                card["excerpt"] = _excerpt(text)
                api_mentions.append(card)
            else:
                other.append(card)
        return {
            "snapshot": app.catalog.snapshot,
            "items_total": len(items),
            "matched": matched,
            "api_mentions": api_mentions,
            "other": other,
            "raw": result.get("data"),
        }

    server.add_tool(wb_changes, annotations=ToolAnnotations(read_only_hint=True,
                                                            open_world_hint=True))
