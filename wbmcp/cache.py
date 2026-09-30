"""Кэш ответов WB: чтобы не тянуть каталог карточек или справочники каждый раз.

Кэшируются только ответы методов, доступных токену «только чтение», и только
если для метода задан TTL. Умолчания покрывают справочники, склады, тарифы и
список карточек; всё остальное по умолчанию не кэшируется, потому что новые
заказы или остатки нужны свежими. Правила настраиваются в ``[server.cache.ttl]``.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from wbmcp.catalog.models import Operation
from wbmcp.dbutil import connect_sqlite

# Ключи правил: op:<operationId>, path:<METHOD /путь>, path:</префикс пути>,
# section:<раздел>, "*" — для всех кэшируемых методов.
DEFAULT_TTL_RULES: dict[str, float] = {
    "path:/content/v2/object": 86400,
    "path:/content/v2/directory": 86400,
    "path:/api/marketplace/v3/fbs/dictionaries": 86400,
    "path:/api/v3/warehouses": 3600,
    "path:/api/v3/offices": 3600,
    "path:/api/v1/seller-info": 3600,
    "path:/api/communications/v2/news": 600,
    "path:/content/v2/get/cards": 300,
    "section:rates": 3600,
    "path:/ping": 0,
}


@dataclass(frozen=True)
class CacheEntry:
    """Сохранённый ответ и его метаданные."""

    payload: dict[str, Any]
    stored_at: float
    expires_at: float
    sid: str
    section: str
    operation: str

    def expired(self, now: float) -> bool:
        """Истёк ли срок."""
        return now >= self.expires_at


class CacheStore(Protocol):
    """Хранилище записей кэша."""

    async def get(self, key: str, now: float) -> CacheEntry | None:
        """Запись по ключу, если не истекла."""
        ...

    async def set(self, key: str, entry: CacheEntry) -> None:
        """Сохранить запись."""
        ...

    async def invalidate(self, sid: str | None = None, section: str | None = None) -> int:
        """Удалить записи кабинета и/или раздела; вернуть число удалённых."""
        ...

    async def count(self) -> int:
        """Сколько записей лежит (включая истёкшие, если хранилище их не чистит)."""
        ...


class MemoryCache:
    """LRU в памяти процесса."""

    def __init__(self, max_entries: int = 5000) -> None:
        """Ёмкость в записях; старые вытесняются."""
        self._entries: OrderedDict[str, CacheEntry] = OrderedDict()
        self.max_entries = max(1, max_entries)

    async def get(self, key: str, now: float) -> CacheEntry | None:
        """Запись по ключу; истёкшая удаляется."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expired(now):
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry

    async def set(self, key: str, entry: CacheEntry) -> None:
        """Сохранить, вытеснив самые старые при переполнении."""
        self._entries[key] = entry
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    async def invalidate(self, sid: str | None = None, section: str | None = None) -> int:
        """Удалить записи по кабинету и/или разделу."""
        doomed = [
            k for k, e in self._entries.items()
            if (sid is None or e.sid == sid) and (section is None or e.section == section)
        ]
        for key in doomed:
            del self._entries[key]
        return len(doomed)

    async def count(self) -> int:
        """Число записей."""
        return len(self._entries)


