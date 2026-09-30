"""Кэш ответов: правила TTL, хранилища, интеграция с инструментами."""

import fnmatch
import json

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from wbmcp.app import build_app
from wbmcp.auth import ClientPrincipal
from wbmcp.cache import (
    DEFAULT_TTL_RULES,
    CacheEntry,
    MemoryCache,
    ResponseCache,
    SqliteCache,
    make_cache_key,
    ttl_for,
)
from wbmcp.cloud.redis_cache import RedisCache
from wbmcp.config import CacheConfig, Config, ConfigError, SellerConfig, ServerConfig, load_config
from wbmcp.server import WbMcpServer
from wbmcp.tools.confirm import AutoConfirmer


def entry(payload=None, sid="s", section="sec", op="op", stored=0.0, ttl=10.0):
    return CacheEntry(payload or {"v": 1}, stored, stored + ttl, sid, section, op)


async def test_memory_cache_ttl_lru_invalidate():
    cache = MemoryCache(max_entries=2)
    await cache.set("a", entry(stored=0, ttl=10))
    await cache.set("b", entry(stored=0, ttl=10, section="other"))
    assert (await cache.get("a", 5)).payload == {"v": 1}
    assert await cache.get("a", 10) is None            # истекла и удалена
    assert await cache.count() == 1
    await cache.set("a", entry())
    await cache.set("c", entry())                       # вытесняет самую старую (b)
    assert await cache.count() == 2 and await cache.get("b", 1) is None
    assert await cache.invalidate(section="sec") == 2
    assert await cache.count() == 0
    await cache.set("x", entry(sid="1"))
    await cache.set("y", entry(sid="2"))
    assert await cache.invalidate(sid="1") == 1 and await cache.invalidate() == 1


async def test_sqlite_cache_persists_and_prunes(tmp_path):
    path = tmp_path / "c" / "cache.db"
    cache = SqliteCache(path, max_entries=2)
    await cache.set("a", entry(payload={"x": [1, 2]}, stored=100, ttl=50))
    again = SqliteCache(path)
    assert (await again.get("a", 120)).payload == {"x": [1, 2]}
    assert await again.get("a", 200) is None
    await cache.set("b", entry(stored=300, ttl=50))     # чистит истёкшую a
    assert await cache.count() == 1
    await cache.set("c", entry(stored=301, ttl=50))
    await cache.set("d", entry(stored=302, ttl=50))     # лишняя b вылетает
    assert await cache.count() == 2 and await cache.get("b", 303) is None
    assert await cache.invalidate(sid="s", section="sec") == 2
    assert await cache.invalidate() == 0


class FakeRedis:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, *, nx=False, ex=None):
        self.data[key] = value
        return True

    async def delete(self, key):
        self.data.pop(key, None)

    async def keys(self, pattern):
        return [k.encode() for k in self.data if fnmatch.fnmatch(k, pattern)]


async def test_redis_cache():
    redis = FakeRedis()
    cache = RedisCache(redis)
    await cache.set("k1", entry(sid="1", section="a", stored=0, ttl=10))
    await cache.set("k2", entry(sid="1", section="b", stored=0, ttl=10))
    await cache.set("k3", entry(sid="2", section="a", stored=0, ttl=10))
    assert await cache.count() == 3
    assert (await cache.get("k1", 5)).section == "a"
    assert await cache.get("k1", 11) is None             # истекла по времени
    assert await cache.get("nope", 0) is None
    assert await cache.invalidate(sid="1", section="a") == 1
    assert await cache.count() == 2
    assert await cache.invalidate(section="a") == 1     # k3
    assert await cache.invalidate() == 1                 # k2
    assert await cache.count() == 0
    redis.data["wbmcp:cache:k:z"] = json.dumps({"payload": {}, "stored_at": 0, "expires_at": 9,
                                                "sid": "s", "section": "x", "operation": "o"})
    assert (await cache.get("z", 1)).operation == "o"


