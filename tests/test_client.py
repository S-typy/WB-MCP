"""HTTP-клиент WB на MockTransport: допуск, повторы, 429, записи, файлы."""

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from wbmcp.catalog import Catalog
from wbmcp.catalog.models import Operation
from wbmcp.client import (
    AmbiguousWriteError,
    DuplicateWriteError,
    FileStore,
    FileTooLargeError,
    PreflightError,
    RetryPolicy,
    WbClient,
    WbHttpError,
    WriteJournal,
    check_access,
)
from wbmcp.client.errors import summarize_error
from wbmcp.client.journal import make_key
from wbmcp.client.paging import collect_pages
from wbmcp.client.reports import ReportFailedError, ReportTimeoutError, wait_until
from wbmcp.ratelimit import RateLimiter
from wbmcp.sellers.models import Seller
from wbmcp.tokens import Category, decode_token


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    async def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


def seller_for(make_token, **kw) -> Seller:
    token = make_token(**kw)
    return Seller(alias="main", token=token, info=decode_token(token))


def make_client(handler, clock=None, **kw) -> WbClient:
    clock = clock or Clock()
    limiter = RateLimiter(clock=clock, sleep=clock.sleep)
    return WbClient(
        limiter=limiter, transport=httpx.MockTransport(handler), sleep=clock.sleep, clock=clock,
        user_agent="test-agent", **kw,
    )


@pytest.fixture(scope="module")
def ops(catalog: Catalog):
    return {
        "ping": catalog.by_route("GET", "/ping"),
        "new_orders": catalog.by_route("GET", "/api/v3/orders/new"),
        "order_feed": catalog.by_route("POST", "/api/analytics/v1/order-feed"),
        "file": catalog.by_route("GET", "/api/v2/nm-report/downloads/file/{downloadId}"),
        "media": catalog.by_route("POST", "/content/v3/media/file"),
        "subs": catalog.by_route("GET", "/api/common/v1/subscriptions"),
        "supplies": catalog.by_route("POST", "/api/v3/supplies"),
    }


def test_preflight(make_token, ops):
    seller = seller_for(make_token)
    report = check_access(ops["new_orders"], seller)
    assert report.ok and report.host == ops["new_orders"].host
    no_market = seller_for(make_token, s=1 << Category.CONTENT)
    report = check_access(ops["new_orders"], no_market)
    assert not report.ok and "Маркетплейс" in report.problems[0]
    read_only = seller_for(
        make_token, s=(1 << Category.MARKETPLACE) | (1 << Category.ANALYTICS) | (1 << 30)
    )
    write_op = ops["order_feed"]
    assert check_access(write_op, read_only).ok  # order-feed доступен read-only
    sandbox = seller_for(make_token, t=True, acc=2)
    report = check_access(ops["order_feed"], sandbox)
    assert any("песочниц" in p for p in report.problems)
    report = check_access(ops["new_orders"], sandbox)
    assert report.ok and "sandbox" in report.host
    expired = seller_for(make_token, exp=int((datetime.now(UTC) - timedelta(days=1)).timestamp()))
    assert any("истёк" in p for p in check_access(ops["ping"], expired).problems)
    soon = seller_for(make_token, exp=int((datetime.now(UTC) + timedelta(days=3)).timestamp()))
    assert any("истекает" in n for n in check_access(ops["ping"], soon).notes)
    personal = seller_for(make_token, acc=3)
    report = check_access(ops["subs"], personal)
    assert any("сервисный" in p for p in report.problems)
    assert check_access(ops["subs"], seller_for(make_token, acc=4)).ok
    assert any("только чтения" in p
               for p in check_access(ops["supplies"], seller, server_read_only=True).problems)
    assert check_access(write_op, seller, server_read_only=True).ok  # чтение через POST


def test_read_only_token_blocked_on_write(make_token, catalog):
    op = catalog.by_route("POST", "/api/v3/supplies")
    read_only = seller_for(make_token, s=(1 << Category.MARKETPLACE) | (1 << 30))
    assert any("только на чтение" in p for p in check_access(op, read_only).problems)
    jam = next(o for o in catalog if "jam" in o.flags)
    assert any("Джем" in n for n in check_access(jam, seller_for(make_token, acc=4)).notes)
    dep = next(o for o in catalog if o.deprecated)
    assert any("устарев" in n for n in check_access(dep, seller_for(make_token)).notes)


