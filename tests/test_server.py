"""Сервер MCP: инструменты, сгенерированные операции, подтверждения, права, CLI."""

import json

import anyio
import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.server.elicitation import AcceptedElicitation, DeclinedElicitation
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.memory import create_client_server_memory_streams

from wbmcp.__main__ import main
from wbmcp.app import build_app
from wbmcp.auth import ClientPrincipal
from wbmcp.config import Config, SellerConfig, ServerConfig
from wbmcp.server import WbMcpServer
from wbmcp.tools.confirm import AutoConfirmer, ConfirmSchema, ElicitConfirmer, is_dangerous
from wbmcp.tools.runner import extract_path_params, split_arguments

CORE_TOOLS = {"wb_sellers", "wb_seller_add", "wb_seller_remove", "wb_ping", "wb_seller_info",
              "wb_news", "wb_quota", "wb_journal", "wb_audit", "wb_api_search",
              "wb_api_describe", "wb_request"}


SEEN_URLS: list[str] = []


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    SEEN_URLS.append(str(request.url))
    if path == "/ping":
        return httpx.Response(200, json={"Status": "OK", "TS": "now"})
    if path == "/api/v1/seller-info":
        return httpx.Response(200, json={"name": "ИП Тест", "sid": "1234567"})
    if path == "/api/communications/v2/news":
        return httpx.Response(200, json={"data": [{"id": 1, "title": "новость"}]})
    if path == "/api/v3/orders/new":
        return httpx.Response(200, json={"orders": [{"id": 1}]},
                              headers={"X-Ratelimit-Remaining": "19"})
    if path == "/api/v3/supplies" and request.method == "POST":
        return httpx.Response(201, json={"id": "WB-GI-1"})
    if path.startswith("/api/v3/supplies/") and request.method == "DELETE":
        return httpx.Response(204)
    if path == "/api/marketplace/v3/fbs/orders/archive":
        return httpx.Response(400, json={"title": "bad", "detail": "нет такого"})
    return httpx.Response(404, json={"title": "not found", "path": path})


@pytest.fixture
async def app(tmp_path, make_token):
    config = Config(
        server=ServerConfig(profiles=["orders", "common"], data_dir=tmp_path / "data"),
        sellers=[SellerConfig(alias="main", token=make_token(sid="1234567")),
                 SellerConfig(alias="second", token=make_token(sid="2"))],
    )
    built = await build_app(config, env={}, transport=httpx.MockTransport(handler),
                            data_dir=tmp_path / "data")
    yield built
    await built.aclose()


@pytest.fixture
def server(app):
    return WbMcpServer(app, confirmer=AutoConfirmer(False))


def payload(result):
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


async def test_list_tools(server, app):
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert CORE_TOOLS <= names
    generated = [t for t in tools if t.name.startswith("wb_op_")]
    expected = len(app.catalog.for_profiles(["orders", "common"]))
    assert len(generated) == expected == len(server.generated)
    sample = next(t for t in generated if "orders/new" in t.description)
    assert "seller" in sample.input_schema["properties"]
    assert sample.annotations.read_only_hint is True
    writer = next(t for t in generated if t.annotations.read_only_hint is False)
    assert "dry_run" in writer.input_schema["properties"]


async def test_sellers_and_api_tools(server, app):
    sellers = payload(await server.call_tool("wb_sellers", {}))
    assert [s["alias"] for s in sellers["sellers"]] == ["main", "second"]
    assert "token" not in json.dumps(sellers) or "token_type" in json.dumps(sellers)
    search = payload(await server.call_tool("wb_api_search", {"query": "ping"}))
    assert search["found"][0]["path"] == "/ping" and search["total_operations"] == 305
    card = payload(await server.call_tool("wb_api_describe", {"operation": "GET /ping"}))
    assert card["method"] == "GET" and card["input_schema"]
    by_id = payload(await server.call_tool("wb_api_describe", {"operation": card["id"]}))
    assert by_id["id"] == card["id"]
    with pytest.raises(ToolError):
        await server.call_tool("wb_api_describe", {"operation": "GET /nope"})
    with pytest.raises(ToolError):
        await server.call_tool("wb_api_describe", {"operation": "nothing"})


