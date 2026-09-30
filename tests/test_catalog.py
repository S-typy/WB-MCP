"""Каталог операций на реальном снимке спецификаций и на синтетике."""

import json

import pytest

from wbmcp.catalog import Catalog, UnknownOperationError, sections_for_profiles
from wbmcp.catalog.limits import LimitParseError, parse_count, parse_duration, parse_limit
from wbmcp.catalog.loader import load_snapshot, load_spec
from wbmcp.catalog.profiles import PROFILES, UnknownProfileError
from wbmcp.catalog.schema import RefResolver
from wbmcp.catalog.text import clean_summary, normalize_scope, strip_html
from wbmcp.tokens import Category, TokenType


def test_snapshot_loads_everything(catalog, specs_dir):
    assert len(catalog) == 305
    assert catalog.snapshot == specs_dir.name
    assert sum(1 for op in catalog if op.sandbox_host) == 143
    assert all(op.host.startswith("https://") for op in catalog)
    assert sum(1 for op in catalog if op.limit is None) == 3
    assert sum(1 for op in catalog if "multipart" in op.flags) == 2
    assert sum(1 for op in catalog if "binary" in op.flags) == 2
    assert sum(1 for op in catalog if "jam" in op.flags) == 7
    assert len(catalog.sections()) == 13
    assert catalog.sections()["orders-fbs"]["operations"] == 47


def test_operation_fields(catalog):
    op = catalog.by_route("POST", "/api/analytics/v1/order-feed")
    assert op.token_category is Category.ANALYTICS
    assert op.read_only is True
    assert op.token_types == ()
    assert op.limit is not None
    assert op.limit.key == f"op:{op.id}"
    personal = op.limit.for_token(TokenType.PERSONAL)
    basic = op.limit.for_token(TokenType.BASIC)
    assert (personal.period_s, personal.limit, personal.interval_s, personal.burst) == (
        60, 1, 60, 1,
    )
    assert basic.period_s == 3 * 3600
    assert op.limit.for_token(TokenType.TEST) == basic
    assert "paged" in op.flags
    assert catalog.get(op.tool_name) is op


def test_group_limit_and_error_cost(catalog):
    op = catalog.by_route("GET", "/api/marketplace/v3/fbs/dictionaries/countries/oksm")
    assert op.limit.key.startswith("group:")
    assert "FBS" in op.limit.scope
    assert op.limit.error_cost == 10
    row = op.limit.for_token(TokenType.SERVICE)
    assert (row.limit, row.interval_s, row.burst) == (300, 0.2, 20)
    assert row.token_type is None
    assert op.token_category is Category.MARKETPLACE
    assert op.sandbox_host is None or "sandbox" in op.sandbox_host


def test_restricted_token_types(catalog):
    op = catalog.by_route("POST", "/api/analytics/v1/stocks-report/seller-warehouses")
    assert op.token_types == ("personal", "service")
    op = catalog.by_route("GET", "/api/common/v1/subscriptions")
    assert op.token_types == ("service",)
    assert op.token_category is None


def test_flags(catalog):
    assert "async-report" in catalog.by_route("POST", "/api/v2/nm-report/downloads").flags
    binary = catalog.by_route("GET", "/api/v2/nm-report/downloads/file/{downloadId}")
    assert {"binary", "async-report"} <= binary.flags
    assert binary.path_params[0].name == "downloadId"
    multipart = [op for op in catalog if "multipart" in op.flags]
    assert all(op.request_body["content_type"].startswith("multipart/") for op in multipart)
    deprecated = [op for op in catalog if op.deprecated]
    assert any(op.path == "/adv/v1/budget" for op in deprecated)


def test_route_lookup(catalog):
    op = catalog.match_route("get", "/api/v2/nm-report/downloads/file/abc-123")
    assert op.path == "/api/v2/nm-report/downloads/file/{downloadId}"
    assert catalog.match_route("GET", "/ping").path == "/ping"
    with pytest.raises(UnknownOperationError):
        catalog.by_route("GET", "/nope")
    with pytest.raises(UnknownOperationError):
        catalog.match_route("GET", "/nope/x")
    with pytest.raises(UnknownOperationError):
        catalog.get("wb_op_nothing")