async def test_successful_get_with_ratelimit_headers(make_token, ops):
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers["Authorization"]
        seen["ua"] = request.headers["User-Agent"]
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"orders": [1, 2]},
                              headers={"X-Ratelimit-Remaining": "19", "X-Ratelimit-Limit": "20"})

    seller = seller_for(make_token)
    client = make_client(handler)
    result = await client.call(ops["new_orders"], seller, query={"limit": 10, "next": None})
    assert result.status == 200 and result.data == {"orders": [1, 2]}
    assert seen["auth"] == seller.token and seen["ua"] == "test-agent"
    assert seen["url"].endswith("/api/v3/orders/new?limit=10")
    assert result.ratelimit["x-ratelimit-remaining"] == "19"
    d = result.as_dict()
    assert d["data"] == {"orders": [1, 2]} and d["attempts"] == 1
    assert seller.token not in json.dumps(d)
    await client.aclose()


async def test_dry_run_does_not_send(make_token, ops):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    client = make_client(handler, journal=WriteJournal(":memory:"))
    result = await client.call(ops["supplies"], seller_for(make_token), body={"a": 1},
                               dry_run=True)
    assert result.dry_run and calls == []
    d = result.as_dict()
    assert d["request"]["body"] == {"a": 1} and "Authorization" not in d["request"]["headers"]
    assert d["journal_key"]


async def test_preflight_error_before_request(make_token, ops):
    client = make_client(lambda r: httpx.Response(200))
    with pytest.raises(PreflightError) as info:
        await client.call(ops["new_orders"], seller_for(make_token, s=1))
    assert "Маркетплейс" in str(info.value)


async def test_http_error_carries_full_body(make_token, ops):
    def handler(request):
        return httpx.Response(400, json={"title": "Bad", "detail": "поле x", "errors": ["e"]})

    client = make_client(handler)
    with pytest.raises(WbHttpError) as info:
        await client.call(ops["new_orders"], seller_for(make_token))
    assert info.value.status == 400 and info.value.data["detail"] == "поле x"
    assert "title=Bad" in str(info.value)
    assert summarize_error(["a"]) == "['a']"
    assert summarize_error({}, "txt") == "{}"
    assert summarize_error(None, "") == "без тела"
    assert summarize_error(None, "plain text") == "plain text"


async def test_retry_on_5xx_and_429(make_token, ops):
    statuses = iter([503, 429, 200])

    def handler(request):
        status = next(statuses)
        if status == 429:
            return httpx.Response(429, headers={"X-Ratelimit-Retry": "2"})
        return httpx.Response(status, json={"ok": status == 200})

    clock = Clock()
    client = make_client(handler, clock=clock)
    result = await client.call(ops["new_orders"], seller_for(make_token))
    assert result.data == {"ok": True} and result.attempts == 3
    assert clock.sleeps[0] == 1.0  # бэкофф после 503
    assert any(s >= 2 for s in clock.sleeps)  # ожидание после 429 через лимитер
    assert result.waited_s >= 3


async def test_gives_up_after_max_attempts(make_token, ops):
    client = make_client(lambda r: httpx.Response(502, text="bad gateway"),
                         retry=RetryPolicy(max_attempts=2))
    with pytest.raises(WbHttpError) as info:
        await client.call(ops["new_orders"], seller_for(make_token))
    assert info.value.status == 502 and "bad gateway" in str(info.value)


async def test_network_errors_read_retry_then_fail(make_token, ops):
    def handler(request):
        raise httpx.ConnectError("boom")

    client = make_client(handler, retry=RetryPolicy(max_attempts=2))
    with pytest.raises(WbHttpError) as info:
        await client.call(ops["new_orders"], seller_for(make_token))
    assert info.value.status == 0 and "ConnectError" in str(info.value)


async def test_write_journal_flow(make_token, ops):
    journal = WriteJournal(":memory:")
    responses = iter([httpx.Response(200, json={"id": 1}), httpx.Response(200, json={"id": 2})])
    client = make_client(lambda r: next(responses), journal=journal)
    seller = seller_for(make_token)
    result = await client.call(ops["supplies"], seller, body={"x": 1})
    assert result.data == {"id": 1}
    entry = await journal.get(result.journal_key)
    assert entry.state == "sent" and entry.status == 200 and entry.sid == seller.sid
    with pytest.raises(DuplicateWriteError) as info:
        await client.call(ops["supplies"], seller, body={"x": 1})
    assert info.value.state == "sent"
    forced = await client.call(ops["supplies"], seller, body={"x": 1}, force=True)
    assert forced.data == {"id": 2}
    assert (await journal.recent(seller.sid))[0].key == result.journal_key
    assert make_key("a", "1", {"b": 1, "a": 2}) == make_key("a", "1", {"a": 2, "b": 1})
    assert make_key("a", "1", {}) != make_key("b", "1", {})


