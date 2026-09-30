"""Аудит вызовов: кто, каким кабинетом, какой инструмент, чем кончилось.

Содержимого запросов и ответов здесь нет — только хэш аргументов, статус и
короткий текст ошибки. Токены в тексты попасть не должны, но на всякий случай
всё, что похоже на JWT, вырезается.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from wbmcp.dbutil import connect_sqlite

_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}")


def redact_secrets(text: str) -> str:
    """Заменить всё, что похоже на JWT, на пометку."""
    return _JWT.sub("<token>", text or "")


def args_hash(arguments: dict[str, Any]) -> str:
    """Короткий хэш аргументов вызова."""
    canonical = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class AuditRecord:
    """Одна строка аудита."""

    ts: str
    client: str
    seller: str
    sid: str
    tool: str
    operation: str
    args_hash: str
    outcome: str
    status: int | None
    waited_s: float
    elapsed_s: float
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Словарь для инструментов."""
        return asdict(self)


def build_record(
    *,
    client: str,
    seller: str,
    sid: str,
    tool: str,
    operation: str,
    arguments: dict[str, Any],
    outcome: str,
    status: int | None = None,
    waited_s: float = 0.0,
    elapsed_s: float = 0.0,
    error: str = "",
) -> AuditRecord:
    """Собрать запись аудита с хэшем аргументов и вырезанными секретами."""
    return AuditRecord(
        ts=datetime.now(UTC).isoformat(timespec="seconds"),
        client=client,
        seller=seller,
        sid=sid,
        tool=tool,
        operation=operation,
        args_hash=args_hash(arguments),
        outcome=outcome,
        status=status,
        waited_s=round(waited_s, 3),
        elapsed_s=round(elapsed_s, 3),
        error=redact_secrets(error)[:500],
    )


class AuditStore(Protocol):
    """Интерфейс аудита."""

    async def record(self, **fields: Any) -> AuditRecord:
        """Записать вызов."""
        ...

    async def recent(self, limit: int = 50, seller: str | None = None) -> list[AuditRecord]:
        """Последние записи."""
        ...

    async def stats(self) -> dict[str, Any]:
        """Сводка."""
        ...


class AuditLog:
    """Аудит в SQLite; ``:memory:`` для тестов."""

    def __init__(self, path: Path | str) -> None:
        """Открыть журнал аудита."""
        self._conn = connect_sqlite(path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, "
            "client TEXT, seller TEXT, sid TEXT, tool TEXT, operation TEXT, args_hash TEXT, "
            "outcome TEXT, status INTEGER, waited_s REAL, elapsed_s REAL, error TEXT)"
        )
        self._conn.commit()

    async def record(self, **fields: Any) -> AuditRecord:
        """Записать вызов."""
        record = build_record(**fields)
        self._conn.execute(
            "INSERT INTO audit (ts, client, seller, sid, tool, operation, args_hash, outcome, "
            "status, waited_s, elapsed_s, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record.ts, record.client, record.seller, record.sid, record.tool, record.operation,
             record.args_hash, record.outcome, record.status, record.waited_s, record.elapsed_s,
             record.error),
        )
        self._conn.commit()
        return record

    async def recent(self, limit: int = 50, seller: str | None = None) -> list[AuditRecord]:
        """Последние записи, новые сверху."""
        query = ("SELECT ts, client, seller, sid, tool, operation, args_hash, outcome, status, "
                 "waited_s, elapsed_s, error FROM audit")
        params: tuple[Any, ...] = ()
        if seller:
            query += " WHERE seller = ?"
            params = (seller,)
        query += " ORDER BY id DESC LIMIT ?"
        rows = self._conn.execute(query, params + (limit,)).fetchall()
        return [AuditRecord(*row) for row in rows]

    async def stats(self) -> dict[str, Any]:
        """Сводка: сколько вызовов по исходам и кабинетам, сколько ждали."""
        by_outcome = dict(self._conn.execute(
            "SELECT outcome, COUNT(*) FROM audit GROUP BY outcome"
        ).fetchall())
        by_seller = dict(self._conn.execute(
            "SELECT seller, COUNT(*) FROM audit GROUP BY seller"
        ).fetchall())
        waited, count = self._conn.execute(
            "SELECT COALESCE(SUM(waited_s), 0), COUNT(*) FROM audit"
        ).fetchone()
        return {
            "calls": count,
            "by_outcome": by_outcome,
            "by_seller": by_seller,
            "waited_total_s": round(waited, 3),
        }
