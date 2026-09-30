"""Большие выгрузки: хранилище, конверт с образцом, wb_result, query, ресурс."""

import json
import time

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ResourceError, ToolError

from wbmcp.app import build_app
from wbmcp.auth import ClientPrincipal
from wbmcp.config import Config, ResultsConfig, SellerConfig, ServerConfig, load_config
from wbmcp.results import (
    ResultQueryError,
    ResultStore,
    UnknownResultError,
    apply_query,
    preview_of,
)
from wbmcp.server import WbMcpServer
from wbmcp.tools.confirm import AutoConfirmer


def test_preview_and_query():
    big = {"cards": [{"nmID": i, "title": "x" * 300, "sizes": [{"s": j} for j in range(9)]}
                     for i in range(8)], "cursor": {"total": 8}}
    view = preview_of(big, items=2, depth=3, text=10)
    assert len(view["cards"]) == 2 and view["cards"][0]["title"].startswith("xxxxxxxxxx…")
    assert view["cards"][0]["sizes"] == "<список из 9>"
    assert preview_of({"a": {"b": {"c": {"d": 1}}}}, depth=2)["a"]["b"] == "<объект с ключами c>"
    assert apply_query(big, "cards[?nmID > `5`].nmID") == [6, 7]
    with pytest.raises(ResultQueryError):
        apply_query(big, "cards[")


def test_store_save_dedupe_list_purge(tmp_path):
    clock = {"now": 1000.0}
    store = ResultStore(tmp_path / "r", inline_max_bytes=50, preview_items=2, keep_hours=1,
                        clock=lambda: clock["now"])
    data = {"items": [{"id": i} for i in range(20)]}
    first = store.save(data, tool="t", operation="op", seller="s")
    second = store.save(data, tool="t", operation="op", seller="s")
    assert first.id == second.id and first.items == 20 and first.top_keys == ["items"]
    assert (tmp_path / "r" / f"{first.id}.json").exists()
    assert [r.id for r in store.list()] == [first.id]
    stored, loaded = store.load(first.id)
    assert loaded == data and stored.size == first.size
    sliced = store.query(first.id, "items[].id", offset=5, limit=3)
    assert sliced["items"] == [5, 6, 7] and sliced["total"] == 20
    assert store.query(first.id, "items[0]")["value"] == {"id": 0}
    assert "value" in store.query(first.id) and "total" not in store.query(first.id)
    with pytest.raises(UnknownResultError):
        store.load("nope")
    clock["now"] += 2 * 3600
    other = store.save([1, 2, 3], tool="t", operation="op", seller="s")
    assert other.items == 3 and not (tmp_path / "r" / f"{first.id}.json").exists()
    with pytest.raises(UnknownResultError):
        store.get(first.id)
    assert first.as_dict()["created_at"].startswith("1970")


def test_compact(tmp_path):
    store = ResultStore(tmp_path / "r", inline_max_bytes=100, preview_items=1)
    small = {"data": {"a": 1}}
    assert store.compact(small, "data", tool="t", operation="op", seller="s") is False
    assert store.compact({"nodata": 1}, "data", tool="t", operation="op", seller="s") is False
    big = {"data": [{"id": i, "name": "n" * 20} for i in range(30)], "status": 200}
    assert store.compact(big, "data", tool="t", operation="op", seller="s") is True
    assert big["truncated"] and len(big["data"]) == 1 and big["stored"]["items"] == 30
    assert "wb_result" in big["stored"]["note"] and big["stored"]["id"]
    assert store.get(big["stored"]["id"]).tool == "t"


def test_results_config(tmp_path):
    cfg = ResultsConfig()
    assert cfg.inline_max_bytes == 200_000 and cfg.keep_hours == 168
    file = tmp_path / "c.toml"
    file.write_text('[server.results]\ninline_max_bytes = 10\npreview_items = 1\nkeep_hours = 2\n',
                    encoding="utf-8")
    loaded = load_config(file, env={})
    assert loaded.server.results.inline_max_bytes == 10 and loaded.server.results.keep_hours == 2