def test_ttl_rules(catalog):
    cards = catalog.by_route("POST", "/content/v2/get/cards/list")
    orders = catalog.by_route("GET", "/api/v3/orders/new")
    ping = catalog.by_route("GET", "/ping")
    warehouses = catalog.by_route("GET", "/api/v3/warehouses")
    status = catalog.by_route("GET", "/api/v1/warehouse_remains/tasks/{task_id}/status")
    supplies = catalog.by_route("POST", "/api/v3/supplies")
    rules = dict(DEFAULT_TTL_RULES)
    assert ttl_for(cards, rules, 0) == 300 and ttl_for(warehouses, rules, 0) == 3600
    assert ttl_for(orders, rules, 0) == 0 and ttl_for(orders, rules, 60) == 60
    assert ttl_for(ping, rules, 60) == 0 and not ping.cacheable
    assert not status.cacheable and ttl_for(status, {"*": 100}, 100) == 0
    assert not supplies.cacheable
    assert ttl_for(cards, {**rules, f"op:{cards.id}": 7}, 0) == 7
    assert ttl_for(cards, {**rules, "path:POST /content/v2/get/cards/list": 8}, 0) == 8
    assert ttl_for(cards, {**rules, "path:/content/v2/get/cards/list": 9}, 0) == 9
    assert ttl_for(orders, {"section:orders-fbs": 11}, 0) == 11
    assert ttl_for(orders, {"*": 12}, 0) == 12


def test_cache_key_and_config():
    same = make_cache_key("1", "op", {"b": 2, "a": 1})
    assert make_cache_key("1", "op", {"a": 1, "b": 2}) == same
    assert make_cache_key("1", "op", {}) != make_cache_key("2", "op", {})
    cfg = CacheConfig(backend="sqlite", ttl={"section:rates": 10})
    assert cfg.default_ttl_s == 0 and cfg.ttl["section:rates"] == 10
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        file = Path(d) / "c.toml"
        file.write_text('[server.cache]\nbackend = "sqlite"\ndefault_ttl_s = 30\n'
                        '[server.cache.ttl]\n"section:rates" = 7200\n"*" = 5\n',
                        encoding="utf-8")
        loaded = load_config(file, env={})
    assert loaded.server.cache.backend == "sqlite"
    assert loaded.server.cache.ttl == {"section:rates": 7200, "*": 5}


class Backend:
    def __init__(self):
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        path = request.url.path
        if path == "/api/v3/warehouses":
            return httpx.Response(200, json=[{"id": 7, "n": self.calls}])
        if path == "/content/v2/get/cards/list":
            body = json.loads(request.content)
            if "nmID" not in body["settings"]["cursor"]:
                return httpx.Response(200, json={
                    "cards": [{"nmID": 1}, {"nmID": 2}],
                    "cursor": {"updatedAt": "t", "nmID": 2, "total": 2}})
            return httpx.Response(200, json={"cards": [{"nmID": 3}],
                                             "cursor": {"updatedAt": "t2", "nmID": 3, "total": 1}})
        if path == "/api/v3/orders/new":
            return httpx.Response(200, json={"orders": [self.calls]})
        if path == "/api/v3/stocks/7" and request.method == "PUT":
            return httpx.Response(204)
        if path == "/api/v1/seller-info":
            return httpx.Response(500, text="oops")
        return httpx.Response(404, json={"path": path})


@pytest.fixture
def backend():
    return Backend()


@pytest.fixture
async def app(tmp_path, make_token, backend):
    config = Config(server=ServerConfig(profiles=["core"], data_dir=tmp_path / "d",
                                        cache=CacheConfig(ttl={"section:orders-fbs": 60})),
                    sellers=[SellerConfig(alias="main", token=make_token())])
    built = await build_app(config, env={}, transport=httpx.MockTransport(backend),
                            data_dir=tmp_path / "d")
    yield built
    await built.aclose()


@pytest.fixture
def server(app):
    return WbMcpServer(app, confirmer=AutoConfirmer(True))


def payload(result):
    return result.structured_content


async def test_hit_miss_fresh_and_stats(server, app, backend):
    first = payload(await server.call_tool("wb_warehouses", {}))
    assert first["cache"] == {"hit": False, "ttl_s": 3600} and backend.calls == 1
    second = payload(await server.call_tool("wb_warehouses", {}))
    assert second["cache"]["hit"] is True and second["data"] == first["data"]
    assert backend.calls == 1
    third = payload(await server.call_tool("wb_warehouses", {"fresh": True}))
    assert third["cache"] == {"hit": False, "ttl_s": 3600} and backend.calls == 2
    stats = payload(await server.call_tool("wb_cache", {}))
    assert stats["hits"] == 1 and stats["misses"] == 1 and stats["entries"] == 1
    assert stats["backend"] == "memory" and stats["rules"]["section:orders-fbs"] == 60
    text = app.metrics.render()
    assert 'wbmcp_cache_total{result="hit"} 1' in text
    audit = await app.audit.recent()
    assert audit[1].outcome == "cache"