async def test_write_ambiguous_on_timeout_and_5xx(make_token, ops):
    journal = WriteJournal(":memory:")

    def timeout_handler(request):
        raise httpx.ReadTimeout("slow")

    client = make_client(timeout_handler, journal=journal)
    seller = seller_for(make_token)
    with pytest.raises(AmbiguousWriteError) as info:
        await client.call(ops["supplies"], seller, body={"x": 1})
    assert (await journal.get(info.value.journal_key)).state == "ambiguous"
    with pytest.raises(DuplicateWriteError):
        await client.call(ops["supplies"], seller, body={"x": 1})
    client5 = make_client(lambda r: httpx.Response(500, text="oops"), journal=journal)
    with pytest.raises(AmbiguousWriteError) as info5:
        await client5.call(ops["supplies"], seller, body={"x": 2})
    assert "HTTP 500" in str(info5.value)
    assert (await journal.get(info5.value.journal_key)).status == 500
    client4 = make_client(lambda r: httpx.Response(422, json={"detail": "плохо"}), journal=journal)
    with pytest.raises(WbHttpError):
        await client4.call(ops["supplies"], seller, body={"x": 3})
    assert (await journal.recent(seller.sid))[0].state == "failed"


async def test_write_without_journal_is_not_ambiguous_blocked(make_token, ops):
    client = make_client(lambda r: httpx.Response(500, text="x"))
    with pytest.raises(AmbiguousWriteError) as info:
        await client.call(ops["supplies"], seller_for(make_token), body={})
    assert info.value.journal_key == "-"


async def test_binary_response_saved(tmp_path, make_token, ops):
    payload = b"PK\x03\x04zipzip"

    def handler(request):
        return httpx.Response(200, content=payload, headers={"Content-Type": "application/zip"})

    store = FileStore(tmp_path / "files", max_bytes=100)
    client = make_client(handler, files=store)
    result = await client.call(ops["file"], seller_for(make_token),
                               path_params={"downloadId": "a b"})
    assert result.url.endswith("/downloads/file/a%20b")
    assert result.file.size == len(payload) and result.file.path.suffix == ".zip"
    d = result.as_dict(inline_limit=1000)
    assert base64.b64decode(d["file"]["base64"]) == payload
    assert "base64" not in result.file.as_dict()
    big = FileStore(tmp_path / "small", max_bytes=3)
    with pytest.raises(FileTooLargeError):
        big.save(payload, "application/zip")
    saved = store.save(b"x", "application/pdf", "report.pdf")
    assert saved.path.name.endswith("_report.pdf")
    assert store.save(b"y", "weird/type", "").path.name.endswith(".bin")


async def test_binary_without_store_and_text_responses(make_token, ops):
    client = make_client(lambda r: httpx.Response(200, content=b"\x00\x01",
                                                  headers={"Content-Type": "application/zip"}))
    result = await client.call(ops["file"], seller_for(make_token),
                               path_params={"downloadId": "1"})
    assert result.file is None and result.text
    client = make_client(lambda r: httpx.Response(200, text="pong",
                                                  headers={"Content-Type": "text/plain"}))
    result = await client.call(ops["ping"], seller_for(make_token))
    assert result.text == "pong" and result.as_dict()["text"] == "pong"
    client = make_client(lambda r: httpx.Response(200, content=b"{bad json",
                                                  headers={"Content-Type": "application/json"}))
    result = await client.call(ops["ping"], seller_for(make_token))
    assert result.data is None and result.text == "{bad json"
    client = make_client(lambda r: httpx.Response(204))
    result = await client.call(ops["ping"], seller_for(make_token))
    assert result.status == 204 and result.as_dict().get("data") is None


async def test_multipart_upload(tmp_path, make_token, ops):
    seen = {}

    def handler(request):
        seen["content_type"] = request.headers["Content-Type"]
        seen["body"] = request.read()
        seen["nm"] = request.headers.get("X-Nm-Id")
        return httpx.Response(200, json={"data": None})

    file = tmp_path / "photo.jpg"
    file.write_bytes(b"\xff\xd8jpeg")
    client = make_client(handler)
    result = await client.call(
        ops["media"], seller_for(make_token), headers={"X-Nm-Id": 123, "X-Photo-Number": 1},
        body={"uploadfile": {"path": str(file)}, "note": "x"},
    )
    assert result.status == 200
    assert seen["content_type"].startswith("multipart/form-data") and seen["nm"] == "123"
    assert b"photo.jpg" in seen["body"] and b"jpeg" in seen["body"] and b"note" in seen["body"]
    result = await client.call(
        ops["media"], seller_for(make_token),
        body={"uploadfile": {"base64": base64.b64encode(b"abc").decode(), "filename": "a.png"}},
    )
    assert result.status == 200
    with pytest.raises(ValueError):
        await client.call(ops["media"], seller_for(make_token), body="not-a-dict")
    with pytest.raises(ValueError):
        FileStore.read_part({"nope": 1})


def test_build_url_and_missing_params():
    assert WbClient.build_url("https://h", "/a/{id}/b", {"id": "x/y"}) == "https://h/a/x%2Fy/b"
    with pytest.raises(ValueError):
        WbClient.build_url("https://h", "/a/{id}", {})