CARDS = [{"nmID": i, "brand": "XBOX" if i % 2 else "PS", "title": f"game {i}", "desc": "d" * 500}
         for i in range(40)]


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/content/v2/get/cards/list":
        body = json.loads(request.content)
        cursor = body["settings"]["cursor"]
        if "nmID" not in cursor:
            return httpx.Response(200, json={
                "cards": CARDS[:20], "cursor": {"updatedAt": "t", "nmID": 19, "total": 20}})
        if cursor["nmID"] == 19:
            return httpx.Response(200, json={
                "cards": CARDS[20:], "cursor": {"updatedAt": "t2", "nmID": 39, "total": 20}})
        return httpx.Response(200, json={"cards": [], "cursor": {"total": 0}})
    if path == "/api/v3/warehouses":
        return httpx.Response(200, json=[{"id": i, "name": f"склад {i}"} for i in range(3)])
    if path == "/api/v3/orders/new":
        return httpx.Response(200, json={"orders": [{"id": i, "note": "x" * 100}
                                                    for i in range(50)]})
    return httpx.Response(404, json={"path": path})


@pytest.fixture
async def app(tmp_path, make_token):
    config = Config(server=ServerConfig(profiles=["orders"], data_dir=tmp_path / "d",
                                        results=ResultsConfig(inline_max_bytes=2000,
                                                              preview_items=2)),
                    sellers=[SellerConfig(alias="main", token=make_token())])
    built = await build_app(config, env={}, transport=httpx.MockTransport(handler),
                            data_dir=tmp_path / "d")
    yield built
    await built.aclose()


@pytest.fixture
def server(app):
    return WbMcpServer(app, confirmer=AutoConfirmer(True))


def payload(result):
    return result.structured_content


async def test_big_response_goes_to_file(server, app):
    result = payload(await server.call_tool("wb_orders_new", {}))
    assert result["truncated"] and len(result["data"]["orders"]) == 2
    stored = result["stored"]
    assert stored["items"] == 50 and stored["top_keys"] == ["orders"]
    assert (app.data_dir / "results" / f"{stored['id']}.json").exists()
    assert app.metrics.counter("results_total", {"kind": "stored"}) == 1
    # срез через wb_result
    sliced = payload(await server.call_tool(
        "wb_result", {"result_id": stored["id"], "select": "orders[].id", "offset": 10,
                      "limit": 5}))
    assert sliced["items"] == [10, 11, 12, 13, 14] and sliced["total"] == 50
    listing = payload(await server.call_tool("wb_result", {}))
    assert listing["results"][0]["id"] == stored["id"]
    with pytest.raises(ToolError):
        await server.call_tool("wb_result", {"result_id": "nope"})
    with pytest.raises(ToolError):
        await server.call_tool("wb_result", {"result_id": stored["id"], "select": "orders["})
    # ресурс с полным содержимым
    contents = list(await server.read_resource(f"wb://results/{stored['id']}"))
    full = json.loads(contents[0].content)
    assert len(full["orders"]) == 50 and contents[0].mime_type == "application/json"


async def test_query_projection_and_cache(server, app):
    small = payload(await server.call_tool("wb_warehouses", {"select": "[].name"}))
    assert small["data"] == ["склад 0", "склад 1", "склад 2"] and small["select"] == "[].name"
    assert "truncated" not in small
    # проекция не мешает кэшу: полный ответ закэширован, другой query читает из кэша
    again = payload(await server.call_tool("wb_warehouses", {"select": "[0].id"}))
    assert again["data"] == 0 and again["cache"]["hit"] is True
    with pytest.raises(ToolError):
        await server.call_tool("wb_warehouses", {"select": "[["})
    projected = payload(await server.call_tool("wb_orders_new",
                                               {"select": "orders[?id < `3`].id"}))
    assert projected["data"] == [0, 1, 2] and "truncated" not in projected