async def test_cards_catalog_served_from_cache(server, backend):
    first = payload(await server.call_tool("wb_cards", {"limit": 2}))
    assert first["count"] == 3 and backend.calls == 2
    second = payload(await server.call_tool("wb_cards", {"limit": 2}))
    assert second["count"] == 3 and backend.calls == 2      # обе страницы из кэша
    await server.call_tool("wb_cards", {"limit": 2, "fresh": True})
    assert backend.calls == 4


async def test_write_invalidates_section_and_errors_not_cached(server, backend):
    orders = payload(await server.call_tool("wb_orders_new", {}))
    assert orders["cache"]["ttl_s"] == 60
    await server.call_tool("wb_warehouses", {})
    result = payload(await server.call_tool(
        "wb_stocks_set", {"warehouse_id": 7, "stocks": [{"sku": "a", "amount": 1}]}))
    assert result["cache"] == {"invalidated": 1, "section": "item-management"}
    stats = payload(await server.call_tool("wb_cache", {}))
    assert stats["entries"] == 1                              # заказы FBS остались
    with pytest.raises(ToolError):
        await server.call_tool("wb_seller_info", {})
    assert (await server.app.cache.stats())["entries"] == 1
    dry = payload(await server.call_tool(
        "wb_request", {"method": "GET", "path": "/api/v3/warehouses", "dry_run": True}))
    assert "cache" not in dry
    cleared = payload(await server.call_tool("wb_cache", {"action": "clear", "seller": "main"}))
    assert cleared["cleared"] == 1
    assert payload(await server.call_tool("wb_cache", {"action": "clear"}))["cleared"] == 0
    with pytest.raises(ToolError):
        await server.call_tool("wb_cache", {"action": "explode"})
    with pytest.raises(ToolError):
        await server.call_tool("wb_cache", {"action": "clear", "seller": "ghost"})


async def test_schema_has_fresh_only_for_cacheable(catalog):
    from wbmcp.catalog import Catalog

    cards = Catalog.input_schema(catalog.by_route("POST", "/content/v2/get/cards/list"))
    assert "fresh" in cards["properties"]
    supplies = Catalog.input_schema(catalog.by_route("POST", "/api/v3/supplies"))
    assert "fresh" not in supplies["properties"]


async def test_disabled_cache_and_backends(tmp_path, make_token, backend, monkeypatch):
    config = Config(server=ServerConfig(profiles=["core"], data_dir=tmp_path / "off",
                                        cache=CacheConfig(enabled=False)),
                    sellers=[SellerConfig(alias="main", token=make_token())])
    app = await build_app(config, env={}, transport=httpx.MockTransport(backend),
                          data_dir=tmp_path / "off")
    server = WbMcpServer(app, confirmer=AutoConfirmer(True))
    result = payload(await server.call_tool("wb_warehouses", {}))
    assert "cache" not in result
    await app.aclose()

    config = Config(server=ServerConfig(profiles=["core"], data_dir=tmp_path / "sq",
                                        cache=CacheConfig(backend="sqlite")),
                    sellers=[SellerConfig(alias="main", token=make_token())])
    app = await build_app(config, env={}, transport=httpx.MockTransport(backend),
                          data_dir=tmp_path / "sq")
    assert app.cache.backend == "sqlite" and (tmp_path / "sq" / "cache.db").exists()
    await app.aclose()

    config = Config(server=ServerConfig(profiles=["core"], data_dir=tmp_path / "rd",
                                        cache=CacheConfig(backend="redis")),
                    sellers=[SellerConfig(alias="main", token=make_token())])
    with pytest.raises(ConfigError):
        await build_app(config, env={}, transport=httpx.MockTransport(backend),
                        data_dir=tmp_path / "rd")

    import wbmcp.cloud.redis_store as redis_store

    fake = FakeRedis()

    async def aclose():
        return None

    fake.aclose = aclose
    monkeypatch.setattr(redis_store, "connect_redis", lambda url: fake)
    config = Config(server=ServerConfig(profiles=["core"], data_dir=tmp_path / "rd2",
                                        redis_url="redis://x", cache=CacheConfig(backend="redis")),
                    sellers=[SellerConfig(alias="main", token=make_token())])
    app = await build_app(config, env={}, transport=httpx.MockTransport(backend),
                          data_dir=tmp_path / "rd2")
    server = WbMcpServer(app, confirmer=AutoConfirmer(True))
    await server.call_tool("wb_warehouses", {})
    hit = payload(await server.call_tool("wb_warehouses", {}))
    assert hit["cache"]["hit"] is True and any(k.startswith("wbmcp:cache:") for k in fake.data)
    await app.aclose()


