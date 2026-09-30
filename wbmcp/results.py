"""Большие выгрузки: полный ответ в файл, агенту конверт с образцом и срезы по запросу.

Принцип не меняется: агент получает всё. Меняется только место: если ответ больше
порога, он целиком ложится в файл, а в контекст уходит конверт с путём, размером,
числом элементов и первыми записями. Дальше агент читает файл сам или просит срезы
и проекции через wb_result (JMESPath).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import jmespath
from jmespath.exceptions import JMESPathError

from wbmcp.dbutil import connect_sqlite


class ResultQueryError(ValueError):
    """Выражение JMESPath не разобрано или не применимо."""


class UnknownResultError(KeyError):
    """Результата с таким id нет или файл уже удалён."""


@dataclass(frozen=True)
class StoredResult:
    """Сохранённая выгрузка."""

    id: str
    path: str
    size: int
    sha256: str
    created_at: float
    tool: str
    operation: str
    seller: str
    items: int | None
    top_keys: list[str]
    scopes: list[str] = field(default_factory=lambda: ["read"])

    def as_dict(self) -> dict[str, Any]:
        """Словарь для инструментов."""
        data = asdict(self)
        data["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.created_at))
        return data


Viewer = Callable[[StoredResult], bool]
SCHEMA_VERSION = 2
COLUMNS = "id, path, size, sha256, created_at, tool, operation, seller, items, top_keys, scopes"


def _row(row: tuple[Any, ...]) -> StoredResult:
    return StoredResult(*row[:9], top_keys=json.loads(row[9]), scopes=row[10].split(","))


def encode(value: Any) -> str:
    """Каноничный JSON: по нему считаются размер и хэш."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def preview_of(value: Any, items: int = 5, depth: int = 3, text: int = 200) -> Any:
    """Образец структуры: списки укорачиваются, строки обрезаются, глубина ограничена."""
    if depth <= 0:
        if isinstance(value, list):
            return f"<список из {len(value)}>"
        if isinstance(value, dict):
            return f"<объект с ключами {', '.join(list(value)[:8])}>"
    if isinstance(value, list):
        return [preview_of(v, items, depth - 1, text) for v in value[:items]]
    if isinstance(value, dict):
        return {k: preview_of(v, items, depth - 1, text) for k, v in value.items()}
    if isinstance(value, str) and len(value) > text:
        return value[:text] + f"… (+{len(value) - text} симв.)"
    return value


def apply_query(data: Any, expression: str) -> Any:
    """Применить JMESPath к данным."""
    try:
        return jmespath.search(expression, data)
    except JMESPathError as exc:
        raise ResultQueryError(f"выражение JMESPath не принято: {exc}") from exc


def _items_of(value: Any) -> int | None:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        for key in ("data", "cards", "orders", "items", "report", "feedbacks"):
            inner = value.get(key)
            if isinstance(inner, list):
                return len(inner)
            if isinstance(inner, dict):
                nested = _items_of(inner)
                if nested is not None:
                    return nested
    return None


