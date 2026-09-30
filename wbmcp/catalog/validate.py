"""Проверка аргументов по схеме операции до отправки в WB.

Не полный JSON Schema, а та часть, что встречается в спецификациях WB: типы,
nullable, обязательные поля, вложенные объекты и массивы, enum, границы чисел
и строк, oneOf/anyOf/allOf. Форматы (date-time и прочие) не проверяются: с ними
WB разберётся сам, а ложные отказы дороже.
"""

from __future__ import annotations

import re
from typing import Any

from wbmcp.catalog.models import Operation


class ArgumentValidationError(ValueError):
    """Аргументы не подходят под спецификацию операции."""


_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}
_INT = re.compile(r"^-?\d+$")
_NUM = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")


def _type_ok(value: Any, type_name: str, loose: bool) -> bool:
    if type_name == "null":
        return value is None
    expected = _TYPES.get(type_name)
    if expected is None:
        return True
    if isinstance(value, bool) and type_name in ("integer", "number"):
        return False
    if isinstance(value, expected):
        return True
    if loose and isinstance(value, str):
        # параметры запроса и пути часто приходят строками — это нормально
        if type_name == "integer":
            return bool(_INT.match(value))
        if type_name == "number":
            return bool(_NUM.match(value))
        if type_name == "boolean":
            return value.lower() in ("true", "false", "1", "0")
    return False


def validate(value: Any, schema: Any, path: str = "$", *, loose: bool = False) -> list[str]:
    """Список проблем; пустой список — значение подходит под схему."""
    if not isinstance(schema, dict) or not schema:
        return []
    for combinator in ("anyOf", "oneOf"):
        variants = schema.get(combinator)
        if isinstance(variants, list) and variants:
            results = [validate(value, v, path, loose=loose) for v in variants]
            if any(not r for r in results):
                return []
            flat = "; ".join(p for r in results for p in r)
            return [f"{path}: ни один вариант {combinator} не подошёл ({flat})"]
    problems: list[str] = []
    if isinstance(schema.get("allOf"), list):
        for variant in schema["allOf"]:
            problems += validate(value, variant, path, loose=loose)
    if value is None:
        types = schema.get("type")
        nullable = schema.get("nullable") or types == "null" or (
            isinstance(types, list) and "null" in types
        )
        if types and not nullable:
            problems.append(f"{path}: null не допускается")
        return problems
    types = schema.get("type")
    if types:
        allowed = types if isinstance(types, list) else [types]
        if not any(_type_ok(value, t, loose) for t in allowed):
            problems.append(f"{path}: ожидается {'/'.join(allowed)}, получено "
                            f"{type(value).__name__}")
            return problems
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: значение {value!r} не из списка {schema['enum']}")
    if isinstance(value, dict):
        for name in schema.get("required") or []:
            if name not in value:
                problems.append(f"{path}.{name}: обязательное поле отсутствует")
        properties = schema.get("properties") or {}
        for name, item in value.items():
            if name in properties:
                problems += validate(item, properties[name], f"{path}.{name}", loose=loose)
            elif schema.get("additionalProperties") is False:
                problems.append(f"{path}.{name}: поле не описано в схеме")
            elif isinstance(schema.get("additionalProperties"), dict):
                problems += validate(item, schema["additionalProperties"], f"{path}.{name}",
                                     loose=loose)
    elif isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            problems.append(f"{path}: минимум {schema['minItems']} элементов")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{path}: максимум {schema['maxItems']} элементов")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                problems += validate(item, items, f"{path}[{index}]", loose=loose)
    elif isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            problems.append(f"{path}: короче {schema['minLength']} символов")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append(f"{path}: длиннее {schema['maxLength']} символов")
        pattern = schema.get("pattern")
        if pattern:
            try:
                if not re.search(pattern, value):
                    problems.append(f"{path}: не подходит под шаблон {pattern}")
            except re.error:  # шаблоны в спеках бывают не для python
                pass
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{path}: меньше минимума {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{path}: больше максимума {schema['maximum']}")
    return problems


def validate_call(
    op: Operation,
    *,
    path_params: dict[str, Any],
    query: dict[str, Any],
    headers: dict[str, Any],
    body: Any,
) -> list[str]:
    """Проверить параметры и тело вызова операции по её спецификации."""
    problems: list[str] = []
    given = {"path": path_params, "query": query, "header": headers}
    for param in op.parameters:
        bucket = given.get(param.location)
        if bucket is None:
            continue
        key = param.name
        if param.location == "header":
            found = {k.lower(): v for k, v in bucket.items()}
            value = found.get(key.lower())
            present = key.lower() in found
        else:
            value = bucket.get(key)
            present = key in bucket
        if not present or value is None:
            if param.required and param.location != "header":
                problems.append(f"{param.location}.{key}: обязательный параметр не задан")
            continue
        problems += validate(value, param.schema, f"{param.location}.{key}", loose=True)
    if op.request_body:
        if body is None:
            if op.request_body["required"]:
                problems.append("body: тело запроса обязательно")
        elif not op.request_body["content_type"].startswith("multipart/"):
            problems += validate(body, op.request_body["schema"], "body")
    elif body is not None:
        problems.append("body: у операции нет тела запроса")
    return problems