async def test_response_cache_wrapper():
    cache = ResponseCache(MemoryCache(), clock=lambda: 100.0, rules={"*": 5}, default_ttl_s=0)
    assert await cache.lookup("k") is None and cache.misses == 1
    stats = await cache.stats()
    assert stats["entries"] == 0 and stats["rules"] == {"*": 5}


async def test_post_reads_are_not_writes(catalog, server, backend, monkeypatch):
    cards = catalog.by_route("POST", "/content/v2/get/cards/list")
    assert cards.is_write and not cards.mutating
    from wbmcp.auth import ClientPrincipal, required_scopes

    assert required_scopes(cards) == {"read"}
    reader = ClientPrincipal("r", frozenset({"read"}), None)
    monkeypatch.setattr(server.app, "principal", lambda: reader)
    first = await server.call_tool("wb_cards", {"limit": 2, "fresh": True})
    second = await server.call_tool("wb_cards", {"limit": 2, "fresh": True})
    assert first.structured_content["count"] == second.structured_content["count"] == 3
    assert await server.app.journal.recent(server.app.sellers._stores[0]._records[0].alias) == []


async def test_cache_hit_requires_token_access(server, app, backend, make_token):
    # склады: категория Маркетплейс (бит 4); второй токен того же продавца без неё
    warm = payload(await server.call_tool("wb_warehouses", {"seller": "main"}))
    assert warm["cache"]["hit"] is False and backend.calls == 1
    await app.sellers.add("narrow", make_token(s=1 << 1))
    with pytest.raises(ToolError, match="нет категории"):
        await server.call_tool("wb_warehouses", {"seller": "narrow"})
    await app.sellers.add("stale", make_token(exp=1))
    with pytest.raises(ToolError, match="истёк"):
        await server.call_tool("wb_warehouses", {"seller": "stale"})
    assert backend.calls == 1
    audit = await app.audit.recent()
    assert audit[0].outcome == "denied" and audit[1].outcome == "denied"
    # песочница с тем же sid живёт в своём ключе и не получает боевой ответ
    await app.sellers.add("box", make_token(t=True, acc=2))
    boxed = payload(await server.call_tool("wb_warehouses", {"seller": "box"}))
    assert boxed["cache"]["hit"] is False and backend.calls == 2
    assert "sandbox" in boxed["url"]


async def test_cache_clear_respects_client_sellers(server, app, backend, make_token, monkeypatch):
    await app.sellers.add("second", make_token(sid="2"))
    await server.call_tool("wb_warehouses", {"seller": "main"})
    await server.call_tool("wb_warehouses", {"seller": "second"})
    assert (await app.cache.stats())["entries"] == 2
    reader = ClientPrincipal("agent", frozenset({"read"}), frozenset({"second"}))
    monkeypatch.setattr(app, "principal", lambda: reader)
    with pytest.raises(ToolError, match="не разрешён"):
        await server.call_tool("wb_cache", {"action": "clear", "seller": "main"})
    cleared = payload(await server.call_tool("wb_cache", {"action": "clear"}))
    assert cleared["cleared"] == 1 and "admin" in cleared["note"]
    assert (await app.cache.stats())["entries"] == 1      # запись main осталась
    admin = ClientPrincipal("root", frozenset({"read", "admin"}), None)
    monkeypatch.setattr(app, "principal", lambda: admin)
    await server.call_tool("wb_warehouses", {"seller": "second"})
    cleared = payload(await server.call_tool("wb_cache", {"action": "clear"}))
    assert cleared["cleared"] == 2 and (await app.cache.stats())["entries"] == 0
