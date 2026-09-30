# Copyright (c) 2026 S-typy
#
# Облачный режим wb-mcp распространяется на условиях коммерческой лицензии,
# см. wbmcp/cloud/LICENSE. Ядро сервера лицензировано под GNU AGPL-3.0,
# см. LICENSE в корне репозитория.
"""Кэш ответов в Redis: общий для всех экземпляров сервера."""

from __future__ import annotations

import json

from wbmcp.cache import CacheEntry
from wbmcp.cloud.redis_store import RedisLike


class RedisCache:
    """CacheStore поверх Redis; TTL держит сам Redis."""

    def __init__(self, redis: RedisLike, *, prefix: str = "wbmcp:cache:") -> None:
        """Ключи: <prefix><sid>:<section>:<key>, чтобы чистить по кабинету и разделу."""
        self._redis = redis
        self._prefix = prefix

    def _name(self, entry_sid: str, section: str, key: str) -> str:
        return f"{self._prefix}{entry_sid}:{section}:{key}"

    async def get(self, key: str, now: float) -> CacheEntry | None:
        """Запись по ключу; ключ хранится и в индексе без кабинета для поиска."""
        raw = await self._redis.get(self._prefix + "k:" + key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        entry = CacheEntry(data["payload"], data["stored_at"], data["expires_at"], data["sid"],
                           data["section"], data["operation"])
        return None if entry.expired(now) else entry

    async def set(self, key: str, entry: CacheEntry) -> None:
        """Сохранить с TTL и продублировать имя в индекс кабинета и раздела."""
        ttl = max(1, int(round(entry.expires_at - entry.stored_at)))
        raw = json.dumps({
            "payload": entry.payload, "stored_at": entry.stored_at,
            "expires_at": entry.expires_at, "sid": entry.sid, "section": entry.section,
            "operation": entry.operation,
        }, ensure_ascii=False, default=str)
        await self._redis.set(self._prefix + "k:" + key, raw, ex=ttl)
        await self._redis.set(self._name(entry.sid, entry.section, key), "1", ex=ttl)

    async def invalidate(self, sid: str | None = None, section: str | None = None) -> int:
        """Удалить записи по шаблону кабинета и раздела."""
        pattern = f"{self._prefix}{sid or '*'}:{section or '*'}:*"
        names = await self._redis.keys(pattern)
        removed = 0
        for name in names:
            text = name.decode("utf-8") if isinstance(name, bytes) else str(name)
            key = text.rsplit(":", 1)[-1]
            await self._redis.delete(self._prefix + "k:" + key)
            await self._redis.delete(text)
            removed += 1
        return removed

    async def count(self) -> int:
        """Число записей."""
        return len(await self._redis.keys(self._prefix + "k:*"))
