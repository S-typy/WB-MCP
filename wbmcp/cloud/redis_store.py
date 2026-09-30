# Copyright (c) 2026 S-typy
#
# Облачный режим wb-mcp распространяется на условиях коммерческой лицензии,
# см. wbmcp/cloud/LICENSE. Ядро сервера лицензировано под GNU AGPL-3.0,
# см. LICENSE в корне репозитория.
"""Состояние лимитера в Redis: общий бюджет кабинета для всех экземпляров."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

from wbmcp.ratelimit.bucket import BucketState


class RedisLike(Protocol):
    """Минимум от асинхронного клиента Redis, который нам нужен."""

    async def get(self, key: str) -> Any:
        """Значение по ключу."""
        ...

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> Any:
        """Записать значение; nx — только если ключа нет, ex — TTL в секундах."""
        ...

    async def delete(self, key: str) -> Any:
        """Удалить ключ."""
        ...

    async def keys(self, pattern: str) -> Any:
        """Ключи по шаблону."""
        ...


def connect_redis(url: str) -> Any:
    """Асинхронный клиент redis-py по адресу; соединение открывается при первой команде."""
    try:
        import redis.asyncio as redis_asyncio
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise RuntimeError("для redis_url нужен пакет redis: pip install 'wb-mcp[redis]'") from exc
    return redis_asyncio.from_url(url)


class RedisLimiterStore:
    """LimiterStore поверх Redis; блокировка — SET NX с TTL.

    Состояние общее для всех экземпляров, поэтому время в нём — настенные часы
    (лимитер выбирает их сам по признаку ``shared``); часы машин должны быть синхронизированы.
    """

    shared = True

    def __init__(self, redis: RedisLike, *, prefix: str = "wbmcp:limit:", lock_ttl_s: int = 5,
                 lock_poll_s: float = 0.01) -> None:
        """Клиент Redis передаётся снаружи: сервер сам соединений не открывает."""
        self._redis = redis
        self._prefix = prefix
        self._lock_ttl_s = lock_ttl_s
        self._lock_poll_s = lock_poll_s

    def _key(self, key: str) -> str:
        return self._prefix + key

    async def get(self, key: str) -> BucketState | None:
        """Состояние ведра."""
        raw = await self._redis.get(self._key(key))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        return BucketState(tokens=data["tokens"], updated_at=data["updated_at"],
                           blocked_until=data.get("blocked_until", 0.0))

    async def set(self, key: str, state: BucketState) -> None:
        """Сохранить состояние; ведро без обращений сутки — забыть."""
        payload = json.dumps({"tokens": state.tokens, "updated_at": state.updated_at,
                              "blocked_until": state.blocked_until})
        await self._redis.set(self._key(key), payload, ex=86400)

    @asynccontextmanager
    async def lock(self, key: str) -> AsyncIterator[None]:
        """Захватить замок ведра; чужой замок ждём короткими опросами."""
        lock_key = self._key(key) + ":lock"
        token = uuid.uuid4().hex
        while not await self._redis.set(lock_key, token, nx=True, ex=self._lock_ttl_s):
            await asyncio.sleep(self._lock_poll_s)
        try:
            yield
        finally:
            current = await self._redis.get(lock_key)
            if isinstance(current, bytes):
                current = current.decode("utf-8")
            if current == token:
                await self._redis.delete(lock_key)

    async def keys(self, prefix: str) -> list[str]:
        """Ключи вёдер с данным префиксом (без замков)."""
        raw = await self._redis.keys(self._key(prefix) + "*")
        result = []
        for item in raw:
            name = item.decode("utf-8") if isinstance(item, bytes) else str(item)
            if name.endswith(":lock"):
                continue
            result.append(name[len(self._prefix):])
        return sorted(result)
