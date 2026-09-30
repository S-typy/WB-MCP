"""Модели каталога: операция, параметры, правило лимита."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from wbmcp.tokens import Category, TokenType

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Названия строк таблицы лимитов в спецификациях. Тестовый токен встречается редко,
# для него берём строку базового — цифры у них совпадают.
LIMIT_ROW_BY_TYPE: dict[TokenType, tuple[str, ...]] = {
    TokenType.PERSONAL: ("персональный",),
    TokenType.SERVICE: ("сервисный",),
    TokenType.BASIC: ("базовый",),
    TokenType.TEST: ("тестовый", "базовый"),
}


@dataclass(frozen=True)
class LimitRow:
    """Одна строка таблицы лимитов."""

    token_type: str | None
    period_s: float
    limit: int
    interval_s: float
    burst: int

    def as_dict(self) -> dict[str, Any]:
        """Плоское представление для инструментов."""
        return {
            "token_type": self.token_type,
            "period_s": self.period_s,
            "limit": self.limit,
            "interval_s": self.interval_s,
            "burst": self.burst,
        }


@dataclass(frozen=True)
class LimitRule:
    """Правило лимита операции: ключ общего bucket-а и строки по типам токена."""

    key: str
    scope: str
    rows: tuple[LimitRow, ...]
    error_cost: int = 1

    def for_token(self, token_type: TokenType) -> LimitRow | None:
        """Строка для типа токена; таблица без типов подходит всем."""
        untyped = [r for r in self.rows if r.token_type is None]
        if untyped:
            return untyped[0]
        wanted = LIMIT_ROW_BY_TYPE[token_type]
        for name in wanted:
            for row in self.rows:
                if row.token_type == name:
                    return row
        return None

    def as_dict(self) -> dict[str, Any]:
        """Представление для инструментов."""
        return {
            "key": self.key,
            "scope": self.scope,
            "error_cost": self.error_cost,
            "rows": [r.as_dict() for r in self.rows],
        }


@dataclass(frozen=True)
class Parameter:
    """Параметр операции: путь, запрос или заголовок."""

    name: str
    location: str
    required: bool
    schema: dict[str, Any]
    description: str = ""


@dataclass(frozen=True)
class Operation:
    """Одна операция API со всем, что нужно для вызова и допуска."""

    id: str
    method: str
    path: str
    section: str
    section_title: str
    summary: str
    description: str
    tags: tuple[str, ...]
    category: str | None
    token_category: Category | None
    read_only: bool | None
    token_types: tuple[str, ...]
    host: str
    sandbox_host: str | None
    deprecated: bool
    parameters: tuple[Parameter, ...]
    request_body: dict[str, Any] | None
    responses: dict[str, dict[str, Any]]
    limit: LimitRule | None
    flags: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_write(self) -> bool:
        """Метод записи по HTTP (POST/PUT/PATCH/DELETE); часть таких методов только читает."""
        return self.method in WRITE_METHODS

    @property
    def mutating(self) -> bool:
        """Меняет ли операция данные кабинета: метод записи, недоступный токену «только чтение».

        У WB много чтений через POST (список карточек, отчёты); их нельзя гонять через
        журнал записей и подтверждения.
        """
        return self.is_write and self.read_only is not True

    @property
    def path_params(self) -> tuple[Parameter, ...]:
        """Параметры пути."""
        return tuple(p for p in self.parameters if p.location == "path")

    @property
    def query_params(self) -> tuple[Parameter, ...]:
        """Параметры строки запроса."""
        return tuple(p for p in self.parameters if p.location == "query")

    @property
    def header_params(self) -> tuple[Parameter, ...]:
        """Заголовки, кроме авторизации: её ставит клиент сам."""
        return tuple(
            p for p in self.parameters
            if p.location == "header" and p.name.lower() not in ("authorization", "content-type")
        )

    @property
    def cacheable(self) -> bool:
        """Можно ли кэшировать ответ: метод только читает и не отдаёт файлы или статусы."""
        return (
            self.read_only is True
            and not (self.flags & {"binary", "async-report"})
            and self.path != "/ping"
        )

    @property
    def tool_name(self) -> str:
        """Имя сгенерированного инструмента MCP."""
        return f"wb_op_{self.id}"

    def brief(self) -> dict[str, Any]:
        """Короткая карточка для поиска."""
        return {
            "id": self.id,
            "method": self.method,
            "path": self.path,
            "section": self.section,
            "summary": self.summary,
            "category": self.category,
            "read_only": self.read_only,
            "token_types": list(self.token_types) or "any",
            "sandbox": self.sandbox_host is not None,
            "deprecated": self.deprecated,
            "flags": sorted(self.flags),
        }