async def test_curated_aggregate_spills_and_queries(server, app):
    cards = payload(await server.call_tool("wb_cards", {"limit": 20}))
    assert cards["count"] == 40 and cards["truncated"] and len(cards["cards"]) == 2
    assert cards["stored"]["items"] == 40
    xbox = payload(await server.call_tool("wb_cards", {"limit": 20,
                                                       "select": "[?brand=='XBOX'].nmID"}))
    assert xbox["cards"] == [1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23, 25, 27, 29, 31, 33,
                             35, 37, 39] and "truncated" not in xbox
    dry = payload(await server.call_tool("wb_request", {
        "method": "GET", "path": "/api/v3/orders/new", "select": "orders[0]"}))
    assert dry["data"] == {"id": 0, "note": "x" * 100}


async def test_generated_schema_has_query(app):
    from wbmcp.catalog import Catalog

    orders = Catalog.input_schema(app.catalog.by_route("GET", "/api/v3/orders/new"))
    assert "select" in orders["properties"]
    file_op = app.catalog.by_route("GET", "/api/v2/nm-report/downloads/file/{downloadId}")
    assert "select" not in Catalog.input_schema(file_op)["properties"]
    supplies = Catalog.input_schema(app.catalog.by_route("POST", "/api/v3/supplies"))
    assert "select" not in supplies["properties"]


async def test_results_are_visible_only_to_their_owner(server, app, monkeypatch):
    # одинаковые данные у двух кабинетов — две записи с разными владельцами
    same = [{"id": i} for i in range(300)]
    mine = app.results.save(same, tool="t", operation="getV3Orders", seller="main")
    theirs = app.results.save(same, tool="t", operation="getV3Orders", seller="second")
    money = app.results.save({"balance": 1}, tool="t", operation="postV1AcquiringList",
                             seller="main", scopes=("read", "finance"))
    assert mine.id != theirs.id and mine.sha256 == theirs.sha256
    reader = ClientPrincipal("agent", frozenset({"read"}), frozenset({"main"}))
    monkeypatch.setattr(app, "principal", lambda: reader)
    listing = payload(await server.call_tool("wb_result", {}))
    assert [r["id"] for r in listing["results"]] == [mine.id]
    sliced = payload(await server.call_tool("wb_result", {"result_id": mine.id, "limit": 2}))
    assert sliced["total"] == 300
    for hidden in (theirs.id, money.id):
        with pytest.raises(ToolError, match="недоступен"):
            await server.call_tool("wb_result", {"result_id": hidden})
        with pytest.raises(ResourceError, match="недоступен"):
            await server.read_resource(f"wb://results/{hidden}")
    # с finance свой финансовый результат виден, чужой кабинет по-прежнему нет
    rich = ClientPrincipal("agent", frozenset({"read", "finance"}), frozenset({"main"}))
    monkeypatch.setattr(app, "principal", lambda: rich)
    listing = payload(await server.call_tool("wb_result", {}))
    assert {r["id"] for r in listing["results"]} == {mine.id, money.id}
    with pytest.raises(ToolError):
        await server.call_tool("wb_result", {"result_id": theirs.id})
    # индекс мог ослабить scope (старая миграция): требования операции проверяются и сейчас
    monkeypatch.setattr(app, "principal", lambda: reader)
    app.results._conn.execute("UPDATE results SET scopes = 'read' WHERE id = ?", (money.id,))
    app.results._conn.commit()
    assert app.results.get(money.id).scopes == ["read"]
    with pytest.raises(ToolError, match="недоступен"):
        await server.call_tool("wb_result", {"result_id": money.id})
    with pytest.raises(ResourceError, match="недоступен"):
        await server.read_resource(f"wb://results/{money.id}")
    # выгрузка неизвестной операции — только admin
    odd = app.results.save({"x": 1}, tool="t", operation="zzz", seller="main")
    with pytest.raises(ToolError):
        await server.call_tool("wb_result", {"result_id": odd.id})
    root = ClientPrincipal("root", frozenset({"read", "admin"}), None)
    monkeypatch.setattr(app, "principal", lambda: root)
    assert payload(await server.call_tool("wb_result", {"result_id": odd.id}))["value"] == {"x": 1}