async def test_wb_request_and_generated(server, app):
    result = payload(await server.call_tool(
        "wb_request", {"method": "GET", "path": "/ping", "seller": "main", "query": {"x": 1}}))
    assert result["data"]["Status"] == "OK" and result["operation"]
    assert any("не описаны" in n for n in result["notes"])
    with pytest.raises(ToolError):
        await server.call_tool("wb_request", {"method": "GET", "path": "/zzz"})
    op = app.catalog.by_route("GET", "/api/v3/orders/new")
    result = payload(await server.call_tool(op.tool_name, {"seller": "main", "limit": 5}))
    assert result["data"] == {"orders": [{"id": 1}]}
    assert result["ratelimit"]["x-ratelimit-remaining"] == "19"
    # неизвестный аргумент отмечается, но не ломает вызов
    result = payload(await server.call_tool(op.tool_name, {"seller": "main", "bogus": 1}))
    assert any("неизвестные" in n for n in result["notes"])
    # ошибка WB приходит с телом
    archive = app.catalog.by_route("GET", "/api/marketplace/v3/fbs/orders/archive")
    with pytest.raises(ToolError) as info:
        await server.call_tool(archive.tool_name, {"seller": "main", "year": 2026, "month": 9,
                                                   "next": 0, "limit": 100})
    assert "нет такого" in str(info.value)
    # без seller при двух кабинетах — понятная ошибка
    with pytest.raises(ToolError) as info:
        await server.call_tool(op.tool_name, {})
    assert "доступны" in str(info.value)


async def test_wb_request_params_cannot_override_controls(server):
    # dry_run и force в query или headers — параметры WB, а не наши флаги
    before = len(SEEN_URLS)
    result = payload(await server.call_tool("wb_request", {
        "method": "POST", "path": "/api/v3/supplies", "seller": "main",
        "body": {"name": "поставка"}, "dry_run": True,
        "query": {"dry_run": False, "force": True, "seller": "second"},
        "headers": {"dry_run": "false"}}))
    assert result["dry_run"] is True and len(SEEN_URLS) == before
    assert result["request"]["query"] == {"dry_run": False, "force": True, "seller": "second"}
    assert result["request"]["headers"]["dry_run"] == "false"
    # удаление опасно: force в query не заменяет подтверждение
    with pytest.raises(ToolError, match="не подтверждено"):
        await server.call_tool("wb_request", {
            "method": "DELETE", "path": "/api/v3/supplies/WB-GI-1", "seller": "main",
            "query": {"force": True}})
    assert len(SEEN_URLS) == before


async def test_write_dry_run_confirm_and_force(app):
    create = app.catalog.by_route("POST", "/api/v3/supplies")
    delete = next(op for op in app.catalog if op.method == "DELETE" and "/supplies/" in op.path)
    assert not is_dangerous(create) and is_dangerous(delete)
    server = WbMcpServer(app, confirmer=AutoConfirmer(False))
    dry = payload(await server.call_tool(
        create.tool_name, {"seller": "main", "body": {"name": "x"}, "dry_run": True}))
    assert dry["dry_run"] and dry["request"]["body"] == {"name": "x"}
    created = payload(await server.call_tool(create.tool_name,
                                             {"seller": "main", "body": {"name": "x"}}))
    assert created["data"] == {"id": "WB-GI-1"} and created["journal_key"]
    with pytest.raises(ToolError) as dup:
        await server.call_tool(create.tool_name, {"seller": "main", "body": {"name": "x"}})
    assert "force=true" in str(dup.value)
    path_arg = delete.path_params[0].name
    with pytest.raises(ToolError) as info:
        await server.call_tool(delete.tool_name, {"seller": "main", path_arg: "WB-GI-1"})
    assert "не подтверждено" in str(info.value)
    yes = WbMcpServer(app, confirmer=AutoConfirmer(True))
    done = payload(await yes.call_tool(delete.tool_name, {"seller": "main", path_arg: "WB-GI-1"}))
    assert done["status"] == 204
    forced = payload(await server.call_tool(
        delete.tool_name, {"seller": "main", path_arg: "WB-GI-2", "force": True}))
    assert forced["status"] == 204
    journal = payload(await server.call_tool("wb_journal", {"seller": "main"}))
    assert journal["entries"] and journal["entries"][0]["state"] == "sent"
    audit = payload(await server.call_tool("wb_audit", {}))
    assert audit["stats"]["by_outcome"]["cancelled"] == 1 and audit["recent"]


