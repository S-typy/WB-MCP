"""Валидация аргументов в инструментах, метрики, wb_changes, HTTP-маршруты, smoke."""

import json

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from wbmcp.__main__ import main
from wbmcp.app import build_app
from wbmcp.config import Config, SellerConfig, ServerConfig
from wbmcp.http import build_http_app
from wbmcp.server import WbMcpServer
from wbmcp.smoke import run_smoke
from wbmcp.tools.changes import collect_strings, match_operations, news_items
from wbmcp.tools.confirm import AutoConfirmer

NEWS = {"data": [
    {"id": 1, "title": "Изменения в /api/v3/orders/new и GET /api/v3/orders",
     "content": "Добавили поле. Метод /api/v3/orders/{orderId}/meta/sgtin тоже."},
    {"id": 2, "title": "Просто новость", "content": "Без путей"},
    {"id": 3, "title": "Неизвестный путь", "content": "/api/v9/nothing/here"},
    {"id": 4, "title": "Про интеграцию", "content": "<p>В API продавца поменялись лимиты</p>"},
]}


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/ping":
        return httpx.Response(200, json={"Status": "OK"})
    if path == "/api/v1/seller-info":
        return httpx.Response(200, json={"name": "ИП", "sid": "1234567"})
    if path == "/api/communications/v2/news":
        return httpx.Response(200, json=NEWS)
    if path.startswith("/api/v3/stocks/"):
        return httpx.Response(204)
    if path == "/api/v3/orders":
        return httpx.Response(200, json={"orders": [], "next": 0})
    return httpx.Response(404, json={"path": path})


@pytest.fixture
async def app(tmp_path, make_token):
    config = Config(server=ServerConfig(profiles=["core"], data_dir=tmp_path / "d"),
                    sellers=[SellerConfig(alias="main", token=make_token())])
    built = await build_app(config, env={}, transport=httpx.MockTransport(handler),
                            data_dir=tmp_path / "d")
    yield built
    await built.aclose()


@pytest.fixture
def server(app):
    return WbMcpServer(app, confirmer=AutoConfirmer(True))


async def test_validation_blocks_bad_arguments(server, app):
    with pytest.raises(ToolError) as info:
        await server.call_tool("wb_stocks_set", {"warehouse_id": 1,
                                                 "stocks": [{"sku": 5, "amount": "x"}]})
    assert "проверку по спецификации" in str(info.value) and "force=true" in str(info.value)
    forced = await server.call_tool("wb_stocks_set", {"warehouse_id": 1, "force": True,
                                                      "stocks": [{"sku": 5, "amount": "x"}]})
    assert forced.structured_content["status"] == 204
    with pytest.raises(ToolError) as info:
        await server.call_tool("wb_request", {"method": "GET", "path": "/api/v3/orders",
                                              "query": {"limit": "many", "next": 0}})
    assert "query.limit" in str(info.value)
    with pytest.raises(ToolError) as info:
        await server.call_tool("wb_request", {"method": "GET", "path": "/api/v3/orders",
                                              "query": {"limit": 10}})
    assert "query.next: обязательный" in str(info.value)
    ok = await server.call_tool("wb_request", {"method": "GET", "path": "/api/v3/orders",
                                               "query": {"limit": "10", "next": 0}})
    assert ok.structured_content["status"] == 200
    audit = await app.audit.recent()
    assert audit[-1].outcome == "denied"


async def test_metrics_after_calls(server, app):
    await server.call_tool("wb_request", {"method": "GET", "path": "/ping"})
    with pytest.raises(ToolError):
        await server.call_tool("wb_request", {"method": "GET", "path": "/api/v3/orders/new"})
    text = app.metrics.render()
    assert 'wbmcp_calls_total{outcome="ok",section="api-information",seller="main"} 1' in text
    assert 'wbmcp_wb_status_total{seller="main",status="404"} 1' in text
    assert 'wbmcp_calls_total{outcome="error",section="orders-fbs",seller="main"} 1' in text
    assert "wbmcp_wait_seconds_count" in text and "wbmcp_elapsed_seconds_sum" in text
    dry = await server.call_tool("wb_request", {"method": "PUT", "path": "/api/v3/stocks/1",
                                                "body": {"stocks": [{"sku": "a", "amount": 1}]},
                                                "dry_run": True})
    assert dry.structured_content["dry_run"]
    assert app.metrics.counter("calls_total", {"seller": "main", "section": "item-management",
                                               "outcome": "dry_run"}) == 1


