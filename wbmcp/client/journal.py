"""Журнал записей: чтобы не отправить одно и то же дважды вслепую.

Ключ — операция + кабинет + канонический JSON аргументов. Состояния:
sending -> sent | failed | ambiguous. Неоднозначный исход (таймаут, 5xx)
блокирует повтор до явного force. Резервирование атомарное, поэтому два
экземпляра сервера с общей базой не отправят одну запись дважды.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from wbmcp.dbutil import connect_sqlite

DUPLICATE_WINDOW = timedelta(minutes=10)
BLOCKING_STATES = ("ambiguous", "sending", "reserved")


@dataclass(frozen=True)
class JournalEntry:
    """Запись журнала."""

    key: str
    operation: str
    sid: str
    state: str
    status: int | None
    note: str
    updated_at: datetime


def make_key(operation_id: str, sid: str, arguments: dict[str, Any]) -> str:
    """Хэш логического ключа записи."""
    canonical = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(f"{operation_id}|{sid}|{canonical}".encode()).hexdigest()
    return digest[:32]


def is_blocking(entry: JournalEntry, now: datetime | None = None) -> bool:
    """Мешает ли запись новому вызову: висит, неоднозначна или совсем свежая."""
    if entry.state in BLOCKING_STATES:
        return True
    now = now or datetime.now(UTC)
    return entry.state == "sent" and now - entry.updated_at < DUPLICATE_WINDOW


class JournalStore(Protocol):
    """Интерфейс журнала записей."""

    async def get(self, key: str) -> JournalEntry | None:
        """Запись по ключу."""
        ...

    async def mark(self, key: str, operation: str, sid: str, state: str, *,
                   status: int | None = None, note: str = "") -> JournalEntry:
        """Записать состояние."""
        ...

    async def reserve(self, key: str, operation: str, sid: str) -> JournalEntry | None:
        """Занять ключ под отправку; вернуть мешающую запись, если занять нельзя."""
        ...

    async def recent(self, sid: str, limit: int = 50) -> list[JournalEntry]:
        """Последние записи кабинета."""
        ...


class WriteJournal:
    """Журнал в SQLite; для тестов подходит и ``:memory:``."""

    def __init__(self, path: Path | str) -> None:
        """Открыть журнал; таблица создаётся при первом обращении."""
        self._conn = connect_sqlite(path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS writes (key TEXT PRIMARY KEY, "
            "operation TEXT NOT NULL, sid TEXT NOT NULL, state TEXT NOT NULL, "
            "status INTEGER, note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)"
        )
        self._conn.commit()

    def _get(self, key: str) -> JournalEntry | None:
        row = self._conn.execute(
            "SELECT key, operation, sid, state, status, note, updated_at "
            "FROM writes WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return JournalEntry(row[0], row[1], row[2], row[3], row[4], row[5],
                            datetime.fromisoformat(row[6]))

    async def get(self, key: str) -> JournalEntry | None:
        """Запись по ключу."""
        return self._get(key)

    async def mark(self, key: str, operation: str, sid: str, state: str, *,
                   status: int | None = None, note: str = "") -> JournalEntry:
        """Записать состояние."""
        now = datetime.now(UTC)
        self._conn.execute(
            "INSERT INTO writes (key, operation, sid, state, status, note, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET state = excluded.state, "
            "status = excluded.status, note = excluded.note, updated_at = excluded.updated_at",
            (key, operation, sid, state, status, note, now.isoformat()),
        )
        self._conn.commit()
        return JournalEntry(key, operation, sid, state, status, note, now)

    async def reserve(self, key: str, operation: str, sid: str) -> JournalEntry | None:
        """Занять ключ: вставка, если пусто, иначе перехват старой записи."""
        now = datetime.now(UTC)
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO writes (key, operation, sid, state, status, note, updated_at) "
            "VALUES (?, ?, ?, 'sending', NULL, '', ?)",
            (key, operation, sid, now.isoformat()),
        )
        if cur.rowcount == 1:
            self._conn.commit()
            return None
        cur = self._conn.execute(
            "UPDATE writes SET state = 'sending', status = NULL, note = '', updated_at = ? "
            "WHERE key = ? AND state NOT IN ('ambiguous', 'sending', 'reserved') "
            "AND NOT (state = 'sent' AND updated_at > ?)",
            (now.isoformat(), key, (now - DUPLICATE_WINDOW).isoformat()),
        )
        self._conn.commit()
        if cur.rowcount == 1:
            return None
        return self._get(key)

    async def blocking_entry(self, key: str, now: datetime | None = None) -> JournalEntry | None:
        """Запись, из-за которой повтор без force нельзя."""
        entry = self._get(key)
        if entry is not None and is_blocking(entry, now):
            return entry
        return None

    async def recent(self, sid: str, limit: int = 50) -> list[JournalEntry]:
        """Последние записи кабинета, новые сверху."""
        rows = self._conn.execute(
            "SELECT key, operation, sid, state, status, note, updated_at FROM writes "
            "WHERE sid = ? ORDER BY updated_at DESC LIMIT ?",
            (sid, limit),
        ).fetchall()
        return [JournalEntry(r[0], r[1], r[2], r[3], r[4], r[5], datetime.fromisoformat(r[6]))
                for r in rows]