async def test_scopes_and_seller_restrictions(app, monkeypatch):
    server = WbMcpServer(app, confirmer=AutoConfirmer(True))
    principal = ClientPrincipal("agent", frozenset({"read"}), frozenset({"main"}))
    monkeypatch.setattr(app, "principal", lambda: principal)
    create = app.catalog.by_route("POST", "/api/v3/supplies")
    with pytest.raises(ToolError) as info:
        await server.call_tool(create.tool_name, {"seller": "main", "body": {}})
    assert "scope write" in str(info.value)
    with pytest.raises(ToolError) as info:
        await server.call_tool("wb_request",
                               {"method": "GET", "path": "/ping", "seller": "second"})
    assert "не разрешён" in str(info.value)
    sellers = payload(await server.call_tool("wb_sellers", {}))
    assert [s["alias"] for s in sellers["sellers"]] == ["main"]
    for name, args in (("wb_seller_add", {"alias": "x", "token": "a.b.c"}),
                       ("wb_seller_remove", {"alias": "x"}), ("wb_audit", {}),
                       ("wb_ping", {"seller": "second"}), ("wb_quota", {"seller": "second"}),
                       ("wb_journal", {"seller": "second"})):
        with pytest.raises(ToolError):
            await server.call_tool(name, args)


async def test_core_tools(server, app, make_token):
    ping = payload(await server.call_tool("wb_ping", {"seller": "main"}))
    assert ping["hosts"] and all(h.get("status") == 200 for h in ping["hosts"])
    one = payload(await server.call_tool(
        "wb_ping", {"seller": "main", "host": "https://content-api.wildberries.ru/"}))
    assert one["hosts"][0]["host"] == "https://content-api.wildberries.ru"
    info = payload(await server.call_tool("wb_seller_info", {"seller": "main"}))
    assert info["data"]["name"] == "ИП Тест" and info["token"]["sid"] == "1234567"
    news = payload(await server.call_tool("wb_news",
                                          {"seller": "main", "from_date": "2026-09-01"}))
    assert news["data"]["data"][0]["title"] == "новость"
    assert SEEN_URLS[-1].endswith("news?from=2026-09-01")
    await server.call_tool("wb_news", {"seller": "main", "from_id": 5})
    assert SEEN_URLS[-1].endswith("news?fromID=5")
    await server.call_tool("wb_news", {"seller": "main"})
    assert "news?from=20" in SEEN_URLS[-1]
    quota = payload(await server.call_tool("wb_quota", {"seller": "main"}))
    assert quota["quota"] and quota["note"] is None
    op = app.catalog.by_route("GET", "/api/v3/orders/new")
    quota = payload(await server.call_tool("wb_quota", {"seller": "main", "operation": op.id}))
    assert quota["quota"][0]["burst"] == 20
    fresh = payload(await server.call_tool("wb_quota", {"seller": "second"}))
    assert fresh["note"] and fresh["quota"] == []
    everything = payload(await server.call_tool("wb_quota", {"seller": "second",
                                                             "all_classes": True}))
    assert len(everything["quota"]) > 30
    added = payload(await server.call_tool(
        "wb_seller_add", {"alias": "third", "token": make_token(sid="3"), "note": "n"}))
    assert added["sid"] == "3"
    with pytest.raises(ToolError):
        await server.call_tool("wb_seller_add", {"alias": "bad", "token": "x"})
    removed = payload(await server.call_tool("wb_seller_remove", {"alias": "third"}))
    assert removed["removed"] is True