def test_result_index_migrates_old_schema(tmp_path):
    # индекс без колонки scopes от ранней версии дополняется при открытии
    import sqlite3

    directory = tmp_path / "r"
    directory.mkdir()
    conn = sqlite3.connect(directory / "index.db")
    conn.execute(
        "CREATE TABLE results (id TEXT PRIMARY KEY, path TEXT NOT NULL, size INTEGER NOT NULL, "
        "sha256 TEXT NOT NULL, created_at REAL NOT NULL, tool TEXT NOT NULL, "
        "operation TEXT NOT NULL, seller TEXT NOT NULL, items INTEGER, top_keys TEXT NOT NULL)"
    )
    for name, operation in (("old", "getV3Orders"), ("money", "getBalance"), ("odd", "zzz")):
        (directory / f"{name}.json").write_text("[1]", encoding="utf-8")
        conn.execute("INSERT INTO results VALUES (?, ?, 3, 'x', ?, 't', ?, 's', 1, '[]')",
                     (name, str(directory / f"{name}.json"), time.time(), operation))
    conn.commit()
    conn.close()
    # без резолвера scope старые выгрузки видны только admin
    assert [r.scopes for r in ResultStore(directory).list()] == [["admin"]] * 3
    # с резолвером scope берутся из операции, неизвестная — admin
    known = {"getV3Orders": {"read"}, "getBalance": {"read", "finance"}}
    conn = sqlite3.connect(directory / "index.db")
    conn.execute("ALTER TABLE results DROP COLUMN scopes")
    conn.commit()
    conn.close()
    store = ResultStore(directory, scopes_for=lambda op: known.get(op, {"admin"}))
    assert store.get("old").scopes == ["read"]
    assert store.get("money").scopes == ["finance", "read"]
    assert store.get("odd").scopes == ["admin"]
    assert ResultStore(directory).get("money").scopes == ["finance", "read"]  # повторно не трогает
    assert store._version() == 2


def test_result_index_recomputes_after_early_migration(tmp_path):
    # ранняя версия уже добавила колонку с умолчанием read и без отметки версии
    import sqlite3

    directory = tmp_path / "r"
    directory.mkdir()
    conn = sqlite3.connect(directory / "index.db")
    conn.execute(
        "CREATE TABLE results (id TEXT PRIMARY KEY, path TEXT NOT NULL, size INTEGER NOT NULL, "
        "sha256 TEXT NOT NULL, created_at REAL NOT NULL, tool TEXT NOT NULL, "
        "operation TEXT NOT NULL, seller TEXT NOT NULL, items INTEGER, top_keys TEXT NOT NULL, "
        "scopes TEXT NOT NULL DEFAULT 'read')"
    )
    (directory / "money.json").write_text("[1]", encoding="utf-8")
    conn.execute("INSERT INTO results (id, path, size, sha256, created_at, tool, operation, "
                 "seller, items, top_keys) VALUES ('money', ?, 3, 'x', ?, 't', 'getBalance', "
                 "'s', 1, '[]')", (str(directory / "money.json"), time.time()))
    conn.commit()
    conn.close()
    store = ResultStore(directory, scopes_for=lambda op: {"read", "finance"})
    assert store.get("money").scopes == ["finance", "read"] and store._version() == 2
    # версия отмечена: следующий запуск с другим резолвером ничего не пересчитывает
    assert ResultStore(directory, scopes_for=lambda op: {"read"}).get("money").scopes == [
        "finance", "read"]


async def test_aggregated_results_get_real_owner(server, app, monkeypatch):
    reader = ClientPrincipal("agent", frozenset({"read"}), frozenset({"main"}))
    monkeypatch.setattr(app, "principal", lambda: reader)
    result = payload(await server.call_tool("wb_cards", {"limit": 20}))   # seller не указан
    assert result["truncated"] and result["stored"]["seller"] == "main"
    assert result["stored"]["operation"] == "postV2GetCardsList"
    listing = payload(await server.call_tool("wb_result", {}))
    assert [r["id"] for r in listing["results"]] == [result["stored"]["id"]]
    assert listing["results"][0]["scopes"] == ["read"]
    sliced = payload(await server.call_tool("wb_result", {"result_id": result["stored"]["id"],
                                                          "limit": 1}))
    assert sliced["total"] == 40
    with pytest.raises(ToolError, match="не найден"):
        await server.call_tool("wb_orders", {"seller": "other"})
