"""Хранилища на настоящем Postgres (встроенный pgserver)."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from wbmcp.__main__ import main
from wbmcp.app import build_app
from wbmcp.cloud import PostgresTenantRegistry
from wbmcp.config import Config, SellerConfig, ServerConfig
from wbmcp.postgres import PostgresBackend
from wbmcp.sellers import SellerRecord, SellerRegistry
from wbmcp.sellers.crypto import FernetCipher
from wbmcp.server import WbMcpServer
from wbmcp.tools.confirm import AutoConfirmer


@pytest.fixture
async def backend(pg_url):
    pg = await PostgresBackend(pg_url, schema=f"t_{uuid.uuid4().hex[:8]}", max_size=4).open()
    yield pg
    await pg.close()


def test_schema_name_is_validated(pg_url):
    with pytest.raises(ValueError):
        PostgresBackend(pg_url, schema="bad-name; drop")


async def test_seller_store(backend, make_token):
    cipher = FernetCipher(FernetCipher.generate_key())
    store = backend.sellers(cipher)
    registry = SellerRegistry([store])
    token = make_token(sid="11", acc=4)
    await registry.add("shop", token, note="n")
    seller = await registry.get("shop")
    assert seller.sid == "11" and seller.note == "n"
    async with backend.connection() as conn:
        cur = await conn.execute("SELECT token FROM sellers")
        raw = (await cur.fetchone())[0]
    assert raw.startswith("fernet:") and token not in raw
    await store.add(SellerRecord(alias="shop", token=make_token(sid="12", acc=4), note="new"))
    assert (await store.get("shop")).note == "new" and (await registry.get("shop")).sid == "12"
    assert [r.alias for r in await store.list_records()] == ["shop"]
    assert await store.get("missing") is None
    assert await store.remove("shop") is True and await store.remove("shop") is False


async def test_journal(backend):
    journal = backend.journal()
    assert await journal.get("k") is None
    assert await journal.reserve("k", "op", "sid") is None
    busy = await journal.reserve("k", "op", "sid")
    assert busy is not None and busy.state == "sending"
    entry = await journal.mark("k", "op", "sid", "sent", status=200, note="ok")
    assert entry.status == 200 and (await journal.get("k")).note == "ok"
    fresh = await journal.reserve("k", "op", "sid")
    assert fresh.state == "sent"
    assert (await journal.blocking_entry("k")).state == "sent"
    assert await journal.blocking_entry("k", now=datetime.now(UTC) + timedelta(hours=1)) is None
    assert await journal.blocking_entry("none") is None
    await journal.mark("k", "op", "sid", "failed", status=400)
    assert await journal.reserve("k", "op", "sid") is None
    await journal.mark("k", "op", "sid", "ambiguous", note="timeout")
    assert (await journal.reserve("k", "op", "sid")).state == "ambiguous"
    await journal.mark("k2", "op", "sid", "sent", status=200)
    recent = await journal.recent("sid")
    assert [e.key for e in recent] == ["k2", "k"]
    assert await journal.purge(timedelta(seconds=0)) >= 0
    await journal.mark("old", "op", "sid", "sent", status=200)
    async with backend.connection() as conn:
        await conn.execute("UPDATE writes SET updated_at = now() - interval '3 days' "
                           "WHERE key = 'old'")
    assert await journal.purge(timedelta(days=1)) == 1
    assert await journal.get("old") is None


async def test_reserve_is_atomic_across_instances(pg_url):
    schema = f"t_{uuid.uuid4().hex[:8]}"
    first = await PostgresBackend(pg_url, schema=schema).open()
    second = await PostgresBackend(pg_url, schema=schema).open()
    try:
        results = await asyncio.gather(
            *[(first if i % 2 else second).journal().reserve("same", "op", "sid")
              for i in range(12)]
        )
        assert results.count(None) == 1
        assert all(r.state == "sending" for r in results if r is not None)
    finally:
        await first.close()
        await second.close()


async def test_audit(backend, make_token):
    audit = backend.audit()
    token = make_token()
    rec = await audit.record(client="c", seller="main", sid="1", tool="wb_request",
                             operation="op", arguments={"a": 1}, outcome="error", status=401,
                             error=f"bad {token}", waited_s=0.5, elapsed_s=1.25)
    assert rec.error == "bad <token>"
    await audit.record(client="c", seller="other", sid="2", tool="wb_ping", operation="ping",
                       arguments={}, outcome="ok", status=200)
    recent = await audit.recent()
    assert [r.tool for r in recent] == ["wb_ping", "wb_request"]
    assert recent[1].status == 401 and recent[1].ts.startswith("20")
    assert (await audit.recent(seller="main"))[0].outcome == "error"
    stats = await audit.stats()
    assert stats["calls"] == 2 and stats["by_outcome"] == {"error": 1, "ok": 1}
    assert stats["waited_total_s"] == 0.5


async def test_tenants(backend, make_token):
    registry = PostgresTenantRegistry(backend, FernetCipher(FernetCipher.generate_key()))
    a = registry.for_tenant("a")
    b = registry.for_tenant("b")
    await a.add("shop", make_token(sid="1", acc=4))
    await b.add("shop", make_token(sid="2", acc=4))
    assert (await a.get("shop")).sid == "1" and (await b.get("shop")).sid == "2"
    assert await registry.tenants() == ["a", "b"]
    assert registry.for_tenant("a") is a
    store = a._stores[0]
    await store.add(SellerRecord(alias="shop", token=make_token(sid="9", acc=4), note="r"))
    assert (await store.get("shop")).note == "r" and await store.get("nope") is None
    assert [r.alias for r in await store.list_records()] == ["shop"]
    assert await b.remove("shop") and not await b.remove("shop")
    assert await registry.tenants() == ["a"]


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/ping":
        return httpx.Response(200, json={"Status": "OK"})
    if request.url.path == "/api/v3/supplies":
        return httpx.Response(201, json={"id": "WB-GI-1"})
    return httpx.Response(404, json={"path": request.url.path})


async def test_app_with_postgres(pg_url, tmp_path, make_token):
    schema = f"t_{uuid.uuid4().hex[:8]}"
    config = Config(
        server=ServerConfig(profiles=["orders"], data_dir=tmp_path / "d",
                            database_url=pg_url, database_schema=schema),
        sellers=[SellerConfig(alias="main", token=make_token())],
    )
    app = await build_app(config, env={}, transport=httpx.MockTransport(handler),
                          data_dir=tmp_path / "d")
    try:
        assert app.storage == "postgres"
        server = WbMcpServer(app, confirmer=AutoConfirmer(True))
        added = await server.call_tool("wb_seller_add",
                                       {"alias": "pg", "token": make_token(sid="5")})
        assert added.structured_content["sid"] == "5"
        sellers = await server.call_tool("wb_sellers", {})
        assert [s["alias"] for s in sellers.structured_content["sellers"]] == ["main", "pg"]
        create = app.catalog.by_route("POST", "/api/v3/supplies")
        result = await server.call_tool(create.tool_name,
                                        {"seller": "main", "body": {"name": "x"}})
        assert result.structured_content["data"] == {"id": "WB-GI-1"}
        journal = await server.call_tool("wb_journal", {"seller": "main"})
        assert journal.structured_content["entries"][0]["state"] == "sent"
        audit = await server.call_tool("wb_audit", {})
        assert audit.structured_content["stats"]["calls"] >= 1
        removed = await server.call_tool("wb_seller_remove", {"alias": "pg"})
        assert removed.structured_content["removed"] is True
    finally:
        await app.aclose()
    # второй запуск на той же схеме видит таблицы и не падает на CREATE
    again = await build_app(config, env={}, transport=httpx.MockTransport(handler),
                            data_dir=tmp_path / "d")
    assert (await again.audit.stats())["calls"] >= 1
    await again.aclose()


def test_cli_check_with_database_url(pg_url, tmp_path, make_token, capsys):
    config = tmp_path / "c.toml"
    config.write_text(
        f'[server]\ndata_dir = "{tmp_path / "d"}"\ndatabase_schema = "t_cli"\n'
        f'[[sellers]]\nalias = "a"\ntoken = "{make_token()}"\n',
        encoding="utf-8",
    )
    code = main(["--check", "--config", str(config)], env={"WBMCP_DATABASE_URL": pg_url})
    assert code == 0 and "хранилище postgres" in capsys.readouterr().out
    code = main(["--check", "--config", str(config)],
                env={"WBMCP_DATABASE_URL": "postgresql://nobody@127.0.0.1:1/none"})
    assert code == 2


async def test_cloud_mode_rejects_personal_token_and_closes_pool(pg_url, tmp_path, make_token):
    config = Config(
        server=ServerConfig(mode="cloud", user_agent="svc/1", data_dir=tmp_path / "d",
                            database_url=pg_url, database_schema=f"t_{uuid.uuid4().hex[:8]}"),
        sellers=[SellerConfig(alias="p", token=make_token(acc=3))],
    )
    from wbmcp.config import ConfigError

    with pytest.raises(ConfigError):
        await build_app(config, env={}, transport=httpx.MockTransport(handler),
                        data_dir=tmp_path / "d")
