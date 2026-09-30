"""Облачный режим: правила, арендаторы, Redis-хранилище лимитера."""

import asyncio

import httpx
import pytest

from wbmcp.app import build_app
from wbmcp.cloud import CloudPolicy, CloudPolicyError, RedisLimiterStore, TenantRegistry
from wbmcp.config import Config, ConfigError, SellerConfig, ServerConfig
from wbmcp.ratelimit import BucketState
from wbmcp.sellers.crypto import FernetCipher
from wbmcp.sellers.models import SellerRecord
from wbmcp.tokens import decode_token


def test_policy(make_token):
    policy = CloudPolicy()
    policy.check_token(decode_token(make_token(acc=4)), "svc")
    policy.check_token(decode_token(make_token(acc=1)), "basic")
    with pytest.raises(CloudPolicyError) as info:
        policy.check_token(decode_token(make_token(acc=3)), "personal")
    assert "персональный" in str(info.value)
    with pytest.raises(CloudPolicyError):
        policy.check_token(decode_token(make_token(acc=2)))
    policy.check_user_agent("my-service/1.0")
    for bad in ("", "wb-mcp/0.1.0.dev0"):
        with pytest.raises(CloudPolicyError):
            policy.check_user_agent(bad)


def payload(result):
    return result.structured_content


async def test_build_app_cloud_mode(tmp_path, make_token):
    async def make(acc, user_agent="shop-service/1.0"):
        config = Config(
            server=ServerConfig(mode="cloud", user_agent=user_agent, data_dir=tmp_path / "c"),
            sellers=[SellerConfig(alias="s", token=make_token(acc=acc))],
        )
        transport = httpx.MockTransport(lambda r: httpx.Response(200))
        return await build_app(config, env={}, transport=transport, data_dir=tmp_path / "c")

    app = await make(4)
    assert app.config.server.mode == "cloud" and app.storage == "sqlite"
    # политика действует и после старта: личный токен не добавить и не использовать
    from mcp.server.mcpserver.exceptions import ToolError

    from wbmcp.sellers.models import SellerRecord
    from wbmcp.server import WbMcpServer
    from wbmcp.tools.confirm import AutoConfirmer

    server = WbMcpServer(app, confirmer=AutoConfirmer(True))
    with pytest.raises(ToolError, match="запрещён"):
        await server.call_tool("wb_seller_add", {"alias": "p", "token": make_token(acc=3)})
    assert await app.sellers.aliases() == ["s"]
    added = payload(await server.call_tool("wb_seller_add",
                                           {"alias": "b", "token": make_token(acc=1, sid="b")}))
    assert added["alias"] == "b"
    # реестр подменили мимо сервера (например, в базе): перед вызовом кабинет отклоняется
    store = next(st for st in app.sellers._stores if st.writable)
    await store.add(SellerRecord(alias="p", token=make_token(acc=3, sid="p"), note=""))
    with pytest.raises(ToolError, match="запрещён"):
        await server.call_tool("wb_seller_info", {"seller": "p"})
    await app.aclose()
    with pytest.raises(ConfigError):
        await make(3)
    with pytest.raises(ConfigError):
        await make(4, user_agent="wb-mcp/x")


async def test_tenant_registry_isolation(tmp_path, make_token):
    cipher = FernetCipher(FernetCipher.generate_key())
    registry = TenantRegistry(tmp_path / "t" / "tenants.db", cipher)
    a = registry.for_tenant("client-a")
    b = registry.for_tenant("client-b")
    await a.add("shop", make_token(sid="1", acc=4))
    await b.add("shop", make_token(sid="2", acc=4))
    assert (await a.get("shop")).sid == "1" and (await b.get("shop")).sid == "2"
    assert await registry.tenants() == ["client-a", "client-b"]
    assert registry.for_tenant("client-a") is a
    assert await b.remove("shop") and not await b.remove("shop")
    assert await b.aliases() == [] and await registry.tenants() == ["client-a"]
    store = a._stores[0]
    await store.add(SellerRecord(alias="shop", token=make_token(sid="9", acc=4), note="replaced"))
    assert (await store.get("shop")).note == "replaced" and await store.get("missing") is None
    assert [r.alias for r in await store.list_records()] == ["shop"]


class FakeRedis:
    def __init__(self):
        self.data = {}
        self.sets = []

    async def get(self, key):
        value = self.data.get(key)
        return value.encode() if isinstance(value, str) else value

    async def set(self, key, value, *, nx=False, ex=None):
        self.sets.append((key, nx, ex))
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    async def delete(self, key):
        self.data.pop(key, None)

    async def keys(self, pattern):
        prefix = pattern.rstrip("*")
        return [k.encode() for k in self.data if k.startswith(prefix)]