async def test_ping_errors_are_reported_per_host(app):
    def failing(request):
        raise httpx.ConnectError("down")

    config = app.config
    broken = await build_app(config, env={}, transport=httpx.MockTransport(failing),
                             data_dir=app.data_dir)
    server = WbMcpServer(broken, confirmer=AutoConfirmer(False))
    ping = payload(await server.call_tool(
        "wb_ping", {"seller": "main", "host": "https://content-api.wildberries.ru"}))
    assert "error" in ping["hosts"][0]


async def test_ping_refuses_hosts_outside_catalog(server, app, make_token):
    # токен не должен уходить на чужой адрес ни через инструмент, ни через клиент
    before = len(SEEN_URLS)
    for host in ("https://audit.invalid", "https://content-api.wildberries.ru.evil.test",
                 "http://content-api.wildberries.ru"):
        with pytest.raises(ToolError, match="не из каталога"):
            await server.call_tool("wb_ping", {"seller": "main", "host": host})
    assert len(SEEN_URLS) == before
    ping_op = app.catalog.by_route("GET", "/ping")
    main = await app.sellers.get("main")
    from wbmcp.client import PreflightError

    with pytest.raises(PreflightError, match="не из каталога"):
        await app.client.call(ping_op, main, host="https://audit.invalid")
    assert len(SEEN_URLS) == before
    # тестовому токену открыты только хосты песочницы
    await server.call_tool("wb_seller_add", {"alias": "sb", "token": make_token(sid="9", t=True,
                                                                                 acc=2)})
    with pytest.raises(ToolError, match="не из каталога"):
        await server.call_tool("wb_ping", {"seller": "sb",
                                           "host": "https://content-api.wildberries.ru"})
    ok = payload(await server.call_tool(
        "wb_ping", {"seller": "sb", "host": "https://content-api-sandbox.wildberries.ru"}))
    assert ok["hosts"][0]["status"] == 200


def test_split_arguments_and_path_params(app):
    op = app.catalog.by_route("GET", "/api/v2/nm-report/downloads/file/{downloadId}")
    parts = split_arguments(op, {"seller": "m", "downloadId": "1", "dry_run": True, "zzz": 2})
    assert parts["path_params"] == {"downloadId": "1"} and parts["unknown"] == ["zzz"]
    media = app.catalog.by_route("POST", "/content/v3/media/file")
    parts = split_arguments(media, {"x-nm-id": 5, "X-Photo-Number": 1, "body": {}})
    assert parts["headers"] == {"X-Nm-Id": 5, "X-Photo-Number": 1}
    orders = app.catalog.by_route("GET", "/api/v3/orders")
    assert split_arguments(orders, {"limit_param": 3})["query"] == {"limit": 3}
    assert extract_path_params("/a/{id}/b/{x}", "/a/1/b/2") == {"id": "1", "x": "2"}
    assert extract_path_params("/a/{id}", "/other") == {}


class FakeCtx:
    def __init__(self, capabilities, result):
        self.client_capabilities = capabilities
        self._result = result
        self.asked = None

    async def elicit(self, message, schema):
        self.asked = message
        return self._result


class Caps:
    def __init__(self, elicitation):
        self.elicitation = elicitation


async def test_elicit_confirmer():
    confirmer = ElicitConfirmer()
    assert await confirmer.confirm(None, "?") is False
    assert await confirmer.confirm(FakeCtx(None, None), "?") is False
    assert await confirmer.confirm(FakeCtx(Caps(None), None), "?") is False
    accepted = AcceptedElicitation(data=ConfirmSchema(confirm=True))
    ctx = FakeCtx(Caps(object()), accepted)
    assert await confirmer.confirm(ctx, "точно?") is True and ctx.asked == "точно?"
    rejected = AcceptedElicitation(data=ConfirmSchema(confirm=False))
    assert await confirmer.confirm(FakeCtx(Caps(object()), rejected), "?") is False
    assert await confirmer.confirm(FakeCtx(Caps(object()), DeclinedElicitation()), "?") is False

    class Broken:
        @property
        def client_capabilities(self):
            raise ValueError("no request context")

    assert await confirmer.confirm(Broken(), "?") is False


