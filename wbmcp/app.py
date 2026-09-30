"""Сборка приложения из настроек: все компоненты в одном месте."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token

from wbmcp.audit import AuditLog
from wbmcp.auth import (
    AccessDeniedError,
    ClientPrincipal,
    ClientRegistry,
    principal_from_access_token,
    scopes_for_result,
)
from wbmcp.cache import DEFAULT_TTL_RULES, MemoryCache, ResponseCache, SqliteCache
from wbmcp.catalog import Catalog
from wbmcp.client import FileStore, WbClient, WriteJournal
from wbmcp.config import Config, ConfigError, find_specs_dir
from wbmcp.metrics import Metrics, default_metrics
from wbmcp.ratelimit import RateLimiter
from wbmcp.results import ResultStore
from wbmcp.sellers import ConfigSellerStore, SellerRegistry, SqliteSellerStore
from wbmcp.sellers.crypto import cipher_from_env
from wbmcp.sellers.models import Seller


@dataclass
class AppContext:
    """Всё, что нужно инструментам."""

    config: Config
    catalog: Catalog
    sellers: SellerRegistry
    limiter: RateLimiter
    client: WbClient
    journal: WriteJournal
    files: FileStore
    audit: AuditLog
    clients: ClientRegistry | None
    data_dir: Path
    metrics: Metrics = field(default_factory=default_metrics)
    backend: Any = None
    redis: Any = None
    cache: ResponseCache = field(default_factory=lambda: ResponseCache(MemoryCache()))
    results: ResultStore = field(
        default_factory=lambda: ResultStore(Path("~/.wb-mcp/results").expanduser())
    )
    # Запросы приходят по сети (HTTP): файлы с диска сервера клиентам не отдаются.
    remote: bool = False
    # Политика облака (wbmcp.cloud.CloudPolicy): проверяет токен при добавлении и перед вызовом.
    policy: Any = None

    @property
    def storage(self) -> str:
        """Какое хранилище используется: sqlite или postgres."""
        return "postgres" if self.backend is not None else "sqlite"

    @property
    def limits(self) -> str:
        """Где живёт состояние лимитера: memory или redis."""
        return "redis" if self.redis is not None else "memory"

    def principal(self) -> ClientPrincipal:
        """Кто сейчас зовёт инструмент: bearer-ключ в HTTP или локальный клиент."""
        return principal_from_access_token(get_access_token(), self.clients)

    async def seller_for(self, alias: str | None, principal: ClientPrincipal) -> Seller:
        """Кабинет по алиасу с проверкой, что он разрешён клиенту."""
        seller = await self.sellers.resolve(alias)
        principal.check_seller(seller.alias)
        if self.policy is not None:
            try:
                self.policy.check_token(seller.info, seller.alias)
            except ValueError as exc:  # CloudPolicyError: реестр изменили мимо сервера
                raise AccessDeniedError(str(exc)) from exc
        return seller

    @property
    def inline_limit(self) -> int:
        """До какого размера файл из ответа отдаётся ещё и в base64."""
        return min(self.config.server.max_file_bytes, 512 * 1024)

    async def aclose(self) -> None:
        """Закрыть соединения с WB и с базой."""
        await self.client.aclose()
        if self.backend is not None:
            await self.backend.close()
        if self.redis is not None:
            await self.redis.aclose()


async def build_app(
    config: Config,
    *,
    env: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    data_dir: Path | None = None,
) -> AppContext:
    """Собрать компоненты по настройкам; transport подменяется в тестах.

    С database_url кабинеты, журнал записей и аудит живут в Postgres, иначе в SQLite
    в каталоге данных. С redis_url состояние лимитера общее для всех экземпляров.
    Файлы отчётов всегда на диске.
    """
    env = os.environ if env is None else env
    server = config.server
    data_dir = Path(data_dir) if data_dir else server.resolved_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    catalog = Catalog.load(find_specs_dir(server.specs_dir, env))
    cipher = cipher_from_env(env, server.token_key_env)
    backend = None
    if server.database_url:
        from wbmcp.postgres import PostgresBackend

        backend = await PostgresBackend(
            server.database_url, schema=server.database_schema,
            max_size=server.database_pool_size,
        ).open()
        stores = [ConfigSellerStore(config.sellers, env), backend.sellers(cipher)]
        journal: Any = backend.journal()
        audit: Any = backend.audit()
    else:
        stores = [ConfigSellerStore(config.sellers, env),
                  SqliteSellerStore(data_dir / "sellers.db", cipher)]
        journal = WriteJournal(data_dir / "journal.db")
        audit = AuditLog(data_dir / "audit.db")
    policy = None
    if server.mode == "cloud":
        from wbmcp.cloud import CloudPolicy

        policy = CloudPolicy()
    sellers = SellerRegistry(stores, guard=policy.check_token if policy else None)
    redis = None
    if server.redis_url:
        from wbmcp.cloud import RedisLimiterStore
        from wbmcp.cloud.redis_store import connect_redis

        redis = connect_redis(server.redis_url)
        limiter = RateLimiter(RedisLimiterStore(redis))
    else:
        limiter = RateLimiter()
    files = FileStore(data_dir / "files", max_bytes=server.max_file_bytes)
    results = ResultStore(
        data_dir / "results", inline_max_bytes=server.results.inline_max_bytes,
        preview_items=server.results.preview_items, keep_hours=server.results.keep_hours,
        scopes_for=lambda operation_id: scopes_for_result(catalog, operation_id),
    )
    results.purge()
    try:
        cache = _build_cache(server, data_dir, redis)
    except ConfigError:
        if backend is not None:
            await backend.close()
        raise
    client = WbClient(
        limiter=limiter,
        user_agent=server.user_agent,
        timeout_s=server.request_timeout,
        transport=transport,
        journal=journal,
        files=files,
        server_read_only=server.read_only,
        hosts=catalog.hosts(), sandbox_hosts=catalog.hosts(sandbox=True),
    )
    clients = ClientRegistry(config.clients, env) if config.clients else None
    if policy is not None:
        from wbmcp.cloud import CloudPolicyError

        try:
            policy.check_user_agent(server.user_agent)
            await policy.check_registry(sellers)
        except CloudPolicyError as exc:
            if backend is not None:
                await backend.close()
            if redis is not None:
                await redis.aclose()
            raise ConfigError(str(exc)) from exc
    return AppContext(
        config=config,
        catalog=catalog,
        sellers=sellers,
        limiter=limiter,
        client=client,
        journal=journal,
        files=files,
        audit=audit,
        clients=clients,
        data_dir=data_dir,
        backend=backend,
        redis=redis,
        cache=cache,
        results=results,
        policy=policy,
    )


def _build_cache(server: Any, data_dir: Path, redis: Any) -> ResponseCache:
    """Хранилище кэша по настройкам; Redis требует redis_url."""
    cfg = server.cache
    if cfg.backend == "sqlite":
        store: Any = SqliteCache(data_dir / "cache.db", max_entries=cfg.max_entries)
    elif cfg.backend == "redis":
        if redis is None:
            raise ConfigError("cache.backend = redis требует redis_url")
        from wbmcp.cloud.redis_cache import RedisCache

        store = RedisCache(redis)
    else:
        store = MemoryCache(max_entries=cfg.max_entries)
    return ResponseCache(
        store=store, enabled=cfg.enabled, default_ttl_s=cfg.default_ttl_s,
        rules={**DEFAULT_TTL_RULES, **cfg.ttl}, invalidate_on_write=cfg.invalidate_on_write,
        backend=cfg.backend,
    )
