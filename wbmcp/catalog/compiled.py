"""Собранный каталог операций: один JSON вместо файлов спецификаций WB.

Спецификации WB в пакете не распространяются. Вместо них в снимке лежит ``catalog.json``,
собранный из них заранее: методы, пути, хосты, допуск по токенам, лимиты, признаки и схемы
аргументов и ответов. Тексты документации WB (описания методов и полей) в него не входят,
остаются только заголовки методов и разделов. Кто снял спецификации сам, может подложить
их каталог через ``--specs``: тогда загрузчик возьмёт их, а не собранный каталог.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from wbmcp.catalog.models import LimitRow, LimitRule, Operation, Parameter
from wbmcp.tokens import SPEC_CATEGORY

FORMAT = 1
FILE_NAME = "catalog.json"


# Ключи схемы, под которыми лежат имена свойств, а не ключевые слова JSON Schema.
PROPERTY_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions",
                           "dependentSchemas"})


def strip_prose(schema: Any, *, names: bool = False) -> Any:
    """Копия схемы без ключевого слова ``description`` на всех уровнях.

    ``names`` — ключи словаря это имена свойств (внутри ``properties``): их не трогаем, иначе
    пропало бы настоящее поле товара ``description``.
    """
    if isinstance(schema, list):
        return [strip_prose(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    result: dict[str, Any] = {}
    for key, value in schema.items():
        if names:
            result[key] = strip_prose(value)
        elif key == "description":
            continue
        else:
            result[key] = strip_prose(value, names=key in PROPERTY_MAPS)
    return result


def _limit_to_dict(limit: LimitRule | None) -> dict[str, Any] | None:
    return limit.as_dict() if limit else None


def _limit_from_dict(data: dict[str, Any] | None) -> LimitRule | None:
    if not data:
        return None
    rows = tuple(LimitRow(r["token_type"], r["period_s"], r["limit"], r["interval_s"], r["burst"])
                 for r in data["rows"])
    return LimitRule(key=data["key"], scope=data["scope"], rows=rows,
                     error_cost=data.get("error_cost", 1))


def operation_to_dict(op: Operation, *, prose: bool = False) -> dict[str, Any]:
    """Операция как словарь; без ``prose`` тексты WB заменяются пустыми строками."""
    text = (lambda value: value) if prose else (lambda value: "")
    clean = (lambda value: value) if prose else strip_prose
    body = None
    if op.request_body is not None:
        body = dict(op.request_body)
        body["schema"] = clean(body.get("schema"))
        body["description"] = text(body.get("description", ""))
    responses = {}
    for status, resp in op.responses.items():
        entry = dict(resp)
        entry["schema"] = clean(entry.get("schema"))
        entry["description"] = text(entry.get("description", ""))
        responses[status] = entry
    return {
        "id": op.id,
        "method": op.method,
        "path": op.path,
        "section": op.section,
        "section_title": op.section_title,
        "summary": op.summary,
        "description": text(op.description),
        "tags": list(op.tags),
        "category": op.category,
        "read_only": op.read_only,
        "token_types": list(op.token_types),
        "host": op.host,
        "sandbox_host": op.sandbox_host,
        "deprecated": op.deprecated,
        "parameters": [
            {"name": p.name, "location": p.location, "required": p.required,
             "schema": clean(p.schema), "description": text(p.description)}
            for p in op.parameters
        ],
        "request_body": body,
        "responses": responses,
        "limit": _limit_to_dict(op.limit),
        "flags": sorted(op.flags),
    }


def operation_from_dict(data: dict[str, Any]) -> Operation:
    """Операция из словаря ``operation_to_dict``."""
    return Operation(
        id=data["id"],
        method=data["method"],
        path=data["path"],
        section=data["section"],
        section_title=data["section_title"],
        summary=data["summary"],
        description=data.get("description", ""),
        tags=tuple(data.get("tags", ())),
        category=data.get("category"),
        token_category=SPEC_CATEGORY.get(data.get("category")),
        read_only=data.get("read_only"),
        token_types=tuple(data.get("token_types", ())),
        host=data["host"],
        sandbox_host=data.get("sandbox_host"),
        deprecated=bool(data.get("deprecated")),
        parameters=tuple(
            Parameter(name=p["name"], location=p["location"], required=bool(p["required"]),
                      schema=p.get("schema") or {}, description=p.get("description", ""))
            for p in data.get("parameters", ())
        ),
        request_body=data.get("request_body"),
        responses=data.get("responses") or {},
        limit=_limit_from_dict(data.get("limit")),
        flags=frozenset(data.get("flags", ())),
    )


def dump_catalog(operations: Iterable[Operation], path: Path, *, snapshot: str,
                 prose: bool = False) -> int:
    """Записать собранный каталог; вернуть число операций."""
    ops = [operation_to_dict(op, prose=prose) for op in operations]
    payload = {"format": FORMAT, "snapshot": snapshot, "prose": prose, "operations": ops}
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1) + "\n",
                          encoding="utf-8")
    return len(ops)


def load_catalog(path: Path) -> list[Operation]:
    """Прочитать собранный каталог."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("format") != FORMAT:
        raise ValueError(f"{path}: формат каталога {payload.get('format')!r}, ожидается {FORMAT}")
    return [operation_from_dict(item) for item in payload["operations"]]