async def test_end_to_end_over_memory_streams(server):
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:
            low = server._lowlevel_server

            async def serve():
                await low.run(server_streams[0], server_streams[1],
                              low.create_initialization_options(), raise_exceptions=True)

            tg.start_soon(serve)
            async with ClientSession(client_streams[0], client_streams[1]) as session:
                init = await session.initialize()
                assert init.server_info.name == "wb-mcp"
                tools = await session.list_tools()
                assert any(t.name.startswith("wb_op_") for t in tools.tools)
                result = await session.call_tool("wb_api_search", {"query": "ping"})
                assert not result.is_error
                assert result.structured_content["found"][0]["path"] == "/ping"
                generated = server.app.catalog.by_route("GET", "/api/v3/orders/new").tool_name
                assert any(t.name == generated for t in tools.tools)
                result = await session.call_tool(generated, {"seller": "main"})
                assert not result.is_error and result.structured_content["status"] == 200
                bad = await session.call_tool("wb_request", {"method": "GET", "path": "/zzz"})
                assert bad.is_error
            tg.cancel_scope.cancel()


def test_cli(tmp_path, make_token, capsys):
    config = tmp_path / "c.toml"
    config.write_text(
        f'[server]\ndata_dir = "{tmp_path / "d"}"\n'
        f'[[sellers]]\nalias = "a"\ntoken = "{make_token()}"\n',
        encoding="utf-8",
    )
    assert main(["--check", "--config", str(config), "--profiles", "orders", "--read-only"],
                env={}) == 0
    out = capsys.readouterr().out
    assert "кабинетов 1" in out and "профилям orders" in out
    assert main(["--check", "--config", str(tmp_path / "missing.toml")], env={}) == 2
    assert main(["--check", "--transport", "http", "--config", str(config)], env={}) == 2
    assert main(["--check", "--transport", "http", "--insecure", "--config", str(config),
                 "--host", "0.0.0.0", "--port", "9000"], env={}) == 0
    bad_specs = tmp_path / "nospecs"
    assert main(["--check", "--config", str(config), "--specs", str(bad_specs)], env={}) == 2
    with pytest.raises(SystemExit) as info:
        main(["--version"], env={})
    assert info.value.code == 0


async def test_remote_clients_upload_only_from_uploads_dir(server, app, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("маркер", encoding="utf-8")
    uploads = app.data_dir / "uploads"
    uploads.mkdir()
    inside = uploads / "photo.jpg"
    inside.write_bytes(b"jpg")
    (uploads / "link.jpg").symlink_to(outside)
    request = {"method": "POST", "path": "/content/v3/media/file", "seller": "main",
               "headers": {"X-Nm-Id": "1", "X-Photo-Number": "1"}}

    def attempt(path):
        return server.call_tool("wb_request", {**request, "body": {"uploadfile": {"path": path}}})

    app.remote = True
    before = len(SEEN_URLS)
    for path in (str(outside), str(uploads / "link.jpg"), str(uploads / ".." / "secret.txt")):
        with pytest.raises(ToolError, match="каталога"):
            await attempt(path)
    assert len(SEEN_URLS) == before
    with pytest.raises(ToolError, match="404"):        # дошло до WB (подменный ответ 404)
        await attempt(str(inside))
    assert len(SEEN_URLS) == before + 1
    with pytest.raises(ToolError, match="нет или это не файл"):
        await attempt(str(uploads / "missing.jpg"))
    # локальный stdio-режим: пути не ограничены
    app.remote = False
    with pytest.raises(ToolError, match="404"):
        await attempt(str(outside))
    assert len(SEEN_URLS) == before + 2
