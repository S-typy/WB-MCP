"""Postgres для облака и нескольких экземпляров: кабинеты, журнал записей, аудит.

Драйвер psycopg 3 с асинхронным пулом. Все таблицы живут в своей схеме, поэтому
несколько серверов могут делить одну базу. Схема и таблицы создаются при старте.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from wbmcp.audit import AuditRecord, build_record
from wbmcp.client.journal import DUPLICATE_WINDOW, JournalEntry, is_blocking
from wbmcp.sellers.crypto import PlainCipher, TokenCipher
from wbmcp.sellers.models import SellerRecord

_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

TABLES = (
    "CREATE TABLE IF NOT EXISTS sellers (alias TEXT PRIMARY KEY, token TEXT NOT NULL, "
    "note TEXT NOT NULL DEFAULT '', added_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "CREATE TABLE IF NOT EXISTS tenant_sellers (tenant TEXT NOT NULL, alias TEXT NOT NULL, "
    "token TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', "
    "added_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (tenant, alias))",
    "CREATE TABLE IF NOT EXISTS writes (key TEXT PRIMARY KEY, operation TEXT NOT NULL, "
    "sid TEXT NOT NULL, state TEXT NOT NULL, status INTEGER, note TEXT NOT NULL DEFAULT '', "
    "updated_at TIMESTAMPTZ NOT NULL)",
    "CREATE INDEX IF NOT EXISTS writes_sid_idx ON writes (sid, updated_at DESC)",
    "CREATE TABLE IF NOT EXISTS audit (id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL, "
    "client TEXT, seller TEXT, sid TEXT, tool TEXT, operation TEXT, args_hash TEXT, "
    "outcome TEXT, status INTEGER, waited_s DOUBLE PRECISION, elapsed_s DOUBLE PRECISION, "
    "error TEXT)",
    "CREATE INDEX IF NOT EXISTS audit_seller_idx ON audit (seller, id DESC)",
)


class PostgresBackend:
    """Пул соединений плюс фабрики хранилищ."""

    def __init__(self, url: str, *, schema: str = "wbmcp", min_size: int = 1,
                 max_size: int = 8) -> None:
        """Схема — обычный идентификатор, чтобы не собирать SQL из чужих строк."""
        if not _SCHEMA_NAME.match(schema):
            raise ValueError(f"недопустимое имя схемы: {schema!r}")
        self.url = url
        self.schema = schema
        self._pool = AsyncConnectionPool(
            url, min_size=min_size, max_size=max_size, open=False,
            kwargs={"autocommit": True, "options": f"-c search_path={schema}"},
        )

    async def open(self) -> PostgresBackend:
        """Открыть пул и создать схему с таблицами."""
        await self._pool.open()
        async with self._pool.connection() as conn:
            await conn.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema))
            )
            for statement in TABLES:
                await conn.execute(statement)
        return self

    async def close(self) -> None:
        """Закрыть пул."""
        await self._pool.close()

    def connection(self):
        """Контекст соединения из пула."""
        return self._pool.connection()

    def sellers(self, cipher: TokenCipher | None = None) -> PostgresSellerStore:
        """Хранилище кабинетов."""
        return PostgresSellerStore(self, cipher)

    def journal(self) -> PostgresWriteJournal:
        """Журнал записей."""
        return PostgresWriteJournal(self)

    def audit(self) -> PostgresAuditLog:
        """Аудит."""
        return PostgresAuditLog(self)


class PostgresSellerStore:
    """Кабинеты в таблице sellers."""

    writable = True

    def __init__(self, backend: PostgresBackend, cipher: TokenCipher | None = None) -> None:
        """Токены шифруются тем же TokenCipher, что и в SQLite."""
        self._backend = backend
        self._cipher = cipher or PlainCipher()

    async def list_records(self) -> list[SellerRecord]:
        """Все кабинеты."""
        async with self._backend.connection() as conn:
            cur = await conn.execute("SELECT alias, token, note FROM sellers ORDER BY alias")
            rows = await cur.fetchall()
        return [SellerRecord(alias=a, token=self._cipher.decrypt(t), note=n) for a, t, n in rows]

    async def get(self, alias: str) -> SellerRecord | None:
        """Кабинет по алиасу."""
        async with self._backend.connection() as conn:
            cur = await conn.execute(
                "SELECT alias, token, note FROM sellers WHERE alias = %s", (alias,)
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return SellerRecord(alias=row[0], token=self._cipher.decrypt(row[1]), note=row[2])

    async def add(self, record: SellerRecord) -> None:
        """Добавить или заменить кабинет."""
        async with self._backend.connection() as conn:
            await conn.execute(
                "INSERT INTO sellers (alias, token, note, added_at) VALUES (%s, %s, %s, now()) "
                "ON CONFLICT (alias) DO UPDATE SET token = EXCLUDED.token, "
                "note = EXCLUDED.note, added_at = now()",
                (record.alias, self._cipher.encrypt(record.token), record.note),
            )

    async def remove(self, alias: str) -> bool:
        """Удалить кабинет."""
        async with self._backend.connection() as conn:
            cur = await conn.execute("DELETE FROM sellers WHERE alias = %s", (alias,))
            return cur.rowcount > 0


def _entry(row: tuple[Any, ...]) -> JournalEntry:
    return JournalEntry(row[0], row[1], row[2], row[3], row[4], row[5], row[6])


class PostgresWriteJournal:
    """Журнал записей в таблице writes с атомарным резервированием."""

    def __init__(self, backend: PostgresBackend) -> None:
        """Журнал живёт в схеме бэкенда."""
        self._backend = backend

    async def get(self, key: str) -> JournalEntry | None:
        """Запись по ключу."""
        async with self._backend.connection() as conn:
            cur = await conn.execute(
                "SELECT key, operation, sid, state, status, note, updated_at "
                "FROM writes WHERE key = %s", (key,)
            )
            row = await cur.fetchone()
        return _entry(row) if row else None

    async def mark(self, key: str, operation: str, sid: str, state: str, *,
                   status: int | None = None, note: str = "") -> JournalEntry:
        """Записать состояние."""
        now = datetime.now(UTC)
        async with self._backend.connection() as conn:
            await conn.execute(
                "INSERT INTO writes (key, operation, sid, state, status, note, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (key) DO UPDATE SET "
                "state = EXCLUDED.state, status = EXCLUDED.status, note = EXCLUDED.note, "
                "updated_at = EXCLUDED.updated_at",
                (key, operation, sid, state, status, note, now),
            )
        return JournalEntry(key, operation, sid, state, status, note, now)

    async def reserve(self, key: str, operation: str, sid: str) -> JournalEntry | None:
        """Занять ключ атомарно; вернуть мешающую запись, если занять нельзя."""
        now = datetime.now(UTC)
        async with self._backend.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO writes (key, operation, sid, state, status, note, updated_at) "
                "VALUES (%s, %s, %s, 'sending', NULL, '', %s) ON CONFLICT (key) DO NOTHING",
                (key, operation, sid, now),
            )
            if cur.rowcount == 1:
                return None
            cur = await conn.execute(
                "UPDATE writes SET state = 'sending', status = NULL, note = '', updated_at = %s "
                "WHERE key = %s AND state NOT IN ('ambiguous', 'sending', 'reserved') "
                "AND NOT (state = 'sent' AND updated_at > %s)",
                (now, key, now - DUPLICATE_WINDOW),
            )
            if cur.rowcount == 1:
                return None
        return await self.get(key)

    async def blocking_entry(self, key: str, now: datetime | None = None) -> JournalEntry | None:
        """Запись, из-за которой повтор без force нельзя."""
        entry = await self.get(key)
        if entry is not None and is_blocking(entry, now):
            return entry
        return None

    async def recent(self, sid: str, limit: int = 50) -> list[JournalEntry]:
        """Последние записи кабинета."""
        async with self._backend.connection() as conn:
            cur = await conn.execute(
                "SELECT key, operation, sid, state, status, note, updated_at FROM writes "
                "WHERE sid = %s ORDER BY updated_at DESC LIMIT %s", (sid, limit)
            )
            rows = await cur.fetchall()
        return [_entry(r) for r in rows]

    async def purge(self, older_than: timedelta) -> int:
        """Удалить завершённые записи старше срока; вернуть число удалённых."""
        async with self._backend.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM writes WHERE state IN ('sent', 'failed') AND updated_at < %s",
                (datetime.now(UTC) - older_than,),
            )
            return cur.rowcount


class PostgresAuditLog:
    """Аудит в таблице audit."""

    def __init__(self, backend: PostgresBackend) -> None:
        """Аудит живёт в схеме бэкенда."""
        self._backend = backend

    async def record(self, **fields: Any) -> AuditRecord:
        """Записать вызов."""
        record = build_record(**fields)
        async with self._backend.connection() as conn:
            await conn.execute(
                "INSERT INTO audit (ts, client, seller, sid, tool, operation, args_hash, "
                "outcome, status, waited_s, elapsed_s, error) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (datetime.fromisoformat(record.ts), record.client, record.seller, record.sid,
                 record.tool, record.operation, record.args_hash, record.outcome, record.status,
                 record.waited_s, record.elapsed_s, record.error),
            )
        return record

    async def recent(self, limit: int = 50, seller: str | None = None) -> list[AuditRecord]:
        """Последние записи, новые сверху."""
        query = ("SELECT ts, client, seller, sid, tool, operation, args_hash, outcome, status, "
                 "waited_s, elapsed_s, error FROM audit")
        params: tuple[Any, ...] = ()
        if seller:
            query += " WHERE seller = %s"
            params = (seller,)
        query += " ORDER BY id DESC LIMIT %s"
        async with self._backend.connection() as conn:
            cur = await conn.execute(query, params + (limit,))
            rows = await cur.fetchall()
        return [AuditRecord(row[0].isoformat(timespec="seconds"), *row[1:]) for row in rows]

    async def stats(self) -> dict[str, Any]:
        """Сводка по исходам и кабинетам."""
        async with self._backend.connection() as conn:
            cur = await conn.execute("SELECT outcome, COUNT(*) FROM audit GROUP BY outcome")
            by_outcome = dict(await cur.fetchall())
            cur = await conn.execute("SELECT seller, COUNT(*) FROM audit GROUP BY seller")
            by_seller = dict(await cur.fetchall())
            cur = await conn.execute("SELECT COALESCE(SUM(waited_s), 0), COUNT(*) FROM audit")
            waited, count = await cur.fetchone()
        return {
            "calls": count,
            "by_outcome": by_outcome,
            "by_seller": by_seller,
            "waited_total_s": round(float(waited), 3),
        }
