"""Автопагинация: у WB три схемы — next, cursor-объект и offset/limit."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


async def collect_pages(
    fetch: Callable[[Any], Awaitable[Any]],
    *,
    items_of: Callable[[Any], list[Any]],
    next_of: Callable[[Any], Any],
    max_pages: int = 50,
    start: Any = None,
) -> tuple[list[Any], int, bool]:
    """Собрать элементы всех страниц.

    ``fetch(cursor)`` возвращает ответ, ``items_of`` достаёт из него элементы,
    ``next_of`` — курсор следующей страницы или None. Возвращает элементы,
    число страниц и признак, что упёрлись в max_pages.
    """
    items: list[Any] = []
    cursor = start
    pages = 0
    while pages < max_pages:
        response = await fetch(cursor)
        pages += 1
        page_items = items_of(response)
        items.extend(page_items)
        cursor = next_of(response)
        if cursor is None or not page_items:
            return items, pages, False
    return items, pages, True
