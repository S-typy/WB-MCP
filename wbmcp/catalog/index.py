"""Индекс операций: поиск, описание, схемы инструментов, профили."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from wbmcp.catalog.loader import load_snapshot
from wbmcp.catalog.models import Operation
from wbmcp.catalog.profiles import sections_for_profiles

_WORD = re.compile(r"[a-zа-яё0-9_]+", re.I)
RESERVED_ARGS = ("seller", "body", "dry_run", "force", "fresh", "select")


class UnknownOperationError(KeyError):
    """Операции с таким id (или метод+путь) в каталоге нет."""


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _WORD.findall(text or "")]


def _stem(word: str) -> str:
    """Грубая обрезка окончания: «новые» и «новых» должны совпадать."""
    if len(word) <= 4:
        return word
    return word[: max(4, len(word) - 3)]


class Catalog:
    """Каталог операций снимка спецификаций."""

    def __init__(self, operations: Iterable[Operation], snapshot: str = "") -> None:
        """Индексы по id и по паре метод+путь строятся сразу."""
        self._ops: dict[str, Operation] = {}
        self._by_route: dict[tuple[str, str], Operation] = {}
        self.snapshot = snapshot
        for op in operations:
            self._ops[op.id] = op
            self._by_route[(op.method, op.path)] = op

    @classmethod
    def load(cls, specs_dir: Path) -> Catalog:
        """Загрузить снимок из каталога."""
        specs_dir = Path(specs_dir)
        return cls(load_snapshot(specs_dir), snapshot=specs_dir.name)

    def __len__(self) -> int:
        """Число операций."""
        return len(self._ops)

    def __iter__(self):
        """Перебор операций в порядке загрузки."""
        return iter(self._ops.values())

    def get(self, operation_id: str) -> Operation:
        """Операция по id или по имени инструмента ``wb_op_<id>``."""
        key = operation_id.removeprefix("wb_op_")
        try:
            return self._ops[key]
        except KeyError:
            raise UnknownOperationError(f"операция {operation_id!r} не найдена") from None

    def by_route(self, method: str, path: str) -> Operation:
        """Операция по методу и пути (шаблон пути как в спецификации)."""
        try:
            return self._by_route[(method.upper(), path)]
        except KeyError:
            raise UnknownOperationError(f"нет операции {method.upper()} {path}") from None

    def match_route(self, method: str, path: str) -> Operation:
        """Операция по реальному пути: ``{param}`` в шаблоне сопоставляется с сегментом."""
        try:
            return self.by_route(method, path)
        except UnknownOperationError:
            pass
        for op in self._ops.values():
            if op.method != method.upper():
                continue
            escaped = re.escape(op.path).replace(r"\{", "{").replace(r"\}", "}")
            pattern = "^" + re.sub(r"\{[^/]+\}", r"[^/]+", escaped) + "$"
            if re.match(pattern, path):
                return op
        raise UnknownOperationError(f"нет операции {method.upper()} {path}")

    def hosts(self, sandbox: bool = False) -> frozenset[str]:
        """Все хосты снимка: боевые или песочницы. Токен никуда кроме них не уходит."""
        if sandbox:
            return frozenset(op.sandbox_host for op in self._ops.values() if op.sandbox_host)
        return frozenset(op.host for op in self._ops.values())

    def sections(self) -> dict[str, dict[str, Any]]:
        """Сводка по разделам: название и число операций."""
        result: dict[str, dict[str, Any]] = {}
        for op in self._ops.values():
            entry = result.setdefault(op.section, {"title": op.section_title, "operations": 0})
            entry["operations"] += 1
        return result

    def for_profiles(self, profiles: list[str] | tuple[str, ...]) -> list[Operation]:
        """Операции разделов, входящих в профили."""
        sections = sections_for_profiles(profiles)
        return [op for op in self._ops.values() if op.section in sections]

    def search(
        self,
        query: str,
        *,
        section: str | None = None,
        category: str | None = None,
        method: str | None = None,
        limit: int = 20,
    ) -> list[Operation]:
        """Поиск по словам в id, пути, summary, тегах и описании."""
        words = _tokens(query)
        scored: list[tuple[int, Operation]] = []
        for op in self._ops.values():
            if section and op.section != section:
                continue
            if category and op.category != category:
                continue
            if method and op.method != method.upper():
                continue
            score = self._score(op, words, query)
            if score > 0 or not words:
                scored.append((score, op))
        scored.sort(key=lambda pair: (-pair[0], pair[1].id))
        return [op for _, op in scored[:limit]]

    @staticmethod
    def _score(op: Operation, words: list[str], query: str) -> int:
        if not words:
            return 1
        if query.strip().lower() in (op.id.lower(), op.path.lower(), op.tool_name.lower()):
            return 100
        haystacks = (
            (op.id.lower(), 6),
            (op.path.lower(), 5),
            (op.summary.lower(), 4),
            (" ".join(op.tags).lower(), 3),
            (op.description[:2000].lower(), 1),
        )
        score = 0
        for word in words:
            stem = _stem(word)
            for text, weight in haystacks:
                if word in text:
                    score += weight + 1
                elif stem in text:
                    score += weight
        return score

    def describe(self, operation_id: str) -> dict[str, Any]:
        """Полная карточка операции: допуск, лимит, параметры, тело, ответы."""
        op = self.get(operation_id)
        return {
            **op.brief(),
            "tool": op.tool_name,
            "section_title": op.section_title,
            "tags": list(op.tags),
            "host": op.host,
            "sandbox_host": op.sandbox_host,
            "token_category": op.token_category.name.lower() if op.token_category else None,
            "limit": op.limit.as_dict() if op.limit else None,
            "parameters": [
                {"name": p.name, "in": p.location, "required": p.required, "schema": p.schema}
                for p in op.parameters
            ],
            "request_body": op.request_body,
            "responses": op.responses,
            "description": op.description[:6000],
            "input_schema": self.input_schema(op),
        }

    @staticmethod
    def input_schema(op: Operation) -> dict[str, Any]:
        """JSON-схема аргументов инструмента для операции."""
        properties: dict[str, Any] = {
            "seller": {
                "type": "string",
                "description": "Алиас кабинета продавца; можно не указывать, если кабинет один",
            }
        }
        required: list[str] = []
        for param in op.parameters:
            lowered = param.name.lower()
            if param.location == "header" and lowered in ("authorization", "content-type"):
                continue
            name = param.name if param.name not in RESERVED_ARGS else f"{param.name}_param"
            schema = dict(param.schema)
            schema.setdefault("description", f"параметр {param.location}")
            properties[name] = schema
            if param.required:
                required.append(name)
        if op.request_body:
            body_schema = dict(op.request_body["schema"]) or {"type": "object"}
            if op.request_body["description"]:
                body_schema.setdefault("description", op.request_body["description"])
            if op.request_body["content_type"].startswith("multipart/"):
                body_schema = {
                    "type": "object",
                    "description": "поля multipart; файл — {\"file\": {\"path\": ...}} "
                    "или {\"file\": {\"base64\": ..., \"filename\": ...}}",
                }
            properties["body"] = body_schema
            if op.request_body["required"]:
                required.append("body")
        if not op.mutating and "binary" not in op.flags:
            properties["select"] = {
                "type": "string",
                "description": "проекция ответа выражением JMESPath, например "
                "[].{id: id, name: name}; полный ответ при этом не теряется",
            }
        if op.cacheable:
            properties["fresh"] = {
                "type": "boolean",
                "description": "запросить WB заново, минуя кэш; действует только для методов, "
                "у которых настроен TTL кэша (поле cache в ответе)",
                "default": False,
            }
        if op.mutating:
            properties["dry_run"] = {
                "type": "boolean",
                "description": "проверить допуск и собрать запрос, но не отправлять",
                "default": False,
            }
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return schema