async def test_wb_changes(server, app):
    result = (await server.call_tool("wb_changes", {"from_date": "2026-09-01"})).structured_content
    assert result["items_total"] == 4 and result["snapshot"] == app.catalog.snapshot
    matched = result["matched"]
    assert [m["id"] for m in matched] == [1]
    paths = {o["path"] for o in matched[0]["operations"]}
    assert "/api/v3/orders/new" in paths and "/api/v3/orders" in paths
    assert any("{orderId}" in p for p in paths)
    assert [u["id"] for u in result["other"]] == [2]
    mentions = {m["id"]: m for m in result["api_mentions"]}
    assert set(mentions) == {3, 4}                      # неизвестный путь тоже про API
    assert "API продавца" in mentions[4]["excerpt"] and "<p>" not in mentions[4]["excerpt"]
    assert result["raw"] == NEWS
    without = (await server.call_tool("wb_changes", {})).structured_content
    assert without["items_total"] == 4
    assert collect_strings({"a": ["x", 1, {"b": "y"}], "c": None}) == ["x", "y"]
    assert news_items([{"id": 1}, "junk"]) == [{"id": 1}]
    assert news_items({"items": [{"id": 2}]}) == [{"id": 2}]
    assert news_items({"title": "one"}) == [{"title": "one"}]
    assert news_items({}) == [] and news_items("x") == []
    assert match_operations(app.catalog, "ничего") == []


async def test_http_app_health_and_metrics(server, app):
    assert app.remote is False
    asgi = build_http_app(server, host="127.0.0.1")
    assert app.remote is True     # по сети пути на диске сервера клиентам закрыты
    await server.call_tool("wb_request", {"method": "GET", "path": "/ping"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi),
                                 base_url="http://test") as client:
        health = await client.get("/health")
        assert health.status_code == 200
        body = health.json()
        assert body["status"] == "ok" and body["operations"] == 305 and body["sellers"] == 1
        metrics = await client.get("/metrics")
        assert metrics.status_code == 200 and "wbmcp_calls_total" in metrics.text
        assert metrics.headers["content-type"].startswith("text/plain")


async def test_smoke_run(app, capsys):
    lines = []
    code = await run_smoke(app, "main", printer=lines.append)
    assert code == 0 and lines[-1].startswith("итого: 5 из 5")
    assert any(line.startswith("OK   wb_ping") for line in lines)


async def test_smoke_reports_failures(tmp_path, make_token):
    def broken(request):
        return httpx.Response(404, json={"title": "down"})

    config = Config(server=ServerConfig(profiles=["core"], data_dir=tmp_path / "b"),
                    sellers=[SellerConfig(alias="main", token=make_token())])
    app = await build_app(config, env={}, transport=httpx.MockTransport(broken),
                          data_dir=tmp_path / "b")
    lines = []
    code = await run_smoke(app, None, printer=lines.append)
    assert code == 1 and any(line.startswith("FAIL wb_seller_info") for line in lines)
    assert any(line.startswith("OK   wb_sellers") for line in lines)


def test_cli_smoke(tmp_path, make_token, capsys):
    config = tmp_path / "c.toml"
    config.write_text(
        f'[server]\ndata_dir = "{tmp_path / "d"}"\n'
        f'[[sellers]]\nalias = "a"\ntoken = "{make_token()}"\n',
        encoding="utf-8",
    )
    code = main(["--smoke", "--seller", "a", "--config", str(config)], env={},
                transport=httpx.MockTransport(handler))
    out = capsys.readouterr().out
    assert code == 0 and "итого: 5 из 5" in out
    assert json.loads('{"ok": true}')["ok"]


async def test_smoke_sandbox_steps(tmp_path, make_token):
    def sandbox_handler(request: httpx.Request) -> httpx.Response:
        assert "sandbox" in request.url.host
        if request.url.path == "/ping":
            return httpx.Response(200, json={"Status": "OK"})
        if request.url.path == "/api/v3/warehouses":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/v3/orders/new":
            return httpx.Response(200, json={"orders": []})
        return httpx.Response(404, json={"path": request.url.path})

    config = Config(server=ServerConfig(profiles=["core"], data_dir=tmp_path / "sb"),
                    sellers=[SellerConfig(alias="sb", token=make_token(acc=2, s=0, t=True))])
    app = await build_app(config, env={}, transport=httpx.MockTransport(sandbox_handler),
                          data_dir=tmp_path / "sb")
    lines = []
    code = await run_smoke(app, None, printer=lines.append)
    assert code == 0 and lines[0].startswith("токен тестовый")
    assert any(line.startswith("OK   wb_warehouses") for line in lines)
    assert not any("wb_seller_info" in line for line in lines)


async def test_quota_in_sandbox(tmp_path, make_token):
    config = Config(server=ServerConfig(profiles=["core"], data_dir=tmp_path / "q"),
                    sellers=[SellerConfig(alias="sb", token=make_token(acc=2, s=0, t=True))])
    handler = httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
    app = await build_app(config, env={}, transport=handler, data_dir=tmp_path / "q")
    server = WbMcpServer(app, confirmer=AutoConfirmer(True))
    empty = (await server.call_tool("wb_quota", {})).structured_content
    assert empty["sandbox"] and empty["quota"] == [] and empty["note"]
    await server.call_tool("wb_warehouses", {})
    used = (await server.call_tool("wb_quota", {})).structured_content
    assert used["quota"][0]["key"] == "sandbox:marketplace-api-sandbox.wildberries.ru"
    assert used["quota"][0]["burst"] == 1
    everything = (await server.call_tool("wb_quota", {"all_classes": True})).structured_content
    assert len(everything["quota"]) > 5
    await app.aclose()
