"""Курируемые инструменты: пагинация, отчёты, записи."""

import json

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from wbmcp.app import build_app
from wbmcp.config import Config, SellerConfig, ServerConfig
from wbmcp.server import WbMcpServer
from wbmcp.tools.confirm import AutoConfirmer


class Backend:
    """Маленький WB: страницы заказов, карточки, отчёты со статусами."""

    def __init__(self):
        self.status_calls = 0
        self.nm_status_calls = 0
        self.fail_report = False
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        q = dict(request.url.params)
        if path == "/api/v3/orders":
            page = int(q.get("next", "0") or 0)
            pages = {0: {"next": 10, "orders": [{"id": 1}, {"id": 2}]},
                     10: {"next": 20, "orders": [{"id": 3}]}, 20: {"next": 0, "orders": []}}
            return httpx.Response(200, json=pages[page])
        if path == "/api/v3/orders/new":
            return httpx.Response(200, json={"orders": [{"id": 9}]})
        if path == "/content/v2/get/cards/list":
            body = json.loads(request.content)
            cursor = body["settings"]["cursor"]
            if "nmID" not in cursor:
                return httpx.Response(200, json={"cards": [{"nmID": 1}, {"nmID": 2}],
                                                 "cursor": {"updatedAt": "t", "nmID": 2,
                                                            "total": 2}})
            return httpx.Response(200, json={"cards": [{"nmID": 3}],
                                             "cursor": {"updatedAt": "t2", "nmID": 3, "total": 1}})
        if path == "/api/v1/feedbacks":
            return httpx.Response(200, json={"data": {"feedbacks": [{"id": "f1"}]},
                                             "echo": q})
        if path == "/api/v3/warehouses":
            return httpx.Response(200, json=[{"id": 7, "name": "Склад"}])
        if path == "/api/v2/upload/task":
            return httpx.Response(200, json={"data": {"id": 55}})
        if path.startswith("/api/v3/stocks/"):
            return httpx.Response(204)
        if path in ("/api/v1/warehouse_remains", "/api/v1/paid_storage",
                    "/api/v1/acceptance_report"):
            return httpx.Response(200, json={"data": {"taskId": "task-1"}})
        if path.endswith("/tasks/task-1/status"):
            self.status_calls += 1
            if self.fail_report:
                return httpx.Response(200, json={"data": {"status": "error"}})
            status = "done" if self.status_calls >= 2 else "processing"
            return httpx.Response(200, json={"data": {"status": status}})
        if path.endswith("/tasks/task-1/download"):
            return httpx.Response(200, json=[{"nmId": 1, "quantity": 3}])
        if path == "/api/v2/nm-report/downloads" and request.method == "POST":
            return httpx.Response(200, json={"data": "Created"})
        if path == "/api/v2/nm-report/downloads":
            self.nm_status_calls += 1
            status = "SUCCESS" if self.nm_status_calls >= 2 else "PROCESSING"
            return httpx.Response(200, json={"data": [{"id": q.get("filter[downloadIds]"),
                                                       "status": status}]})
        if path.startswith("/api/v2/nm-report/downloads/file/"):
            return httpx.Response(200, content=b"PKzip",
                                  headers={"Content-Type": "application/zip"})
        return httpx.Response(404, json={"path": path})


@pytest.fixture
def backend():
    return Backend()


@pytest.fixture
async def server(tmp_path, make_token, backend):
    config = Config(server=ServerConfig(profiles=["core"], data_dir=tmp_path / "d"),
                    sellers=[SellerConfig(alias="main", token=make_token())])
    app = await build_app(config, env={}, transport=httpx.MockTransport(backend),
                          data_dir=tmp_path / "d")
    yield WbMcpServer(app, confirmer=AutoConfirmer(True))
    await app.aclose()


def payload(result):
    return result.structured_content


async def test_orders_and_cards_pagination(server, backend):
    new = payload(await server.call_tool("wb_orders_new", {}))
    assert new["data"]["orders"][0]["id"] == 9
    orders = payload(await server.call_tool("wb_orders", {"date_from": 1, "limit": 2}))
    assert [o["id"] for o in orders["orders"]] == [1, 2, 3]
    assert orders["pages"] == 3 and not orders["capped"]
    capped = payload(await server.call_tool("wb_orders", {"max_pages": 1}))
    assert capped["capped"] and capped["count"] == 2
    cards = payload(await server.call_tool("wb_cards", {"limit": 2, "text_search": "чай",
                                                        "locale": "ru"}))
    assert [c["nmID"] for c in cards["cards"]] == [1, 2, 3] and cards["pages"] == 2
    sent = json.loads(backend.requests[-1].content)
    assert sent["settings"]["cursor"]["nmID"] == 2 and sent["settings"]["filter"]["textSearch"]
    assert backend.requests[-1].url.params["locale"] == "ru"


async def test_feedbacks_warehouses(server):
    fb = payload(await server.call_tool("wb_feedbacks", {"take": 5, "nm_id": 3}))
    assert fb["data"]["echo"]["isAnswered"] == "false" and fb["data"]["echo"]["nmId"] == "3"
    assert "dateFrom" not in fb["data"]["echo"]
    wh = payload(await server.call_tool("wb_warehouses", {}))
    assert wh["data"][0]["id"] == 7


async def test_prices_and_stocks_writes(server, backend):
    with pytest.raises(ToolError):
        await server.call_tool("wb_prices_set", {"data": []})
    dry = payload(await server.call_tool("wb_prices_set",
                                         {"data": [{"nmID": 1, "price": 100}], "dry_run": True}))
    assert dry["dry_run"] and dry["request"]["body"] == {"data": [{"nmID": 1, "price": 100}]}
    done = payload(await server.call_tool("wb_prices_set", {"data": [{"nmID": 1, "price": 100}]}))
    assert done["data"] == {"data": {"id": 55}}
    with pytest.raises(ToolError):
        await server.call_tool("wb_stocks_set", {"warehouse_id": 7})
    stocks = payload(await server.call_tool(
        "wb_stocks_set", {"warehouse_id": 7, "stocks": [{"sku": "a", "amount": 1}]}))
    assert stocks["status"] == 204
    assert backend.requests[-1].url.path == "/api/v3/stocks/7"
    assert json.loads(backend.requests[-1].content) == {"stocks": [{"sku": "a", "amount": 1}]}


async def test_task_report_flow(server, backend, monkeypatch):
    import wbmcp.client.reports as reports

    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(reports.asyncio, "sleep", fake_sleep)
    result = payload(await server.call_tool(
        "wb_report", {"kind": "warehouse_remains", "params": {"groupByNm": True},
                      "poll_interval_s": 1}))
    assert result["task_id"] == "task-1" and result["download"]["data"][0]["quantity"] == 3
    assert result["status"]["data"]["status"] == "done" and sleeps == [1]
    created = backend.requests[0]
    assert created.url.params["groupByNm"] == "true"
    dates = {"dateFrom": "2026-09-01", "dateTo": "2026-09-02"}
    no_wait = payload(await server.call_tool("wb_report", {"kind": "paid_storage", "wait": False,
                                                           "params": dates}))
    assert no_wait["task_id"] == "task-1" and "created" in no_wait
    backend.fail_report = True
    with pytest.raises(ToolError) as info:
        await server.call_tool("wb_report", {"kind": "acceptance_report", "poll_interval_s": 0,
                                             "params": dates})
    assert "не сформирован" in str(info.value)
    with pytest.raises(ToolError):
        await server.call_tool("wb_report", {"kind": "unknown"})


async def test_task_report_without_task_id(tmp_path, make_token):
    def no_task(request):
        return httpx.Response(200, json={"data": {}})

    config = Config(server=ServerConfig(profiles=["core"], data_dir=tmp_path / "n"),
                    sellers=[SellerConfig(alias="main", token=make_token())])
    app = await build_app(config, env={}, transport=httpx.MockTransport(no_task),
                          data_dir=tmp_path / "n")
    server = WbMcpServer(app, confirmer=AutoConfirmer(True))
    with pytest.raises(ToolError) as info:
        await server.call_tool("wb_report", {"kind": "warehouse_remains"})
    assert "taskId" in str(info.value)


async def test_nm_report_flow(server, backend, monkeypatch):
    import wbmcp.client.reports as reports

    async def fake_sleep(s):
        return None

    monkeypatch.setattr(reports.asyncio, "sleep", fake_sleep)
    result = payload(await server.call_tool(
        "wb_report", {"kind": "nm_report", "params": {
            "reportType": "DETAIL_HISTORY_REPORT",
            "params": {"startDate": "2026-09-01", "endDate": "2026-09-02"}}}))
    assert result["download"]["file"]["size"] == 5 and result["download_id"]
    body = json.loads(backend.requests[0].content)
    assert body["id"] == result["download_id"] and body["reportType"] == "DETAIL_HISTORY_REPORT"
    no_wait = payload(await server.call_tool(
        "wb_report", {"kind": "nm_report", "wait": False, "params": {
            "id": "fixed-id", "reportType": "DETAIL_HISTORY_REPORT",
            "params": {"startDate": "2026-09-01", "endDate": "2026-09-02"}}}))
    assert no_wait["download_id"] == "fixed-id"


async def test_report_timeout(server, backend, monkeypatch):
    import wbmcp.client.reports as reports

    calls = {"n": 0}

    def clock():
        calls["n"] += 1
        return 0 if calls["n"] <= 2 else 1000

    monkeypatch.setattr(reports.time, "monotonic", clock)

    async def fake_sleep(s):
        return None

    monkeypatch.setattr(reports.asyncio, "sleep", fake_sleep)
    backend.status_calls = -100
    with pytest.raises(ToolError) as info:
        await server.call_tool("wb_report", {"kind": "warehouse_remains", "timeout_s": 10})
    assert "не готов" in str(info.value)
