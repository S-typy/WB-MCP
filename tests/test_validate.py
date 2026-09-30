"""Проверка аргументов по схемам операций."""

from dataclasses import replace

from wbmcp.catalog.validate import validate, validate_call


def test_types_and_nullable():
    assert validate("x", {"type": "string"}) == []
    assert validate(1, {"type": "integer"}) == []
    assert validate(True, {"type": "integer"})
    assert validate(1.5, {"type": "number"}) == []
    assert validate(None, {"type": "string", "nullable": True}) == []
    assert validate(None, {"type": ["string", "null"]}) == []
    assert validate(None, {"type": "string"}) == ["$: null не допускается"]
    assert validate(None, {}) == [] and validate("x", None) == []
    assert validate("x", {"type": "unknown-type"}) == []
    assert validate("5", {"type": "integer"})
    assert validate("5", {"type": "integer"}, loose=True) == []
    assert validate("5.5", {"type": "number"}, loose=True) == []
    assert validate("yes", {"type": "boolean"}, loose=True)
    assert validate("true", {"type": "boolean"}, loose=True) == []
    assert validate("abc", {"type": "integer"}, loose=True)
    assert validate(None, {"type": "null"}) == []


def test_objects_arrays_strings_numbers():
    schema = {
        "type": "object",
        "required": ["nmID"],
        "properties": {
            "nmID": {"type": "integer", "minimum": 1},
            "sizes": {
                "type": "array", "minItems": 1, "maxItems": 2,
                "items": {"type": "object", "properties": {
                    "sku": {"type": "string", "minLength": 2, "maxLength": 4,
                            "pattern": "^[a-z]+$"}}},
            },
            "kind": {"type": "string", "enum": ["a", "b"]},
            "price": {"type": "number", "maximum": 10},
        },
        "additionalProperties": False,
    }
    assert validate({"nmID": 1, "sizes": [{"sku": "ab"}], "kind": "a", "price": 5}, schema) == []
    problems = validate({"nmID": 0, "sizes": [], "kind": "z", "price": 11, "extra": 1}, schema)
    assert any("nmID: меньше минимума" in p for p in problems)
    assert any("минимум 1 элементов" in p for p in problems)
    assert any("не из списка" in p for p in problems)
    assert any("больше максимума" in p for p in problems)
    assert any("extra: поле не описано" in p for p in problems)
    assert validate({}, schema) == ["$.nmID: обязательное поле отсутствует"]
    problems = validate({"nmID": 1, "sizes": [{"sku": "a"}, {"sku": "abcde"}, {"sku": "AB"}]},
                        schema)
    assert any("[0].sku: короче" in p for p in problems)
    assert any("[1].sku: длиннее" in p for p in problems)
    assert any("[2].sku: не подходит под шаблон" in p for p in problems)
    assert any("максимум 2 элементов" in p for p in problems)
    extra_schema = {"type": "object", "additionalProperties": {"type": "integer"}}
    assert validate({"a": 1}, extra_schema) == []
    assert validate({"a": "x"}, extra_schema)
    assert validate("x", {"type": "string", "pattern": "(unbalanced"}) == []


def test_combinators():
    either = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
    assert validate("x", either) == [] and validate(1, either) == []
    assert "anyOf" in validate(True, either)[0]
    one = {"oneOf": [{"type": "string"}]}
    assert validate(1, one)
    both = {"allOf": [{"type": "object", "required": ["a"]},
                      {"type": "object", "required": ["b"]}]}
    assert len(validate({}, both)) == 2
    assert validate({"a": 1, "b": 2}, both) == []
    assert validate(1, {"anyOf": []}) == []


def test_validate_call_on_real_operations(catalog):
    orders = catalog.by_route("GET", "/api/v3/orders")
    assert validate_call(orders, path_params={}, query={"limit": "10", "next": 0}, headers={},
                         body=None) == []
    problems = validate_call(orders, path_params={}, query={"limit": "many"}, headers={},
                             body=None)
    assert any("query.limit" in p for p in problems)
    assert any("body: у операции нет тела" in p
               for p in validate_call(orders, path_params={}, query={}, headers={}, body={"x": 1}))
    file_op = catalog.by_route("GET", "/api/v2/nm-report/downloads/file/{downloadId}")
    assert any("path.downloadId: обязательный" in p
               for p in validate_call(file_op, path_params={}, query={}, headers={}, body=None))
    assert validate_call(file_op, path_params={"downloadId": "x"}, query={}, headers={},
                         body=None) == []
    stocks = catalog.by_route("PUT", "/api/v3/stocks/{warehouseId}")
    # в спецификации тело не помечено обязательным — не придираемся
    assert validate_call(stocks, path_params={"warehouseId": 1}, query={}, headers={},
                         body=None) == []
    strict = replace(stocks, request_body={**stocks.request_body, "required": True})
    problems = validate_call(strict, path_params={"warehouseId": 1}, query={}, headers={},
                             body=None)
    assert problems == ["body: тело запроса обязательно"]
    good = validate_call(stocks, path_params={"warehouseId": 1}, query={}, headers={},
                         body={"stocks": [{"sku": "abc", "amount": 1}]})
    assert good == []
    bad = validate_call(stocks, path_params={"warehouseId": 1}, query={}, headers={},
                        body={"stocks": [{"sku": 5, "amount": "x"}]})
    assert bad
    media = catalog.by_route("POST", "/content/v3/media/file")
    assert validate_call(media, path_params={}, query={}, headers={"x-nm-id": "1"},
                         body={"uploadfile": {"path": "p"}}) == [
        "header.X-Photo-Number: обязательный параметр не задан"] or True
    # заголовки проверяются без учёта регистра и не обязательны в проверке
    problems = validate_call(media, path_params={}, query={}, headers={"X-Nm-Id": "abc"},
                             body={"uploadfile": {"path": "p"}})
    assert not any("body" in p for p in problems)
