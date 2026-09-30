"""JSON-схемы для инструментов: раскрытие $ref и чистка от лишнего."""

from __future__ import annotations

import copy
from typing import Any

DROP_KEYS = frozenset({"example", "examples", "externalDocs", "xml", "x-enumDescriptions"})
MAX_DEPTH = 14


class RefResolver:
    """Раскрывает ссылки ``#/components/...`` внутри одной спецификации."""

    def __init__(self, spec: dict[str, Any]) -> None:
        """Резолвер привязан к спецификации, из которой берутся компоненты."""
        self._spec = spec

    def lookup(self, ref: str) -> dict[str, Any]:
        """Объект по ссылке вида ``#/components/schemas/Name``."""
        if not ref.startswith("#/"):
            raise KeyError(f"внешние ссылки не поддерживаются: {ref}")
        node: Any = self._spec
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            node = node[part]
        return node

    def inline(self, schema: Any, depth: int = MAX_DEPTH, stack: tuple[str, ...] = ()) -> Any:
        """Копия схемы без ``$ref``; циклы и слишком глубокие ветки схлопываются."""
        if isinstance(schema, list):
            return [self.inline(item, depth, stack) for item in schema]
        if not isinstance(schema, dict):
            return schema
        ref = schema.get("$ref")
        if isinstance(ref, str):
            name = ref.rsplit("/", 1)[-1]
            if ref in stack or depth <= 0:
                return {"type": "object", "description": f"см. {name} (рекурсивная структура)"}
            target = self.inline(self.lookup(ref), depth - 1, stack + (ref,))
            extra = {k: v for k, v in schema.items() if k != "$ref"}
            if isinstance(target, dict):
                merged = dict(target)
                merged.update(self.inline(extra, depth - 1, stack))
                return merged
            return target  # pragma: no cover - $ref на не-объект в спеках WB не встречается
        result: dict[str, Any] = {}
        for key, value in schema.items():
            if key in DROP_KEYS or key.startswith("x-"):
                continue
            result[key] = self.inline(value, depth, stack)
        return result


def parameter_schema(param_schema: dict[str, Any], description: str) -> dict[str, Any]:
    """Схема параметра с описанием, если оно есть."""
    result = copy.deepcopy(param_schema) if param_schema else {"type": "string"}
    if description and "description" not in result:
        result["description"] = description
    return result
