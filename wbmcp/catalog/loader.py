"""Загрузка снимков спецификаций и сборка операций."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from wbmcp.catalog import compiled
from wbmcp.catalog.limits import parse_limit
from wbmcp.catalog.models import Operation, Parameter
from wbmcp.catalog.schema import RefResolver, parameter_schema
from wbmcp.catalog.text import clean_summary, strip_html
from wbmcp.tokens import SPEC_CATEGORY

HTTP_METHODS = ("get", "post", "put", "patch", "delete")
ASYNC_REPORT_MARKERS = (
    "/downloads",
    "/download",
    "/tasks",
    "/task",
    "warehouse_remains",
    "acceptance_report",
    "paid_storage",
)
BINARY_TYPES = ("application/zip", "application/pdf", "image/", "application/octet-stream",
                "text/csv")
_DEPRECATED = re.compile(r"устаре|deprecated|будет отключ", re.I)
_PAGED = re.compile(r"пагинац|cursor|курсор", re.I)


def _servers(spec: dict[str, Any], item: dict[str, Any], op: dict[str, Any]) -> list[str]:
    for source in (op.get("servers"), item.get("servers"), spec.get("servers")):
        if source:
            return [s["url"].rstrip("/") for s in source if s.get("url")]
    return []


def _split_hosts(urls: list[str]) -> tuple[str, str | None]:
    prod = [u for u in urls if "sandbox" not in u]
    sandbox = [u for u in urls if "sandbox" in u]
    if not prod:
        raise ValueError(f"у операции нет боевого хоста: {urls}")
    return prod[0], (sandbox[0] if sandbox else None)


def _parameters(
    resolver: RefResolver, item: dict[str, Any], op: dict[str, Any]
) -> list[Parameter]:
    raw = list(item.get("parameters") or []) + list(op.get("parameters") or [])
    result: dict[tuple[str, str], Parameter] = {}
    for entry in raw:
        if "$ref" in entry:
            entry = resolver.lookup(entry["$ref"])
        schema = resolver.inline(entry.get("schema") or {})
        description = strip_html(entry.get("description") or "")
        param = Parameter(
            name=entry["name"],
            location=entry.get("in", "query"),
            required=bool(entry.get("required")) or entry.get("in") == "path",
            schema=parameter_schema(schema, description),
            description=description,
        )
        result[(param.location, param.name)] = param
    return list(result.values())


def _request_body(resolver: RefResolver, op: dict[str, Any]) -> dict[str, Any] | None:
    body = op.get("requestBody")
    if not body:
        return None
    if "$ref" in body:
        body = resolver.lookup(body["$ref"])
    content = body.get("content") or {}
    if not content:
        return None
    content_type, media = next(iter(content.items()))
    return {
        "content_type": content_type,
        "required": bool(body.get("required")),
        "description": strip_html(body.get("description") or ""),
        "schema": resolver.inline(media.get("schema") or {}),
    }


def _responses(resolver: RefResolver, op: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for code, entry in (op.get("responses") or {}).items():
        if "$ref" in entry:
            entry = resolver.lookup(entry["$ref"])
        content = entry.get("content") or {}
        content_type, media = next(iter(content.items())) if content else (None, {})
        schema = resolver.inline(media.get("schema") or {}, depth=6) if media else None
        result[str(code)] = {
            "description": strip_html(entry.get("description") or ""),
            "content_type": content_type,
            "schema": schema,
        }
    return result


def _flags(path: str, description: str, body: dict[str, Any] | None,
           responses: dict[str, dict[str, Any]], deprecated: bool) -> frozenset[str]:
    flags: set[str] = set()
    if any(marker in path for marker in ASYNC_REPORT_MARKERS):
        flags.add("async-report")
    if body and body["content_type"].startswith("multipart/"):
        flags.add("multipart")
    for code, resp in responses.items():
        ct = resp.get("content_type") or ""
        if code.startswith("2") and any(ct.startswith(t) for t in BINARY_TYPES):
            flags.add("binary")
    if "Джем" in description:
        flags.add("jam")
    if deprecated:
        flags.add("deprecated")
    if _PAGED.search(description):
        flags.add("paged")
    return frozenset(flags)


def load_spec(path: Path) -> list[Operation]:
    """Все операции одного файла спецификации."""
    spec = json.loads(path.read_text(encoding="utf-8"))
    resolver = RefResolver(spec)
    section = path.stem
    section_title = (spec.get("info") or {}).get("title") or section
    operations: list[Operation] = []
    for raw_path, item in (spec.get("paths") or {}).items():
        for method in HTTP_METHODS:
            op = item.get(method)
            if not op:
                continue
            host, sandbox_host = _split_hosts(_servers(spec, item, op))
            description_raw = op.get("description") or ""
            description = strip_html(description_raw)
            deprecated = bool(op.get("deprecated")) or bool(_DEPRECATED.search(description[:600]))
            body = _request_body(resolver, op)
            responses = _responses(resolver, op)
            category = op.get("x-category")
            operations.append(
                Operation(
                    id=op["operationId"],
                    method=method.upper(),
                    path=raw_path,
                    section=section,
                    section_title=section_title,
                    summary=clean_summary(op.get("summary") or ""),
                    description=description,
                    tags=tuple(op.get("tags") or ()),
                    category=category,
                    token_category=SPEC_CATEGORY.get(category),
                    read_only=op.get("x-readonly-method"),
                    token_types=tuple(op.get("x-token-types") or ()),
                    host=host,
                    sandbox_host=sandbox_host,
                    deprecated=deprecated,
                    parameters=tuple(_parameters(resolver, item, op)),
                    request_body=body,
                    responses=responses,
                    limit=parse_limit(description_raw, op["operationId"]),
                    flags=_flags(raw_path, description, body, responses, deprecated),
                )
            )
    return operations


def spec_files(specs_dir: Path) -> list[Path]:
    """Файлы спецификаций снимка: каждый ``*.json`` кроме служебных и собранного каталога."""
    return [
        file for file in sorted(Path(specs_dir).glob("*.json"))
        if not file.name.startswith("access-")
        and file.name not in ("inventory.json", compiled.FILE_NAME)
    ]


def load_snapshot(specs_dir: Path) -> list[Operation]:
    """Все операции снимка: из спецификаций WB, а без них — из собранного каталога."""
    operations: list[Operation] = []
    for file in spec_files(specs_dir):
        operations.extend(load_spec(file))
    catalog_file = Path(specs_dir) / compiled.FILE_NAME
    if not operations and catalog_file.is_file():
        operations = compiled.load_catalog(catalog_file)
    if not operations:
        raise ValueError(f"в {specs_dir} нет ни спецификаций, ни {compiled.FILE_NAME}")
    return operations