async def test_host_override_and_priority(make_token, ops):
    seen = {}

    def handler(request):
        seen["host"] = request.url.host
        return httpx.Response(200, json={"Status": "OK"})

    client = make_client(handler)
    result = await client.call(ops["ping"], seller_for(make_token),
                               host="https://marketplace-api.wildberries.ru", priority=1)
    assert seen["host"] == "marketplace-api.wildberries.ru" and result.data["Status"] == "OK"


async def test_collect_pages():
    pages = {None: {"items": [1, 2], "next": 10}, 10: {"items": [3], "next": 20},
             20: {"items": [], "next": None}}

    async def fetch(cursor):
        return pages[cursor]

    items, count, capped = await collect_pages(fetch, items_of=lambda r: r["items"],
                                               next_of=lambda r: r["next"])
    assert items == [1, 2, 3] and count == 3 and not capped
    items, count, capped = await collect_pages(fetch, items_of=lambda r: r["items"],
                                               next_of=lambda r: r["next"], max_pages=1)
    assert items == [1, 2] and capped


async def test_wait_until():
    clock = Clock()
    answers = iter([{"status": "processing"}, {"status": "done"}])

    async def poll():
        return next(answers)

    result = await wait_until(poll, done=lambda r: r["status"] == "done", interval_s=5,
                              sleep=clock.sleep, clock=clock)
    assert result["status"] == "done" and clock.sleeps == [5]

    async def failing():
        return {"status": "error"}

    with pytest.raises(ReportFailedError):
        await wait_until(failing, done=lambda r: False,
                         failed=lambda r: "error" if r["status"] == "error" else None,
                         sleep=clock.sleep, clock=clock)

    async def never():
        return {"status": "processing"}

    with pytest.raises(ReportTimeoutError):
        await wait_until(never, done=lambda r: False, interval_s=10, timeout_s=25,
                         sleep=clock.sleep, clock=clock)


async def test_journal_blocking_states(tmp_path):
    journal = WriteJournal(tmp_path / "j" / "w.db")
    await journal.mark("k", "op", "s", "reserved")
    assert (await journal.blocking_entry("k")).state == "reserved"
    await journal.mark("k", "op", "s", "failed", status=400)
    assert await journal.blocking_entry("k") is None
    await journal.mark("k", "op", "s", "sent", status=200)
    assert await journal.blocking_entry("k") is not None
    later = datetime.now(UTC) + timedelta(hours=1)
    assert await journal.blocking_entry("k", now=later) is None
    assert await journal.blocking_entry("missing") is None


async def test_journal_reserve(tmp_path):
    journal = WriteJournal(tmp_path / "r.db")
    assert await journal.reserve("k", "op", "s") is None          # пусто: заняли
    busy = await journal.reserve("k", "op", "s")                  # уже sending: занято
    assert busy is not None and busy.state == "sending"
    await journal.mark("k", "op", "s", "failed", status=400)
    assert await journal.reserve("k", "op", "s") is None          # после failed можно снова
    await journal.mark("k", "op", "s", "sent", status=200)
    fresh = await journal.reserve("k", "op", "s")                 # свежий sent мешает
    assert fresh is not None and fresh.state == "sent"
    await journal.mark("k", "op", "s", "ambiguous")
    assert (await journal.reserve("k", "op", "s")).state == "ambiguous"


def test_operation_helpers(ops):
    op: Operation = ops["file"]
    assert op.tool_name.startswith("wb_op_")
    assert not op.is_write and op.query_params == ()


async def test_sandbox_token_uses_sandbox_rule(make_token, ops):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"orders": []},
                              headers={"X-Ratelimit-Limit": "1", "X-Ratelimit-Remaining": "0"})

    clock = Clock()
    client = make_client(handler, clock=clock)
    sandbox = seller_for(make_token, acc=2, s=0, t=True)
    first = await client.call(ops["new_orders"], sandbox)
    second = await client.call(ops["new_orders"], sandbox)
    assert all("marketplace-api-sandbox" in url for url in seen)
    assert first.waited_s == 0 and second.waited_s >= 1.0 and second.attempts == 1
    keys = await client.limiter.known_keys(sandbox.sid)
    assert keys == [f"{sandbox.sid}:sandbox:marketplace-api-sandbox.wildberries.ru"]
    # ping песочного хоста тестовым токеном, хотя у /ping в спецификации песочницы нет
    pong = await client.call(ops["ping"], sandbox, host="https://content-api-sandbox.wildberries.ru")
    assert pong.status == 200 and "content-api-sandbox" in seen[-1]
    with pytest.raises(PreflightError):
        await client.call(ops["ping"], sandbox)
