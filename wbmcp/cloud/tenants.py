# Copyright (c) 2026 S-typy
#
# Облачный режим wb-mcp распространяется на условиях коммерческой лицензии,
# см. wbmcp/cloud/LICENSE. Ядро сервера лицензировано под GNU AGPL-3.0,
# см. LICENSE в корне репозитория.
"""Кабинеты нескольких арендаторов: SQLite для одного экземпляра, Postgres для облака."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wbmcp.dbutil import connect_sqlite
from wbmcp.sellers.crypto import PlainCipher, TokenCipher
from wbmcp.sellers.models import SellerRecord
from wbmcp.sellers.registry import SellerRegistry


class TenantSellerStore:
    """Хранилище кабинетов одного арендатора в SQLite; таблица общая, строки разделены."""

    writable = True

    def __init__(self, path: Path, tenant: str, cipher: TokenCipher | None = None) -> None:
        """База создаётся при первом обращении."""
        self._path = Path(path)
        self.tenant = tenant
        self._cipher = cipher or PlainCipher()
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS tenant_sellers (tenant TEXT NOT NULL, "
                "alias TEXT NOT NULL, token TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', "
                "added_at TEXT NOT NULL, PRIMARY KEY (tenant, alias))"
            )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self._path)

    async def list_records(self) -> list[SellerRecord]:
        """Кабинеты арендатора."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT alias, token, note FROM tenant_sellers WHERE tenant = ? ORDER BY alias",
                (self.tenant,),
            ).fetchall()
        return [SellerRecord(alias=a, token=self._cipher.decrypt(t), note=n) for a, t, n in rows]

    async def get(self, alias: str) -> SellerRecord | None:
        """Кабинет по алиасу в пределах арендатора."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT alias, token, note FROM tenant_sellers WHERE tenant = ? AND alias = ?",
                (self.tenant, alias),
            ).fetchone()
        if row is None:
            return None
        return SellerRecord(alias=row[0], token=self._cipher.decrypt(row[1]), note=row[2])

    async def add(self, record: SellerRecord) -> None:
        """Добавить или заменить кабинет арендатора."""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tenant_sellers (tenant, alias, token, note, added_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (self.tenant, record.alias, self._cipher.encrypt(record.token), record.note,
                 datetime.now(UTC).isoformat()),
            )

    async def remove(self, alias: str) -> bool:
        """Удалить кабинет арендатора."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM tenant_sellers WHERE tenant = ? AND alias = ?", (self.tenant, alias)
            )
        return cur.rowcount > 0


class TenantRegistry:
    """Реестр кабинетов на арендатора поверх SQLite."""

    def __init__(self, path: Path, cipher: TokenCipher | None = None) -> None:
        """Одна база на всех арендаторов."""
        self._path = Path(path)
        self._cipher = cipher or PlainCipher()
        self._cache: dict[str, SellerRegistry] = {}

    def for_tenant(self, tenant: str) -> SellerRegistry:
        """Реестр арендатора; создаётся при первом обращении."""
        if tenant not in self._cache:
            store = TenantSellerStore(self._path, tenant, self._cipher)
            self._cache[tenant] = SellerRegistry([store])
        return self._cache[tenant]

    async def tenants(self) -> list[str]:
        """Арендаторы, у которых есть хоть один кабинет."""
        with connect_sqlite(self._path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT tenant FROM tenant_sellers ORDER BY tenant"
            ).fetchall()
        return [r[0] for r in rows]


class PostgresTenantStore:
    """Кабинеты арендатора в таблице tenant_sellers Postgres."""

    writable = True

    def __init__(self, backend: Any, tenant: str, cipher: TokenCipher | None = None) -> None:
        """Бэкенд — PostgresBackend из ядра; таблица создаётся им."""
        self._backend = backend
        self.tenant = tenant
        self._cipher = cipher or PlainCipher()

    async def list_records(self) -> list[SellerRecord]:
        """Кабинеты арендатора."""
        async with self._backend.connection() as conn:
            cur = await conn.execute(
                "SELECT alias, token, note FROM tenant_sellers WHERE tenant = %s ORDER BY alias",
                (self.tenant,),
            )
            rows = await cur.fetchall()
        return [SellerRecord(alias=a, token=self._cipher.decrypt(t), note=n) for a, t, n in rows]

    async def get(self, alias: str) -> SellerRecord | None:
        """Кабинет по алиасу."""
        async with self._backend.connection() as conn:
            cur = await conn.execute(
                "SELECT alias, token, note FROM tenant_sellers WHERE tenant = %s AND alias = %s",
                (self.tenant, alias),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return SellerRecord(alias=row[0], token=self._cipher.decrypt(row[1]), note=row[2])

    async def add(self, record: SellerRecord) -> None:
        """Добавить или заменить кабинет арендатора."""
        async with self._backend.connection() as conn:
            await conn.execute(
                "INSERT INTO tenant_sellers (tenant, alias, token, note, added_at) "
                "VALUES (%s, %s, %s, %s, now()) ON CONFLICT (tenant, alias) DO UPDATE SET "
                "token = EXCLUDED.token, note = EXCLUDED.note, added_at = now()",
                (self.tenant, record.alias, self._cipher.encrypt(record.token), record.note),
            )

    async def remove(self, alias: str) -> bool:
        """Удалить кабинет арендатора."""
        async with self._backend.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM tenant_sellers WHERE tenant = %s AND alias = %s",
                (self.tenant, alias),
            )
            return cur.rowcount > 0


class PostgresTenantRegistry:
    """Реестр кабинетов на арендатора поверх Postgres."""

    def __init__(self, backend: Any, cipher: TokenCipher | None = None) -> None:
        """Бэкенд открыт снаружи и живёт дольше реестра."""
        self._backend = backend
        self._cipher = cipher or PlainCipher()
        self._cache: dict[str, SellerRegistry] = {}

    def for_tenant(self, tenant: str) -> SellerRegistry:
        """Реестр арендатора."""
        if tenant not in self._cache:
            store = PostgresTenantStore(self._backend, tenant, self._cipher)
            self._cache[tenant] = SellerRegistry([store])
        return self._cache[tenant]

    async def tenants(self) -> list[str]:
        """Арендаторы с кабинетами."""
        async with self._backend.connection() as conn:
            cur = await conn.execute("SELECT DISTINCT tenant FROM tenant_sellers ORDER BY tenant")
            rows = await cur.fetchall()
        return [r[0] for r in rows]
