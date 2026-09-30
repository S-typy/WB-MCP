"""Разбор таблиц лимитов из описаний операций.

В описании каждой операции есть блок ``description_limit`` с Markdown-таблицей.
Форм две: одна строка ``Период | Лимит | Интервал | Всплеск`` для всех токенов
или таблица с колонкой ``Тип`` и строкой на каждый тип токена. Текст перед
двоеточием говорит, общий ли это bucket на группу методов.
"""

from __future__ import annotations

import re

from wbmcp.catalog.models import LimitRow, LimitRule
from wbmcp.catalog.text import normalize_scope, strip_html

PER_ACCOUNT = "на один аккаунт продавца"

_SCOPE = re.compile(r"Лимит запросов(?:</a>)?(.*?):", re.S)
_ROW = re.compile(r"^\|(.+)\|\s*$", re.M)
_COST = re.compile(r"4XX(?:</code>)?\s*учитывается как\s*(\d+)")
_DURATION = re.compile(r"(\d+(?:[.,]\d+)?)\s*(мс|миллисекунд\w*|сек\w*|с|мин\w*|ч|час\w*)")
_COUNT = re.compile(r"(\d+)")

_UNIT_SECONDS = {
    "мс": 0.001,
    "с": 1.0,
    "мин": 60.0,
    "ч": 3600.0,
}


class LimitParseError(ValueError):
    """Таблица лимитов есть, но не разобрана."""


def parse_duration(text: str) -> float:
    """«200 мс», «1 мин», «3 ч» -> секунды."""
    match = _DURATION.search(text)
    if not match:
        raise LimitParseError(f"не разобрана длительность: {text!r}")
    value = float(match.group(1).replace(",", "."))
    unit = match.group(2)
    for prefix, seconds in (("мс", 0.001), ("миллисекунд", 0.001), ("мин", 60.0),
                            ("час", 3600.0), ("ч", 3600.0), ("сек", 1.0), ("с", 1.0)):
        if unit.startswith(prefix):
            return value * seconds
    raise LimitParseError(f"неизвестная единица: {unit!r}")  # pragma: no cover


def parse_count(text: str) -> int:
    """«300 запросов» -> 300."""
    match = _COUNT.search(text)
    if not match:
        raise LimitParseError(f"не разобрано число: {text!r}")
    return int(match.group(1))


def _rows_from_table(cells_rows: list[list[str]]) -> list[LimitRow]:
    header = [c.lower() for c in cells_rows[0]]
    typed = header[0].startswith("тип")
    rows: list[LimitRow] = []
    for cells in cells_rows[1:]:
        if not cells or set("".join(cells)) <= set("-: "):
            continue  # разделитель |---|
        if typed:
            if len(cells) < 5:
                continue
            token_type = cells[0].strip().lower()
            period, limit, interval, burst = cells[1:5]
        else:
            if len(cells) < 4:
                continue
            token_type = None
            period, limit, interval, burst = cells[:4]
        rows.append(
            LimitRow(
                token_type=token_type,
                period_s=parse_duration(period),
                limit=parse_count(limit),
                interval_s=parse_duration(interval),
                burst=parse_count(burst),
            )
        )
    return rows


def parse_limit(description: str, operation_id: str) -> LimitRule | None:
    """Правило лимита из описания операции или None, если таблицы нет."""
    if not description or "Лимит запросов" not in description:
        return None
    scope_match = _SCOPE.search(description)
    scope_text = strip_html(scope_match.group(1)) if scope_match else ""
    scope_text = re.sub(r"\s+", " ", scope_text).strip()
    tail = description[scope_match.end():] if scope_match else description
    cells_rows = [[c.strip() for c in m.group(1).split("|")] for m in _ROW.finditer(tail)]
    cells_rows = [r for r in cells_rows if r]
    if not cells_rows:
        return None
    rows = _rows_from_table(cells_rows)
    if not rows:
        return None
    cost_match = _COST.search(description)
    error_cost = int(cost_match.group(1)) if cost_match else 1
    scope_key = normalize_scope(scope_text)
    if scope_key in ("", PER_ACCOUNT):
        key = f"op:{operation_id}"
    else:
        key = "group:" + scope_key.removeprefix(PER_ACCOUNT).strip()
    return LimitRule(key=key, scope=scope_text or PER_ACCOUNT, rows=tuple(rows),
                     error_cost=error_cost)
