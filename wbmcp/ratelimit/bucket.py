"""Математика token bucket без побочных эффектов.

У WB четыре числа: период, лимит, интервал и всплеск. Ёмкость ведра равна
всплеску, пополнение — один запрос за интервал; лимит за период получается сам
(60 с / 200 мс = 300). После ответа 4XX в Маркетплейсе ведро штрафуется
дополнительными единицами, поэтому запас может уйти в минус.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

# Недобор меньше этого считаем нулём: иначе округление float даёт ожидание в 1e-17 секунды,
# а с подменными часами такой sleep не двигает время и лимитер крутится на месте.
EPSILON = 1e-9


@dataclass(frozen=True)
class BucketState:
    """Снимок ведра: сколько единиц есть и когда считали."""

    tokens: float
    updated_at: float
    blocked_until: float = 0.0


class TokenBucket:
    """Правила одного ведра: ёмкость и интервал пополнения."""

    def __init__(self, capacity: int, interval_s: float) -> None:
        """Ёмкость не меньше единицы, интервал строго положительный."""
        if capacity < 1:
            raise ValueError("ёмкость ведра должна быть не меньше 1")
        if interval_s <= 0:
            raise ValueError("интервал пополнения должен быть больше нуля")
        self.capacity = capacity
        self.interval_s = interval_s

    def fresh(self, now: float) -> BucketState:
        """Полное ведро."""
        return BucketState(tokens=float(self.capacity), updated_at=now)

    def refill(self, state: BucketState, now: float) -> BucketState:
        """Досыпать единицы за прошедшее время, не выше ёмкости."""
        elapsed = max(0.0, now - state.updated_at)
        tokens = min(float(self.capacity), state.tokens + elapsed / self.interval_s)
        return replace(state, tokens=tokens, updated_at=now)

    def wait_for(self, state: BucketState, now: float, cost: float = 1.0) -> float:
        """Сколько ждать, чтобы хватило на ``cost``; ноль — можно сразу."""
        state = self.refill(state, now)
        wait = 0.0
        if state.blocked_until > now:
            wait = state.blocked_until - now
        missing = cost - state.tokens
        if missing > EPSILON:   # крошечный недобор от округления float — не повод ждать
            wait = max(wait, missing * self.interval_s)
        return wait

    def take(self, state: BucketState, now: float, cost: float = 1.0) -> BucketState:
        """Списать ``cost`` единиц; вызывающий уже убедился, что можно."""
        state = self.refill(state, now)
        return replace(state, tokens=state.tokens - cost)

    def charge(self, state: BucketState, now: float, extra: float) -> BucketState:
        """Штраф постфактум (4XX = 10 запросов): запас может стать отрицательным."""
        state = self.refill(state, now)
        return replace(state, tokens=state.tokens - extra)

    def clamp(self, state: BucketState, now: float, remaining: float) -> BucketState:
        """Подрезать запас по X-Ratelimit-Remaining, если WB считает, что осталось меньше."""
        state = self.refill(state, now)
        return replace(state, tokens=min(state.tokens, float(remaining)))

    def block(self, state: BucketState, now: float, seconds: float) -> BucketState:
        """Заморозить ведро после 429 на ``seconds``."""
        state = self.refill(state, now)
        return replace(state, blocked_until=max(state.blocked_until, now + seconds))
