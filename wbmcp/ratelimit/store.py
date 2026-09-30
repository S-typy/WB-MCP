"""Хранилища состояния вёдер.

В локальном режиме достаточно памяти процесса. Для нескольких экземпляров
сервера нужен общий store (Redis) с тем же интерфейсом — он живёт в облачной
части.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

from wbmcp.ratelimit.bucket import BucketState


class LimiterStore(Protocol):
    """Состояние ведра по ключу плюс блокировка на время пересчёта.

    ``shared`` — состояние видно другим процессам и машинам; тогда лимитер считает время
    по настенным часам, а не по monotonic, у которого на каждой машине своя шкала.
    """

    shared: bool

    async def get(self, key: str) -> BucketState | None:
        """Состояние ведра или None, если ведра ещё не было."""
        ...

    async def set(self, key: str, state: BucketState) -> None:
        """Сохранить состояние."""
        ...

    def lock(self, key: str) -> AsyncIterator[None]:
        """Контекст, внутри которого ведро меняет только один вызывающий."""
        ...

    async def keys(self, prefix: str) -> list[str]:
        """Ключи с данным префиксом (для отчёта о квоте)."""
        ...


class MemoryLimiterStore:
    """Состояние вёдер в памяти процесса."""

    shared = False

    def __init__(self) -> None:
        """Пустое хранилище."""
        self._states: dict[str, BucketState] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, key: str) -> BucketState | None:
        """Состояние ведра."""
        return self._states.get(key)

    async def set(self, key: str, state: BucketState) -> None:
        """Сохранить состояние."""
        self._states[key] = state

    @asynccontextmanager
    async def lock(self, key: str) -> AsyncIterator[None]:
        """Блокировка на ключ; создаётся лениво."""
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            yield

    async def keys(self, prefix: str) -> list[str]:
        """Ключи по префиксу."""
        return sorted(k for k in self._states if k.startswith(prefix))
