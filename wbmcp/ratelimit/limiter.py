"""Лимитер: очередь к вёдрам, штрафы, 429 и отчёт о квоте.

Ключ ведра — ``<sid>:<ключ правила>``, так что все агенты и все экземпляры
сервера с общим store делят один бюджет кабинета. Фоновые вызовы (опрос
отчётов) уступают интерактивным: перед повторной попыткой они ждут чуть дольше.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from wbmcp.catalog.models import LimitRow, LimitRule
from wbmcp.ratelimit.bucket import BucketState, TokenBucket
from wbmcp.ratelimit.store import LimiterStore, MemoryLimiterStore
from wbmcp.tokens import TokenType

HEADER_REMAINING = "x-ratelimit-remaining"
SANDBOX_INTERVAL_S = 1.0
HEADER_RETRY = "x-ratelimit-retry"
HEADER_RESET = "x-ratelimit-reset"
PRIORITY_STEP_S = 0.05
MIN_SLEEP_S = 0.001     # короче спать нет смысла, а нулевой сон зациклил бы ожидание


def sandbox_rule(host: str) -> LimitRule:
    """Лимит песочницы: один запрос в секунду на хост (проверено 29.09.2026), без штрафа за 4XX."""
    name = host.split("://", 1)[-1].rstrip("/")
    return LimitRule(
        key=f"sandbox:{name}",
        scope=f"песочница, хост {name}",
        rows=(LimitRow(None, SANDBOX_INTERVAL_S, 1, SANDBOX_INTERVAL_S, 1),),
        error_cost=1,
    )


class RateLimitExceeded(RuntimeError):
    """Ждать пришлось бы дольше, чем разрешено."""

    def __init__(self, key: str, wait_s: float) -> None:
        """Сообщение с ключом ведра и временем ожидания."""
        super().__init__(f"лимит {key}: нужно подождать {wait_s:.1f} с")
        self.key = key
        self.wait_s = wait_s


@dataclass(frozen=True)
class Acquired:
    """Разрешение на запрос: ключ ведра и правило, чтобы потом учесть ответ."""

    key: str
    bucket: TokenBucket | None
    rule: LimitRule | None
    waited_s: float = 0.0


class RateLimiter:
    """Выдаёт разрешения на запросы и учитывает ответы WB."""

    def __init__(
        self,
        store: LimiterStore | None = None,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_wait_s: float = 120.0,
    ) -> None:
        """Часы и sleep подменяются в тестах.

        Без явных часов: monotonic для памяти процесса, настенные часы для общего
        хранилища (Redis), потому что monotonic разных машин сравнивать нельзя.
        Экземплярам с общим хранилищем нужны синхронизированные часы (NTP).
        """
        self._store = store or MemoryLimiterStore()
        if clock is None:
            clock = time.time if getattr(self._store, "shared", False) else time.monotonic
        self._clock = clock
        self._sleep = sleep
        self.max_wait_s = max_wait_s

    @staticmethod
    def bucket_for(row: LimitRow) -> TokenBucket:
        """Ведро по строке таблицы лимитов."""
        return TokenBucket(capacity=max(1, row.burst), interval_s=row.interval_s)

    @staticmethod
    def key_for(sid: str, rule: LimitRule) -> str:
        """Ключ ведра кабинета для правила."""
        return f"{sid}:{rule.key}"

    async def _load(self, key: str, bucket: TokenBucket, now: float) -> BucketState:
        state = await self._store.get(key)
        return bucket.refill(state, now) if state else bucket.fresh(now)

    async def acquire(
        self,
        sid: str,
        rule: LimitRule | None,
        token_type: TokenType,
        *,
        priority: int = 0,
        cost: float = 1.0,
    ) -> Acquired:
        """Дождаться права на запрос и списать единицы."""
        row = rule.for_token(token_type) if rule else None
        if rule is None or row is None:
            return Acquired(key=f"{sid}:none", bucket=None, rule=rule)
        bucket = self.bucket_for(row)
        key = self.key_for(sid, rule)
        waited = 0.0
        while True:
            async with self._store.lock(key):
                now = self._clock()
                state = await self._load(key, bucket, now)
                wait = bucket.wait_for(state, now, cost)
                if wait <= 0:
                    await self._store.set(key, bucket.take(state, now, cost))
                    return Acquired(key=key, bucket=bucket, rule=rule, waited_s=waited)
            wait = max(wait, MIN_SLEEP_S) + priority * PRIORITY_STEP_S
            if waited + wait > self.max_wait_s:
                raise RateLimitExceeded(key, wait)
            await self._sleep(wait)
            waited += wait

    async def observe(self, acquired: Acquired, status: int, headers: Mapping[str, str]) -> None:
        """Учесть ответ WB: штраф за 4XX, X-Ratelimit-Remaining, блокировка после 429."""
        if acquired.bucket is None or acquired.rule is None:
            return
        bucket = acquired.bucket
        lowered = {k.lower(): v for k, v in headers.items()}
        async with self._store.lock(acquired.key):
            now = self._clock()
            state = await self._load(acquired.key, bucket, now)
            if status == 429:
                seconds = _seconds(lowered.get(HEADER_RETRY))
                if seconds is None:
                    seconds = _seconds(lowered.get(HEADER_RESET))
                if seconds is None:
                    seconds = bucket.interval_s
                state = bucket.block(state, now, seconds)
            elif 400 <= status < 500 and acquired.rule.error_cost > 1:
                state = bucket.charge(state, now, acquired.rule.error_cost - 1)
            remaining = _seconds(lowered.get(HEADER_REMAINING))
            if remaining is not None and status != 429:
                state = bucket.clamp(state, now, remaining)
            await self._store.set(acquired.key, state)

    async def quota(
        self, sid: str, rules: Mapping[str, LimitRule], token_type: TokenType
    ) -> list[dict[str, Any]]:
        """Остаток по вёдрам кабинета для правил, о которых нас спросили."""
        now = self._clock()
        report: list[dict[str, Any]] = []
        for label, rule in rules.items():
            row = rule.for_token(token_type)
            if row is None:
                report.append(
                    {"scope": label, "limit": None,
                     "note": "для этого типа токена лимит не описан"}
                )
                continue
            bucket = self.bucket_for(row)
            key = self.key_for(sid, rule)
            state = await self._load(key, bucket, now)
            report.append({
                "scope": label,
                "key": rule.key,
                "available": round(max(0.0, state.tokens), 2),
                "burst": bucket.capacity,
                "interval_s": bucket.interval_s,
                "per_period": f"{row.limit}/{row.period_s:g} с",
                "blocked_for_s": round(max(0.0, state.blocked_until - now), 2),
                "error_cost": rule.error_cost,
            })
        return report

    async def known_keys(self, sid: str) -> list[str]:
        """Ключи вёдер, которые кабинет уже трогал."""
        return await self._store.keys(f"{sid}:")


def _seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