def test_search(catalog):
    hits = catalog.search("новые сборочные задания", section="orders-fbs")
    assert hits and "orders/new" in hits[0].path
    assert catalog.search("nm-report downloads", method="post")[0].method == "POST"
    exact = catalog.search("/ping")
    assert exact[0].path == "/ping"
    rates = catalog.search("", section="rates")
    assert rates and all(op.section == "rates" for op in rates)
    assert catalog.search("qwertyuiopzzz") == []
    assert len(catalog.search("отзыв", category="questionsandfeedback", limit=3)) == 3


def test_describe_and_input_schema(catalog):
    file_op = catalog.by_route("GET", "/api/v2/nm-report/downloads/file/{downloadId}")
    card = catalog.describe("wb_op_" + file_op.id)
    assert card["method"] == "GET"
    assert card["limit"]["rows"]
    assert card["input_schema"]["required"] == ["downloadId"]
    assert "seller" in card["input_schema"]["properties"]
    assert "dry_run" not in card["input_schema"]["properties"]
    read_post = catalog.by_route("POST", "/api/analytics/v1/order-feed")
    schema = Catalog.input_schema(read_post)
    assert "dry_run" not in schema["properties"] and "fresh" in schema["properties"]
    assert "body" in schema["properties"]
    write = catalog.by_route("POST", "/api/v3/supplies")
    assert Catalog.input_schema(write)["properties"]["dry_run"]["type"] == "boolean"
    assert json.dumps(schema)  # сериализуется без сюрпризов
    assert "$ref" not in json.dumps(card)


def test_multipart_schema(catalog):
    op = catalog.by_route("POST", "/content/v3/media/file")
    schema = Catalog.input_schema(op)
    assert "multipart" in schema["properties"]["body"]["description"]
    assert [p.name for p in op.header_params] == ["X-Nm-Id", "X-Photo-Number"]
    assert set(schema["required"]) >= {"X-Nm-Id", "X-Photo-Number"}


def test_profiles(catalog):
    assert sections_for_profiles(["core"]) == frozenset()
    assert sections_for_profiles(["full"]) == frozenset(PROFILES["full"])
    assert len(catalog.for_profiles(["full"])) == 305
    orders = catalog.for_profiles(["orders"])
    assert all(op.section in PROFILES["orders"] for op in orders)
    assert len(catalog.for_profiles(["orders", "content"])) == len(orders) + 52
    with pytest.raises(UnknownProfileError):
        sections_for_profiles(["nope"])
    assert set(catalog.sections()) == set(PROFILES["full"])


def test_limit_parser_units_and_forms():
    assert parse_duration("200 мс") == 0.2
    assert parse_duration("1 мин") == 60
    assert parse_duration("3 ч") == 10800
    assert parse_duration("1,5 сек") == 1.5
    assert parse_duration("10 с") == 10
    assert parse_count("300 запросов") == 300
    with pytest.raises(LimitParseError):
        parse_duration("много")
    with pytest.raises(LimitParseError):
        parse_count("нет")
    assert parse_limit("", "x") is None
    assert parse_limit("Лимит запросов есть, таблицы нет", "x") is None
    text = (
        "<a href='#'>Лимит запросов</a> на один аккаунт продавца:\n\n"
        "| Тип | Период | Лимит | Интервал | Всплеск |\n| --- | --- | --- | --- | --- |\n"
        "| Персональный | 1 мин | 3 запроса | 20 сек | 3 запроса |\n"
        "| Сервисный | 1 мин | 3 запроса | 20 сек | 3 запроса |\n"
        "| Базовый | 1 ч | 1 запрос | 1 ч | 1 запрос |\n"
        "| сломанная |\n"
    )
    rule = parse_limit(text, "opX")
    assert rule.key == "op:opX" and len(rule.rows) == 3
    assert rule.for_token(TokenType.TEST).period_s == 3600
    assert rule.error_cost == 1
    only_header = ("Лимит запросов на один аккаунт продавца:\n"
                   "| Период | Лимит | Интервал | Всплеск |\n")
    assert parse_limit(only_header, "y") is None
    short_rows = ("Лимит запросов на один аккаунт продавца:\n"
                  "| Период | Лимит |\n| 1 мин | 3 |\n")
    assert parse_limit(short_rows, "z") is None


