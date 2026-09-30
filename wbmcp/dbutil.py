"""Общее для SQLite-хранилищ."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect_sqlite(path: Path | str) -> sqlite3.Connection:
    """Соединение с WAL и synchronous=NORMAL: запись не ждёт fsync на каждом коммите."""
    target = str(path)
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn
