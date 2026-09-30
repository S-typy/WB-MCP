"""Курируемые инструменты по темам: статусы и стикеры, общение, реклама, FBW, финансы."""

import json

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from wbmcp.app import build_app
from wbmcp.config import Config, SellerConfig, ServerConfig
from wbmcp.server import WbMcpServer
from wbmcp.tools.confirm import AutoConfirmer


class FakeClock:
    """Часы лимитера, которые идут вперёд только во сне."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


class Backend:
    """Маленький WB для новых инструментов; запоминает запросы."""

    def __init__(self):
        self.requests = []
        self.sign = "sign-1"          # текущая подпись чата; меняется, когда «протухает»
        self.sign_fails = 0
        self.buffer_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, q = request.url.path, dict(request.url.params)
        body = json.loads(request.content) if request.content and request.headers.get(
            "content-type", "").startswith("application/json") else None
        if path == "/api/v3/orders/status":
            return httpx.Response(200, json={"orders": [{"id": i, "supplierStatus": "new"}
                                                        for i in body["orders"]]})
        if path == "/api/v3/orders/status/history":
            return httpx.Response(200, json={"orders": [{"id": i, "history": []}
                                                        for i in body["orders"]]})
        if path == "/api/v3/orders/stickers":
            return httpx.Response(200, json={"stickers": [{"orderId": i, "file": "QUJD"}
                                                          for i in body["orders"]]})
        if path == "/api/v3/supplies":
            nxt = int(q.get("next", "0") or 0)
            pages = {0: {"next": 5, "supplies": [{"id": "WB-GI-1"}]},
                     5: {"next": 0, "supplies": [{"id": "WB-GI-2"}]}}
            return httpx.Response(200, json=pages[nxt])
        if path == "/content/v2/cards/update":
            return httpx.Response(200, json={"error": False})
        if path == "/api/v1/feedbacks/answer":
            return httpx.Response(204)
        if path == "/api/v1/questions" and request.method == "GET":
            skip = int(q["skip"])
            total = [{"id": f"q{i}"} for i in range(5)]
            return httpx.Response(200, json={"data": {"questions": total[skip:skip + 2],
                                                      "countUnanswered": 5}})
        if path == "/api/v1/questions" and request.method == "PATCH":
            return httpx.Response(200, json={"error": False})
        if path == "/api/v1/seller/chats":
            return httpx.Response(200, json={"result": [
                {"chatID": "c1", "replySign": self.sign, "clientName": "Иван"}]})
        if path == "/api/v1/seller/events":
            nxt = q.get("next")
            if nxt is None:
                return httpx.Response(200, json={"result": {"events": [{"eventID": "e1"}],
                                                            "next": 100, "totalEvents": 2}})
            if nxt == "100":
                return httpx.Response(200, json={"result": {"events": [{"eventID": "e2"}],
                                                            "next": 200, "totalEvents": 2}})
            return httpx.Response(200, json={"result": {"events": [], "next": 200,
                                                        "totalEvents": 2}})
        if path == "/api/v1/seller/message":
            form = request.content.decode("utf-8", "replace")
            if self.sign_fails and self.sign not in form:
                self.sign_fails -= 1
                return httpx.Response(400, json={"errors": ["invalid sign"]})
            return httpx.Response(200, json={"result": {"chatID": "c1", "sign": self.sign}})
        if path == "/api/v1/claims":
            offset = int(q.get("offset", "0"))
            claims = [{"id": f"cl{i}"} for i in range(3)]
            return httpx.Response(200, json={"claims": claims[offset:offset + 2], "total": 3})
        if path == "/api/advert/v2/adverts":
            return httpx.Response(200, json={"adverts": [{"advertId": 1}], "echo": q})
        if path == "/adv/v3/fullstats":
            ids = q["ids"].split(",")
            return httpx.Response(200, json=[{"advertId": int(i), "views": 10} for i in ids])
        if path == "/api/v1/supplies":
            offset = int(q.get("offset", "0"))
            rows = [{"supplyID": i} for i in range(3)]
            return httpx.Response(200, json=rows[offset:offset + 2])
        if path == "/api/tariffs/v1/acceptance/coefficients":
            return httpx.Response(200, json=[{"warehouseID": 1, "coefficient": 0}])
        if path == "/api/v1/acceptance/options":
            return httpx.Response(200, json={"result": [{"barcode": "b1", "warehouses": []}]})
        if path == "/api/v1/account/balance":
            return httpx.Response(200, json={"current": 10.5, "currency": "RUB"})
        if path.startswith("/api/v1/tariffs/"):
            return httpx.Response(200, json={"response": {"data": {"echo": q}}})
        if path == "/api/v2/upload/task":
            return httpx.Response(200, json={"data": {"id": 77}})
        if path == "/api/v2/buffer/tasks":
            self.buffer_calls += 1
            if self.buffer_calls == 1:
                return httpx.Response(200, json={"data": {"status": 1}})
            return httpx.Response(400, json={"errorText": "not in buffer"})
        if path == "/api/v2/history/tasks":
            return httpx.Response(200, json={"data": {"status": 5}})
        if path == "/api/v2/history/goods/task":
            return httpx.Response(200, json={"data": {"historyGoods": [
                {"nmID": 1, "errorText": "цена ниже минимальной"}, {"nmID": 2}]}})
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

    clock = FakeClock()               # лимиты WB настоящие, ждать их в тестах незачем
    app.limiter._clock, app.limiter._sleep = clock, clock.sleep
    yield WbMcpServer(app, confirmer=AutoConfirmer(True))
    await app.aclose()


def payload(result):
    return result.structured_content


def sent_bodies(backend, path):
    return [json.loads(r.content) for r in backend.requests
            if r.url.path == path and r.content]


CARD = {"nmID": 1, "vendorCode": "a", "sizes": [{"chrtID": 5, "skus": ["2000000000001"]}]}


async def test_order_status_and_stickers(server, backend):
    ids = list(range(1, 1502))
    status = payload(await server.call_tool("wb_order_status", {"order_ids": ids}))
    assert status["count"] == 1501
    assert [len(b["orders"]) for b in sent_bodies(backend, "/api/v3/orders/status")] == [1000, 501]
    history = payload(await server.call_tool("wb_order_status",
                                             {"order_ids": [1], "history": True}))
    assert history["orders"] == [{"id": 1, "history": []}]
    stickers = payload(await server.call_tool("wb_stickers", {"order_ids": list(range(150)),
                                                              "kind": "svg"}))
    assert stickers["count"] == 150 and stickers["kind"] == "svg" and stickers["size"] == "58x40"
    sticker_requests = [r for r in backend.requests if r.url.path == "/api/v3/orders/stickers"]
    assert len(sticker_requests) == 2 and sticker_requests[0].url.params["type"] == "svg"
    for bad in ({"order_ids": []}, {"order_ids": [1], "kind": "pdf"},
                {"order_ids": [1], "width": 10, "height": 10}):
        with pytest.raises(ToolError):
            await server.call_tool("wb_stickers", bad)
    with pytest.raises(ToolError):
        await server.call_tool("wb_order_status", {"order_ids": []})


async def test_supplies_pagination(server):
    supplies = payload(await server.call_tool("wb_supplies", {}))
    assert [s["id"] for s in supplies["supplies"]] == ["WB-GI-1", "WB-GI-2"]
    assert supplies["pages"] == 2 and not supplies["capped"]


async def test_card_update(server, backend):
    dry = payload(await server.call_tool("wb_card_update", {"cards": [CARD], "dry_run": True}))
    assert dry["dry_run"] and dry["request"]["body"] == [CARD]
    done = payload(await server.call_tool("wb_card_update", {"cards": [CARD]}))
    assert done["data"] == {"error": False}
    # без обязательных полей WB карточку не примет, валидатор говорит об этом до отправки
    with pytest.raises(ToolError, match="sizes"):
        await server.call_tool("wb_card_update", {"cards": [{"nmID": 1, "vendorCode": "a"}]})
    for bad in ({"cards": []}, {"cards": [{"vendorCode": "a"}]},
                {"cards": [{"nmID": i} for i in range(3001)]}):
        with pytest.raises(ToolError):
            await server.call_tool("wb_card_update", bad)


async def test_feedback_reply_and_questions(server, backend):
    reply = payload(await server.call_tool("wb_feedback_reply",
                                           {"feedback_id": "f1", "text": "Спасибо!"}))
    assert reply["status"] == 204 and backend.requests[-1].method == "POST"
    await server.call_tool("wb_feedback_reply", {"feedback_id": "f1", "text": "Ещё раз",
                                                 "edit": True})
    assert backend.requests[-1].method == "PATCH"
    with pytest.raises(ToolError):
        await server.call_tool("wb_feedback_reply", {"feedback_id": "f1", "text": "x"})
    questions = payload(await server.call_tool("wb_questions", {"take": 2, "all_pages": True}))
    assert [q["id"] for q in questions["questions"]] == ["q0", "q1", "q2", "q3", "q4"]
    assert questions["pages"] == 3 and questions["countUnanswered"] == 5
    one = payload(await server.call_tool("wb_questions", {"take": 2}))
    assert one["count"] == 2 and one["pages"] == 1 and not one["capped"]
    answered = payload(await server.call_tool("wb_question_answer",
                                              {"question_id": "q0", "text": "Да"}))
    assert answered["data"] == {"error": False}
    assert sent_bodies(backend, "/api/v1/questions")[-1] == {
        "id": "q0", "state": "wbRu", "answer": {"text": "Да"}}
    rejected = payload(await server.call_tool("wb_question_answer",
                                              {"question_id": "q1", "state": "none"}))
    assert rejected["status"] == 200
    for bad in ({"question_id": "q0"}, {"question_id": "q0", "state": "zzz"}):
        with pytest.raises(ToolError):
            await server.call_tool("wb_question_answer", bad)


async def test_chat_tools(server, backend):
    chats = payload(await server.call_tool("wb_chats", {}))
    assert chats["data"]["result"][0]["chatID"] == "c1"
    events = payload(await server.call_tool("wb_chat_events", {}))
    assert [e["eventID"] for e in events["events"]] == ["e1", "e2"]
    assert events["pages"] == 3 and events["next"] == 200 and events["totalEvents"] == 2
    sent = payload(await server.call_tool("wb_chat_send", {"chat_id": "c1", "text": "Привет"}))
    assert sent["data"]["result"]["sign"] == "sign-1"
    # подпись протухла: WB отвечает про sign, инструмент перечитывает чаты и повторяет
    backend.sign, backend.sign_fails = "sign-2", 1
    again = payload(await server.call_tool("wb_chat_send", {"chat_id": "c1", "text": "Ещё",
                                                            "reply_sign": "sign-1"}))
    assert again["data"]["result"]["sign"] == "sign-2" and "повторена" in again["notes"][0]
    # без chat_id повторить нечем
    backend.sign, backend.sign_fails = "sign-3", 1
    with pytest.raises(ToolError, match="sign"):
        await server.call_tool("wb_chat_send", {"reply_sign": "old", "text": "x"})
    for bad in ({"text": "x"}, {"chat_id": "c1"}, {"chat_id": "nope", "text": "x"}):
        with pytest.raises(ToolError):
            await server.call_tool("wb_chat_send", bad)


async def test_returns_pagination(server, backend):
    claims = payload(await server.call_tool("wb_returns", {"limit": 2}))
    assert [c["id"] for c in claims["claims"]] == ["cl0", "cl1", "cl2"] and claims["pages"] == 2
    assert backend.requests[0].url.params["is_archive"] == "false"
    one = payload(await server.call_tool("wb_returns", {"limit": 2, "all_pages": False,
                                                        "archive": True}))
    assert one["count"] == 2 and not one["capped"] and one["archive"] is True


async def test_adverts(server, backend):
    adverts = payload(await server.call_tool("wb_adverts", {"ids": [1, 2], "statuses": [9],
                                                            "payment_type": "cpm"}))
    assert adverts["data"]["echo"] == {"ids": "1,2", "statuses": "9", "payment_type": "cpm"}
    stats = payload(await server.call_tool("wb_adverts_stats", {"ids": list(range(1, 61)),
                                                                "date_from": "2026-09-01",
                                                                "date_to": "2026-09-07"}))
    assert stats["count"] == 60 and stats["date_from"] == "2026-09-01"
    calls = [r for r in backend.requests if r.url.path == "/adv/v3/fullstats"]
    assert len(calls) == 2 and calls[0].url.params["beginDate"] == "2026-09-01"
    default = payload(await server.call_tool("wb_adverts_stats", {"ids": [1]}))
    assert default["date_from"] < default["date_to"]
    with pytest.raises(ToolError):
        await server.call_tool("wb_adverts_stats", {"ids": []})


async def test_fbw_tools(server, backend):
    supplies = payload(await server.call_tool("wb_fbw_supplies", {"limit": 2}))
    assert [s["supplyID"] for s in supplies["supplies"]] == [0, 1, 2] and supplies["pages"] == 2
    plan = payload(await server.call_tool("wb_fbw_plan", {"warehouse_ids": [1],
                                                          "goods": [{"barcode": "b1",
                                                                     "quantity": 3}]}))
    assert plan["coefficients"][0]["warehouseID"] == 1
    assert plan["options"]["result"][0]["barcode"] == "b1"
    options_request = [r for r in backend.requests if r.url.path == "/api/v1/acceptance/options"]
    assert options_request[0].url.params["warehouseID"] == "1"
    with pytest.raises(ToolError):
        await server.call_tool("wb_fbw_plan", {"goods": [{"barcode": "b1"}]})


async def test_finance_tools(server, backend):
    balance = payload(await server.call_tool("wb_balance", {}))
    assert balance["data"]["current"] == 10.5
    commission = payload(await server.call_tool("wb_tariffs", {"locale": "ru"}))
    assert commission["data"]["response"]["data"]["echo"] == {"locale": "ru"}
    box = payload(await server.call_tool("wb_tariffs", {"kind": "box", "date": "2026-09-30"}))
    assert box["data"]["response"]["data"]["echo"] == {"date": "2026-09-30"}
    today = payload(await server.call_tool("wb_tariffs", {"kind": "return"}))
    assert len(today["data"]["response"]["data"]["echo"]["date"]) == 10
    with pytest.raises(ToolError):
        await server.call_tool("wb_tariffs", {"kind": "zzz"})


async def test_prices_wait_for_outcome(server, backend, monkeypatch):
    import wbmcp.client.reports as reports

    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(reports.asyncio, "sleep", fake_sleep)
    result = payload(await server.call_tool("wb_prices_set", {
        "data": [{"nmID": 1, "price": 100}], "poll_interval_s": 1}))
    outcome = result["outcome"]
    assert outcome["upload_id"] == 77 and outcome["status"]["status"] == 5
    assert outcome["goods_with_errors"] == [{"nmID": 1, "errorText": "цена ниже минимальной"}]
    assert sleeps == [1] and backend.buffer_calls == 2