def test_no_row_for_type():
    text = ("Лимит запросов на один аккаунт продавца:\n"
            "| Тип | Период | Лимит | Интервал | Всплеск |\n"
            "| Персональный | 1 мин | 3 запроса | 20 сек | 3 запроса |\n")
    rule = parse_limit(text, "p")
    assert rule.for_token(TokenType.PERSONAL) is not None
    assert rule.for_token(TokenType.SERVICE) is None
    assert rule.as_dict()["rows"][0]["token_type"] == "персональный"


def test_text_helpers():
    assert strip_html("<b>a</b> &amp; b\n\n\n\nc") == "a & b\n\nc"
    assert strip_html("") == ""
    assert clean_summary("Список {{ /api/x }}") == "Список"
    assert normalize_scope("<strong>Для методов</strong>  X :") == "для методов x"


def test_ref_resolver_cycles_and_merge():
    spec = {
        "components": {
            "schemas": {
                "Node": {
                    "type": "object",
                    "properties": {
                        "child": {"$ref": "#/components/schemas/Node"},
                        "v": {"type": "integer", "example": 1},
                    },
                    "x-secret": True,
                },
                "Wrap": {"$ref": "#/components/schemas/Node", "description": "обёртка"},
            }
        }
    }
    resolver = RefResolver(spec)
    node = resolver.inline({"$ref": "#/components/schemas/Node"})
    assert node["properties"]["child"]["type"] == "object"
    assert "рекурсивная" in node["properties"]["child"]["description"]
    assert "example" not in node["properties"]["v"]
    assert "x-secret" not in node
    wrap = resolver.inline({"$ref": "#/components/schemas/Wrap"})
    assert wrap["description"] == "обёртка" and "properties" in wrap
    assert resolver.inline([{"$ref": "#/components/schemas/Node"}])[0]["type"] == "object"
    assert resolver.inline("plain") == "plain"
    with pytest.raises(KeyError):
        resolver.lookup("http://elsewhere#/x")
    deep = resolver.inline({"$ref": "#/components/schemas/Node"}, depth=0)
    assert deep["type"] == "object" and "properties" not in deep


def test_load_spec_synthetic(tmp_path):
    spec = {
        "info": {"title": "Тест"},
        "servers": [{"url": "https://x-api.wildberries.ru/"}],
        "components": {
            "parameters": {"P": {"name": "p", "in": "query", "schema": {"type": "string"}}},
            "responses": {"R": {"description": "ok"}},
        },
        "paths": {
            "/a/{id}": {
                "parameters": [{"name": "id", "in": "path", "schema": {"type": "integer"}}],
                "get": {
                    "operationId": "getA",
                    "summary": "A {{ /a }}",
                    "description": "устаревший метод",
                    "parameters": [
                        {"$ref": "#/components/parameters/P"},
                        {"name": "Authorization", "in": "header", "schema": {"type": "string"}},
                    ],
                    "responses": {"200": {"$ref": "#/components/responses/R"}},
                },
                "post": {
                    "operationId": "postA",
                    "requestBody": {"$ref": "#/components/requestBodies/nothing"},
                    "responses": {},
                },
            }
        },
    }
    spec["components"]["requestBodies"] = {"nothing": {"content": {}}}
    file = tmp_path / "test.json"
    file.write_text(json.dumps(spec), encoding="utf-8")
    ops = load_spec(file)
    get = next(op for op in ops if op.id == "getA")
    assert get.host == "https://x-api.wildberries.ru" and get.sandbox_host is None
    assert get.summary == "A" and get.deprecated and "deprecated" in get.flags
    assert [p.name for p in get.path_params] == ["id"] and get.path_params[0].required
    assert [p.name for p in get.query_params] == ["p"]
    assert get.header_params == ()
    assert get.responses["200"]["description"] == "ok"
    assert get.limit is None
    post = next(op for op in ops if op.id == "postA")
    assert post.request_body is None and post.is_write
    assert Catalog.input_schema(post)["properties"]["dry_run"]
    with pytest.raises(ValueError):
        load_snapshot(tmp_path / "empty")
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError):
        load_snapshot(tmp_path / "empty")
    bad = dict(spec)
    bad["servers"] = [{"url": "https://x-sandbox.wildberries.ru"}]
    (tmp_path / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError):
        load_spec(tmp_path / "bad.json")


