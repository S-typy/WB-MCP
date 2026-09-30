"""Настройки сервера: файл TOML плюс переменные окружения.

Файл не обязателен. Без него сервер поднимается с одним кабинетом из переменной
WB_TOKEN, этого хватает для быстрого старта через uvx.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from wbmcp import __version__

ENV_CONFIG = "WBMCP_CONFIG"
ENV_TOKEN = "WB_TOKEN"
ENV_SPECS = "WBMCP_SPECS"
ENV_DATABASE = "WBMCP_DATABASE_URL"
ENV_REDIS = "WBMCP_REDIS_URL"
PACKAGED_SPECS = Path(__file__).resolve().parent / "specs"


class ConfigError(ValueError):
    """Ошибка в настройках, которую надо показать человеку."""


class SellerConfig(BaseModel):
    """Кабинет из файла настроек: токен берётся из значения, переменной или файла."""

    alias: str
    token: str | None = None
    token_env: str | None = None
    token_file: str | None = None
    note: str = ""

    def resolve_token(self, env: Mapping[str, str] | None = None) -> str:
        """Достать сам токен; приоритет: token, token_env, token_file."""
        env = os.environ if env is None else env
        if self.token:
            return self.token.strip()
        if self.token_env:
            value = env.get(self.token_env, "").strip()
            if not value:
                raise ConfigError(f"кабинет {self.alias!r}: переменная {self.token_env} пуста")
            return value
        if self.token_file:
            path = Path(self.token_file).expanduser()
            if not path.is_file():
                raise ConfigError(f"кабинет {self.alias!r}: файла {path} нет")
            lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
            for line in lines:
                if line and not line.startswith("#"):
                    return line  # первая непустая строка, комментарии пропускаются
            raise ConfigError(f"кабинет {self.alias!r}: в файле {path} нет токена")
        raise ConfigError(
            f"кабинет {self.alias!r}: не указан ни token, ни token_env, ни token_file"
        )


class ClientConfig(BaseModel):
    """MCP-клиент для HTTP-режима: ключ, разрешённые кабинеты и scope."""

    name: str
    key: str | None = None
    key_env: str | None = None
    sellers: list[str] = Field(default_factory=lambda: ["*"])
    scopes: list[str] = Field(default_factory=lambda: ["read"])

    def resolve_key(self, env: Mapping[str, str] | None = None) -> str:
        """Ключ клиента из значения или переменной окружения."""
        env = os.environ if env is None else env
        if self.key:
            return self.key.strip()
        if self.key_env:
            value = env.get(self.key_env, "").strip()
            if value:
                return value
        raise ConfigError(f"клиент {self.name!r}: не задан ключ (key или key_env)")


class CacheConfig(BaseModel):
    """Кэш ответов WB: где хранить и на сколько."""

    enabled: bool = True
    backend: Literal["memory", "sqlite", "redis"] = "memory"
    default_ttl_s: float = 0.0
    max_entries: int = 5000
    ttl: dict[str, float] = Field(default_factory=dict)
    invalidate_on_write: bool = True


class ResultsConfig(BaseModel):
    """Большие выгрузки: порог, образец, срок хранения файлов."""

    inline_max_bytes: int = 200_000
    preview_items: int = 5
    keep_hours: float = 168.0


class ServerConfig(BaseModel):
    """Общие параметры сервера."""

    name: str = "wb-mcp"
    mode: Literal["local", "cloud"] = "local"
    user_agent: str = f"wb-mcp/{__version__}"
    data_dir: Path = Path("~/.wb-mcp")
    specs_dir: Path | None = None
    profiles: list[str] = Field(default_factory=lambda: ["core"])
    read_only: bool = False
    http_host: str = "127.0.0.1"
    http_port: int = 8000
    token_key_env: str = "WBMCP_TOKEN_KEY"
    request_timeout: float = 30.0
    max_file_bytes: int = 20 * 1024 * 1024
    database_url: str | None = None
    database_schema: str = "wbmcp"
    database_pool_size: int = 8
    redis_url: str | None = None
    cache: CacheConfig = Field(default_factory=CacheConfig)
    results: ResultsConfig = Field(default_factory=ResultsConfig)

    def resolved_data_dir(self) -> Path:
        """Каталог данных с раскрытой тильдой."""
        return self.data_dir.expanduser()


class Config(BaseModel):
    """Всё содержимое файла настроек."""

    server: ServerConfig = Field(default_factory=ServerConfig)
    sellers: list[SellerConfig] = Field(default_factory=list)
    clients: list[ClientConfig] = Field(default_factory=list)


def load_config(path: str | Path | None = None, env: Mapping[str, str] | None = None) -> Config:
    """Прочитать настройки из файла (или WBMCP_CONFIG) и дополнить из окружения."""
    env = os.environ if env is None else env
    source = path or env.get(ENV_CONFIG)
    if source:
        file = Path(source).expanduser()
        if not file.is_file():
            raise ConfigError(f"файл настроек не найден: {file}")
        try:
            data = tomllib.loads(file.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"не разобран {file}: {exc}") from exc
    else:
        data = {}
    config = Config.model_validate(data)
    if env.get(ENV_DATABASE):
        config.server.database_url = env[ENV_DATABASE]
    if env.get(ENV_REDIS):
        config.server.redis_url = env[ENV_REDIS]
    if not config.sellers and env.get(ENV_TOKEN):
        config.sellers.append(SellerConfig(alias="default", token_env=ENV_TOKEN))
    aliases = [s.alias for s in config.sellers]
    if len(aliases) != len(set(aliases)):
        raise ConfigError("алиасы кабинетов должны быть уникальны")
    return config


def find_specs_dir(explicit: Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """Где лежат снимки спецификаций: явный путь, WBMCP_SPECS или свежий снимок из пакета."""
    env = os.environ if env is None else env
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    if env.get(ENV_SPECS):
        candidates.append(Path(env[ENV_SPECS]).expanduser())
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
        raise ConfigError(f"каталог спецификаций не найден: {candidate}")
    if PACKAGED_SPECS.is_dir():
        dated = sorted(p for p in PACKAGED_SPECS.iterdir() if p.is_dir())
        if dated:
            return dated[-1]
    raise ConfigError("снимки спецификаций не найдены, укажите specs_dir или WBMCP_SPECS")