def test_shared_store_uses_wall_clock():
    # общее состояние сравнивают разные машины: monotonic там бессмыслен
    import time

    from wbmcp.ratelimit import RateLimiter
    from wbmcp.ratelimit.store import MemoryLimiterStore

    assert RedisLimiterStore.shared and not MemoryLimiterStore.shared
    assert RateLimiter(RedisLimiterStore(FakeRedis()))._clock is time.time
    assert RateLimiter(MemoryLimiterStore())._clock is time.monotonic
    assert RateLimiter(RedisLimiterStore(FakeRedis()), clock=lambda: 5.0)._clock() == 5.0


async def test_redis_store():
    redis = FakeRedis()
    store = RedisLimiterStore(redis, lock_poll_s=0)
    assert await store.get("s:op") is None
    async with store.lock("s:op"):
        assert "wbmcp:limit:s:op:lock" in redis.data
        await store.set("s:op", BucketState(3.5, 10.0, 12.0))
    assert "wbmcp:limit:s:op:lock" not in redis.data
    state = await store.get("s:op")
    assert (state.tokens, state.updated_at, state.blocked_until) == (3.5, 10.0, 12.0)
    assert any(ex == 86400 for _, _, ex in redis.sets)
    await store.set("s:other", BucketState(1.0, 0.0))
    assert await store.keys("s:") == ["s:op", "s:other"]
    # чужой замок: ждём, пока его снимут
    redis.data["wbmcp:limit:busy:lock"] = "someone"

    async def release():
        redis.data.pop("wbmcp:limit:busy:lock")

    asyncio.get_running_loop().call_later(0.01, lambda: asyncio.ensure_future(release()))
    async with store.lock("busy"):
        pass
    # замок, перехваченный другим (TTL истёк), не удаляем

    async def hijack():
        async with store.lock("x"):
            redis.data["wbmcp:limit:x:lock"] = "other"

    await hijack()
    assert redis.data["wbmcp:limit:x:lock"] == "other"
    # строковые значения тоже читаются
    redis.data["wbmcp:limit:str"] = '{"tokens": 1, "updated_at": 2}'
    redis.data["plain-key"] = "x"
    assert (await store.get("str")).blocked_until == 0.0
    assert await store.keys("") == ["s:op", "s:other", "str"]


async def test_build_app_with_redis_url(tmp_path, make_token, monkeypatch):
    import wbmcp.cloud.redis_store as redis_store

    fake = FakeRedis()
    fake.closed = False

    async def aclose():
        fake.closed = True

    fake.aclose = aclose
    monkeypatch.setattr(redis_store, "connect_redis", lambda url: fake)
    config = Config(server=ServerConfig(profiles=["core"], data_dir=tmp_path / "r",
                                        redis_url="redis://localhost:6379/0"),
                    sellers=[SellerConfig(alias="main", token=make_token())])
    handler = httpx.MockTransport(lambda r: httpx.Response(200, json={"Status": "OK"}))
    app = await build_app(config, env={}, transport=handler, data_dir=tmp_path / "r")
    assert app.limits == "redis" and app.redis is fake
    from wbmcp.server import WbMcpServer
    from wbmcp.tools.confirm import AutoConfirmer

    server = WbMcpServer(app, confirmer=AutoConfirmer(False))
    result = await server.call_tool("wb_request", {"method": "GET", "path": "/ping"})
    assert result.structured_content["status"] == 200
    assert any(k.startswith("wbmcp:limit:1234567:") for k in fake.data)
    await app.aclose()
    assert fake.closed


async def test_redis_url_from_env_and_real_client(tmp_path, make_token):
    from wbmcp.cloud.redis_store import connect_redis
    from wbmcp.config import load_config

    config = load_config(env={"WBMCP_REDIS_URL": "redis://127.0.0.1:1/0", "WB_TOKEN": "a.b.c"})
    assert config.server.redis_url == "redis://127.0.0.1:1/0"
    client = connect_redis("redis://127.0.0.1:1/0")
    assert hasattr(client, "set") and hasattr(client, "get")
    await client.aclose()


async def test_cloud_policy_failure_closes_redis(tmp_path, make_token, monkeypatch):
    import wbmcp.cloud.redis_store as redis_store

    fake = FakeRedis()
    closed = []

    async def aclose():
        closed.append(True)

    fake.aclose = aclose
    monkeypatch.setattr(redis_store, "connect_redis", lambda url: fake)
    config = Config(server=ServerConfig(mode="cloud", user_agent="svc/1", data_dir=tmp_path / "c",
                                        redis_url="redis://x"),
                    sellers=[SellerConfig(alias="p", token=make_token(acc=3))])
    with pytest.raises(ConfigError):
        transport = httpx.MockTransport(lambda r: httpx.Response(200))
        await build_app(config, env={}, transport=transport, data_dir=tmp_path / "c")
    assert closed