def test_compiled_catalog_roundtrip(catalog, tmp_path):
    from wbmcp.catalog import compiled

    target = tmp_path / "snap" / compiled.FILE_NAME
    target.parent.mkdir()
    assert compiled.dump_catalog(catalog, target, snapshot="snap") == len(catalog)
    loaded = Catalog.load(tmp_path / "snap")          # в каталоге только catalog.json
    assert len(loaded) == len(catalog) and loaded.snapshot == "snap"
    for op in catalog:
        twin = loaded.get(op.id)
        assert (twin.method, twin.path, twin.host, twin.sandbox_host) == (
            op.method, op.path, op.host, op.sandbox_host)
        assert twin.limit == op.limit and twin.flags == op.flags
        assert twin.token_category == op.token_category and twin.read_only == op.read_only
        assert [p.name for p in twin.parameters] == [p.name for p in op.parameters]
        assert compiled.operation_to_dict(twin) == compiled.operation_to_dict(op)
    # текстов WB в собранном каталоге нет, заголовки остаются
    orders = loaded.by_route("GET", "/api/v3/orders/new")
    assert orders.description == "" and orders.summary
    assert all(p.description == "" for p in orders.parameters)
    text = target.read_text(encoding="utf-8")
    assert '"description": ""' in text and '"prose": false' in text


def test_compiled_catalog_prose_and_format(catalog, tmp_path):
    from wbmcp.catalog import compiled

    assert compiled.strip_prose({"type": "object", "description": "x", "properties": {
        "a": {"type": "string", "description": "y"}}, "items": [{"description": "z"}]}) == {
        "type": "object", "properties": {"a": {"type": "string"}}, "items": [{}]}
    # description как имя свойства (описание товара) — не текст, остаётся вместе с required
    card = {"type": "object", "required": ["description"], "properties": {
        "description": {"type": "string", "description": "текст WB", "maxLength": 5000},
        "sizes": {"type": "array", "items": {"properties": {"description": {"type": "string"}}}},
    }, "$defs": {"description": {"type": "string", "description": "тоже имя"}}}
    assert compiled.strip_prose(card) == {"type": "object", "required": ["description"],
        "properties": {"description": {"type": "string", "maxLength": 5000},
                       "sizes": {"type": "array",
                                 "items": {"properties": {"description": {"type": "string"}}}}},
        "$defs": {"description": {"type": "string"}}}
    update = catalog.by_route("POST", "/content/v2/cards/update")
    body = compiled.operation_to_dict(update)["request_body"]["schema"]
    assert "description" in body["items"]["properties"]
    # с prose тексты сохраняются, без него вычищаются (в публичной сборке их нет изначально)
    from dataclasses import replace

    talkative = replace(catalog.by_route("GET", "/api/v3/orders/new"), description="текст WB")
    with_prose = tmp_path / compiled.FILE_NAME
    compiled.dump_catalog([talkative], with_prose, snapshot="s", prose=True)
    assert compiled.load_catalog(with_prose)[0].description == "текст WB"
    compiled.dump_catalog([talkative], with_prose, snapshot="s")
    assert compiled.load_catalog(with_prose)[0].description == ""
    bad = tmp_path / "bad.json"
    bad.write_text('{"format": 99, "operations": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="формат"):
        compiled.load_catalog(bad)
    with pytest.raises(ValueError, match="нет ни спецификаций"):
        load_snapshot(tmp_path / "empty")
