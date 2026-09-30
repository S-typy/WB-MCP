"""Хранилища записей о кабинетах: файл настроек и SQLite.

Интерфейс асинхронный, чтобы Postgres не блокировал цикл событий. SQLite
отвечает за микросекунды, поэтому зовётся напрямую, без потоков.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from wbmcp.config import SellerConfig
from wbmcp.dbutil import connect_sqlite
from wbmcp.sellers.crypto import PlainCipher, TokenCipher
from wbmcp.sellers.models import SellerRecord


class ReadOnlyStoreError(RuntimeError):
    """Хранилище не умеет менять записи."""


class SellerStore(Protocol):
    """Минимальный интерфейс хранилища кабинетов."""

    writable: bool

    async def list_records(self) -> list[SellerRecord]:
        """Все записи."""
        ...

    async def get(self, alias: str) -> SellerRecord | None:
        """Запись по алиасу или None."""
        ...

    async def add(self, record: SellerRecord) -> None:
        """Добавить или заменить запись."""
        ...

    async def remove(self, alias: str) -> bool:
        """Удалить запись; True, если она была."""
        ...


class ConfigSellerStore:
    """Кабинеты из файла настроек, только чтение."""

    writable = False

    def __init__(
        self, entries: Iterable[SellerConfig], env: Mapping[str, str] | None = None
    ) -> None:
        """Токены достаются сразу, чтобы ошибки настроек всплыли на старте."""
        env = os.environ if env is None else env
        self._records = [
            SellerRecord(alias=e.alias, token=e.resolve_token(env), note=e.note) for e in entries
        ]

    async def list_records(self) -> list[SellerRecord]:
        """Все записи из настроек."""
        return list(self._records)

    async def get(self, alias: str) -> SellerRecord | None:
        """Запись по алиасу."""
        return next((r for r in self._records if r.alias == alias), None)

    async def add(self, record: SellerRecord) -> None:
        """Файл настроек из сервера не правим."""
        raise ReadOnlyStoreError("кабинеты из файла настроек меняются только в файле")

    async def remove(self, alias: str) -> bool:
        """Файл настроек из сервера не правим."""
        raise ReadOnlyStoreError("кабинеты из файла настроек меняются только в файле")


class SqliteSellerStore:
    """Кабинеты в SQLite, токены через TokenCipher."""

    writable = True

    def __init__(self, path: Path, cipher: TokenCipher | None = None) -> None:
        """Открыть (или создать) базу по пути; каталог создаётся при необходимости."""
        self._path = Path(path)
        self._cipher = cipher or PlainCipher()
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sellers ("
                "alias TEXT PRIMARY KEY, token TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', "
                "added_at TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self._path)

    async def list_records(self) -> list[SellerRecord]:
        """Все записи, токены расшифрованы."""
        with self._connect() as conn:
            rows = conn.execute("SELECT alias, token, note FROM sellers ORDER BY alias").fetchall()
        return [SellerRecord(alias=a, token=self._cipher.decrypt(t), note=n) for a, t, n in rows]

    async def get(self, alias: str) -> SellerRecord | None:
        """Запись по алиасу."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT alias, token, note FROM sellers WHERE alias = ?", (alias,)
            ).fetchone()
        if row is None:
            return None
        return SellerRecord(alias=row[0], token=self._cipher.decrypt(row[1]), note=row[2])

    async def add(self, record: SellerRecord) -> None:
        """Добавить или заменить запись."""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sellers (alias, token, note, added_at) "
                "VALUES (?, ?, ?, ?)",
                (record.alias, self._cipher.encrypt(record.token), record.note,
                 datetime.now(UTC).isoformat()),
            )

    async def remove(self, alias: str) -> bool:
        """Удалить запись."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM sellers WHERE alias = ?", (alias,))
        return cur.rowcount > 0