class SqliteCache:
    """Кэш в SQLite: переживает перезапуск сервера."""

    def __init__(self, path: Path | str, max_entries: int = 5000) -> None:
        """Открыть базу; таблица создаётся при первом обращении."""
        self._conn = connect_sqlite(path)
        self.max_entries = max(1, max_entries)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, sid TEXT NOT NULL, "
            "section TEXT NOT NULL, operation TEXT NOT NULL, payload TEXT NOT NULL, "
            "stored_at REAL NOT NULL, expires_at REAL NOT NULL)"
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS cache_scope ON cache (sid, section)")
        self._conn.commit()

    async def get(self, key: str, now: float) -> CacheEntry | None:
        """Запись по ключу, если срок не вышел."""
        row = self._conn.execute(
            "SELECT payload, stored_at, expires_at, sid, section, operation FROM cache "
            "WHERE key = ? AND expires_at > ?", (key, now)
        ).fetchone()
        if row is None:
            return None
        return CacheEntry(json.loads(row[0]), row[1], row[2], row[3], row[4], row[5])

    async def set(self, key: str, entry: CacheEntry) -> None:
        """Сохранить, почистить истёкшие и лишние."""
        self._conn.execute(
            "INSERT OR REPLACE INTO cache (key, sid, section, operation, payload, stored_at, "
            "expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key, entry.sid, entry.section, entry.operation,
             json.dumps(entry.payload, ensure_ascii=False, default=str), entry.stored_at,
             entry.expires_at),
        )
        self._conn.execute("DELETE FROM cache WHERE expires_at <= ?", (entry.stored_at,))
        self._conn.execute(
            "DELETE FROM cache WHERE key IN (SELECT key FROM cache ORDER BY stored_at DESC "
            "LIMIT -1 OFFSET ?)", (self.max_entries,)
        )
        self._conn.commit()

    async def invalidate(self, sid: str | None = None, section: str | None = None) -> int:
        """Удалить записи по кабинету и/или разделу."""
        clauses, params = [], []
        if sid is not None:
            clauses.append("sid = ?")
            params.append(sid)
        if section is not None:
            clauses.append("section = ?")
            params.append(section)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = self._conn.execute("DELETE FROM cache" + where, params)
        self._conn.commit()
        return cur.rowcount

    async def count(self) -> int:
        """Число записей в базе."""
        return self._conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]


def make_cache_key(sid: str, operation_id: str, arguments: dict[str, Any]) -> str:
    """Ключ записи: кабинет, операция и канонический JSON аргументов."""
    canonical = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{sid}|{operation_id}|{canonical}".encode()).hexdigest()[:40]


def ttl_for(op: Operation, rules: dict[str, float], default_ttl_s: float) -> float:
    """TTL для операции по правилам; точное совпадение важнее префикса и раздела."""
    if not op.cacheable:
        return 0.0
    if f"op:{op.id}" in rules:
        return rules[f"op:{op.id}"]
    exact = f"path:{op.method} {op.path}"
    if exact in rules:
        return rules[exact]
    best: tuple[int, float] | None = None
    for key, ttl in rules.items():
        if key.startswith("path:/") and op.path.startswith(key[5:]):
            if best is None or len(key) > best[0]:
                best = (len(key), ttl)
    if best is not None:
        return best[1]
    if f"section:{op.section}" in rules:
        return rules[f"section:{op.section}"]
    return rules.get("*", default_ttl_s)


@dataclass
class ResponseCache:
    """Кэш ответов с правилами TTL и счётчиками."""

    store: CacheStore
    enabled: bool = True
    default_ttl_s: float = 0.0
    rules: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TTL_RULES))
    invalidate_on_write: bool = True
    backend: str = "memory"
    clock: Callable[[], float] = time.time
    hits: int = 0
    misses: int = 0

    def ttl(self, op: Operation) -> float:
        """TTL операции или 0, если не кэшируется."""
        if not self.enabled:
            return 0.0
        return max(0.0, ttl_for(op, self.rules, self.default_ttl_s))

    async def lookup(self, key: str) -> CacheEntry | None:
        """Запись по ключу; считает попадания и промахи."""
        entry = await self.store.get(key, self.clock())
        if entry is None:
            self.misses += 1
        else:
            self.hits += 1
        return entry

    async def remember(self, op: Operation, sid: str, key: str, payload: dict[str, Any],
                       ttl_s: float) -> CacheEntry:
        """Сохранить ответ на ``ttl_s`` секунд."""
        now = self.clock()
        entry = CacheEntry(payload=payload, stored_at=now, expires_at=now + ttl_s, sid=sid,
                           section=op.section, operation=op.id)
        await self.store.set(key, entry)
        return entry

    async def invalidate(self, sid: str | None = None, section: str | None = None) -> int:
        """Сбросить записи кабинета и/или раздела."""
        return await self.store.invalidate(sid, section)

    async def stats(self) -> dict[str, Any]:
        """Сводка для инструмента wb_cache."""
        return {
            "enabled": self.enabled,
            "backend": self.backend,
            "entries": await self.store.count(),
            "hits": self.hits,
            "misses": self.misses,
            "default_ttl_s": self.default_ttl_s,
            "invalidate_on_write": self.invalidate_on_write,
            "rules": dict(sorted(self.rules.items())),
        }