class ResultStore:
    """Файлы результатов плюс индекс в SQLite."""

    def __init__(
        self,
        directory: Path,
        *,
        inline_max_bytes: int = 200_000,
        preview_items: int = 5,
        keep_hours: float = 168.0,
        clock: Callable[[], float] = time.time,
        scopes_for: Callable[[str], Iterable[str]] | None = None,
    ) -> None:
        """Каталог создаётся сразу; старые файлы чистятся при каждом сохранении.

        ``scopes_for`` — scope операции по её id: нужен, чтобы при переезде индекса старой
        версии (без владельца) выгрузки получили правильные scope; без него они видны только admin.
        """
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.inline_max_bytes = inline_max_bytes
        self.preview_items = preview_items
        self.keep_hours = keep_hours
        self._clock = clock
        self._conn = connect_sqlite(self.directory / "index.db")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS results (id TEXT PRIMARY KEY, path TEXT NOT NULL, "
            "size INTEGER NOT NULL, sha256 TEXT NOT NULL, created_at REAL NOT NULL, "
            "tool TEXT NOT NULL, operation TEXT NOT NULL, seller TEXT NOT NULL, items INTEGER, "
            "top_keys TEXT NOT NULL, scopes TEXT NOT NULL DEFAULT 'admin')"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._migrate(scopes_for)
        self._conn.commit()

    def _version(self) -> int:
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
        return int(row[0]) if row else 0

    def _migrate(self, scopes_for: Callable[[str], Iterable[str]] | None) -> None:
        """Довести индекс до текущей схемы.

        Версия 2: у каждой выгрузки scope её операции. Индекс без колонки получает её; индекс,
        где колонку уже добавила ранняя версия с умолчанием ``read``, пересчитывается целиком,
        иначе финансовые выгрузки остались бы открыты клиентам без ``finance``.
        """
        columns = {r[1] for r in self._conn.execute("PRAGMA table_info(results)")}
        if "scopes" in columns and self._version() >= SCHEMA_VERSION:
            return
        if "scopes" not in columns:
            self._conn.execute(
                "ALTER TABLE results ADD COLUMN scopes TEXT NOT NULL DEFAULT 'admin'"
            )
        rows = self._conn.execute("SELECT id, operation FROM results").fetchall()
        for result_id, operation in rows:
            scopes = sorted(set(scopes_for(operation))) if scopes_for else ["admin"]
            self._conn.execute("UPDATE results SET scopes = ? WHERE id = ?",
                               (",".join(scopes) or "admin", result_id))
        self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema', ?)",
                           (str(SCHEMA_VERSION),))

    def save(self, data: Any, *, tool: str, operation: str, seller: str,
             scopes: Iterable[str] = ("read",)) -> StoredResult:
        """Сохранить выгрузку; одинаковые данные одного кабинета дают тот же id и файл.

        Кабинет входит в id: одинаковый ответ у двух кабинетов — две записи, каждая со своим
        владельцем. ``scopes`` — что нужно клиенту, чтобы читать результат (scope операции).
        """
        text = encode(data)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        result_id = hashlib.sha256(f"{seller}\n{text}".encode()).hexdigest()[:16]
        path = self.directory / f"{result_id}.json"
        if not path.exists():
            path.write_text(text, encoding="utf-8")
        top_keys = list(data)[:20] if isinstance(data, dict) else []
        stored = StoredResult(
            id=result_id, path=str(path), size=len(text.encode("utf-8")), sha256=digest,
            created_at=self._clock(), tool=tool, operation=operation, seller=seller,
            items=_items_of(data), top_keys=[str(k) for k in top_keys],
            scopes=sorted(set(scopes) or {"read"}),
        )
        self._conn.execute(
            f"INSERT OR REPLACE INTO results ({COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (stored.id, stored.path, stored.size, stored.sha256, stored.created_at, stored.tool,
             stored.operation, stored.seller, stored.items, json.dumps(stored.top_keys),
             ",".join(stored.scopes)),
        )
        self._conn.commit()
        self.purge()
        return stored

    def compact(self, payload: dict[str, Any], key: str, *, tool: str, operation: str,
                seller: str, scopes: Iterable[str] = ("read",)) -> bool:
        """Если ``payload[key]`` больше порога, вынести в файл и оставить образец."""
        value = payload.get(key)
        if value is None:
            return False
        size = len(encode(value).encode("utf-8"))
        if size <= self.inline_max_bytes:
            return False
        stored = self.save(value, tool=tool, operation=operation, seller=seller, scopes=scopes)
        payload[key] = preview_of(value, self.preview_items)
        payload["truncated"] = True
        payload["stored"] = {
            **stored.as_dict(),
            "note": (
                f"поле {key} больше {self.inline_max_bytes} байт: полный ответ сохранён "
                f"в файл целиком, здесь только образец; срезы и проекции — wb_result "
                f"(result_id, select JMESPath, offset, limit) или ресурс wb://results/{stored.id}"
            ),
        }
        return True

    def get(self, result_id: str, viewer: Viewer | None = None) -> StoredResult:
        """Описание сохранённого результата.

        ``viewer`` решает, виден ли результат тому, кто спрашивает (кабинет и scope);
        чужой результат неотличим от несуществующего.
        """
        row = self._conn.execute(
            f"SELECT {COLUMNS} FROM results WHERE id = ?", (result_id,)
        ).fetchone()
        if row is None or not Path(row[1]).exists():
            raise UnknownResultError(f"результат {result_id!r} не найден или уже удалён")
        stored = _row(row)
        if viewer is not None and not viewer(stored):
            raise UnknownResultError(f"результат {result_id!r} не найден или недоступен клиенту")
        return stored

    def load(self, result_id: str, viewer: Viewer | None = None) -> tuple[StoredResult, Any]:
        """Описание и полные данные."""
        stored = self.get(result_id, viewer)
        return stored, json.loads(Path(stored.path).read_text(encoding="utf-8"))

    def query(self, result_id: str, expression: str | None = None, offset: int = 0,
              limit: int = 100, *, viewer: Viewer | None = None) -> dict[str, Any]:
        """Срез сохранённого результата: JMESPath, затем offset и limit для списков."""
        stored, data = self.load(result_id, viewer)
        value = apply_query(data, expression) if expression else data
        answer: dict[str, Any] = {"result_id": stored.id, "select": expression,
                                  "source": {"tool": stored.tool, "operation": stored.operation,
                                             "size": stored.size, "items": stored.items}}
        if isinstance(value, list):
            answer.update({"total": len(value), "offset": offset, "limit": limit,
                           "items": value[offset:offset + limit]})
        else:
            answer["value"] = value
        return answer

    def list(self, limit: int = 20, viewer: Viewer | None = None) -> list[StoredResult]:
        """Последние сохранённые результаты, видимые спрашивающему."""
        rows = self._conn.execute(
            f"SELECT {COLUMNS} FROM results ORDER BY created_at DESC"
        ).fetchall()
        found = [stored for stored in map(_row, rows) if viewer is None or viewer(stored)]
        return found[:limit]

    def purge(self) -> int:
        """Удалить результаты старше keep_hours; вернуть число удалённых."""
        threshold = self._clock() - self.keep_hours * 3600
        rows = self._conn.execute(
            "SELECT id, path FROM results WHERE created_at < ?", (threshold,)
        ).fetchall()
        for result_id, path in rows:
            Path(path).unlink(missing_ok=True)
            self._conn.execute("DELETE FROM results WHERE id = ?", (result_id,))
        self._conn.commit()
        return len(rows)
