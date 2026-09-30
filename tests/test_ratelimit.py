"""Лимитер: ведро, ожидание, штрафы, 429, квота."""

import pytest

from wbmcp.catalog.models import LimitRow, LimitRule
from wbmcp.ratelimit import (
    Acquired,
    BucketState,
    MemoryLimiterStore,
    RateLimiter,
    RateLimitExceeded,
    TokenBucket,
)
from wbmcp.tokens import TokenType


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


MARKET = LimitRule(
    key="group:маркетплейс",
    scope="для всех методов категории Маркетплейс",
    rows=(
        LimitRow("персональный", 60, 300, 0.2, 20),
        LimitRow("сервисный", 60, 300, 0.2, 20),
        LimitRow("базовый", 60, 150, 0.2, 10),
    ),
    error_cost=10,
)
SLOW = LimitRule(key="op:slow", scope="на один аккаунт продавца",
                 rows=(LimitRow(None, 60, 1, 60, 1),))


def test_bucket_math():
    bucket = TokenBucket(capacity=20, interval_s=0.2)
    state = bucket.fresh(0.0)
    assert state.tokens == 20
    state = bucket.take(state, 0.0, 20)
    assert state.tokens == 0
    assert bucket.wait_for(state, 0.0) == pytest.approx(0.2)
    assert bucket.wait_for(state, 0.1) == pytest.approx(0.1)
    refilled = bucket.refill(state, 100.0)
    assert refilled.tokens == 20  # не выше ёмкости
    charged = bucket.charge(state, 0.0, 9)
    assert charged.tokens == -9
    assert bucket.wait_for(charged, 0.0) == pytest.approx(2.0)
    clamped = bucket.clamp(bucket.fresh(0.0), 0.0, 3)
    assert clamped.tokens == 3
    blocked = bucket.block(bucket.fresh(0.0), 0.0, 5)
    assert bucket.wait_for(blocked, 1.0) == pytest.approx(4.0)
    assert bucket.block(blocked, 0.0, 1).blocked_until == 5.0
    with pytest.raises(ValueError):
        TokenBucket(0, 1)
    with pytest.raises(ValueError):
        TokenBucket(1, 0)


async def test_acquire_burst_then_wait():
    clock = FakeClock()
    limiter = RateLimiter(clock=clock, sleep=clock.sleep)
    for _ in range(20):
        got = await limiter.acquire("sid1", MARKET, TokenType.PERSONAL)
        assert got.key == "sid1:group:маркетплейс" and got.waited_s == 0
    got = await limiter.acquire("sid1", MARKET, TokenType.PERSONAL)
    assert clock.sleeps == [pytest.approx(0.2)]
    assert got.waited_s == pytest.approx(0.2)
    # базовый токен — свой запас, но ведро общее по ключу кабинета
    clock.sleeps.clear()
    for _ in range(10):
        await limiter.acquire("sid2", MARKET, TokenType.BASIC)
    await limiter.acquire("sid2", MARKET, TokenType.BASIC)
    assert clock.sleeps == [pytest.approx(0.2)]


async def test_no_rule_or_no_row_means_no_limit():
    limiter = RateLimiter(clock=FakeClock(), sleep=FakeClock().sleep)
    got = await limiter.acquire("s", None, TokenType.PERSONAL)
    assert got.bucket is None and got.rule is None
    only_personal = LimitRule("op:x", "", (LimitRow("персональный", 60, 1, 60, 1),))
    got = await limiter.acquire("s", only_personal, TokenType.SERVICE)
    assert got.bucket is None
    await limiter.observe(got, 200, {})  # ничего не ломается


async def test_max_wait_and_priority():
    clock = FakeClock()
    limiter = RateLimiter(clock=clock, sleep=clock.sleep, max_wait_s=30)
    await limiter.acquire("s", SLOW, TokenType.PERSONAL)
    with pytest.raises(RateLimitExceeded) as info:
        await limiter.acquire("s", SLOW, TokenType.PERSONAL)
    assert info.value.wait_s == pytest.approx(60)
    assert "подождать" in str(info.value)
    limiter.max_wait_s = 120
    await limiter.acquire("s", SLOW, TokenType.PERSONAL, priority=2)
    assert clock.sleeps[-1] == pytest.approx(60 + 0.1)


async def test_observe_penalty_headers_and_429():
    clock = FakeClock()
    store = MemoryLimiterStore()
    limiter = RateLimiter(store, clock=clock, sleep=clock.sleep)
    got = await limiter.acquire("s", MARKET, TokenType.SERVICE)
    await limiter.observe(got, 400, {"X-Ratelimit-Remaining": "500"})
    state = await store.get(got.key)
    assert state.tokens == pytest.approx(19 - 9)
    await limiter.observe(got, 200, {"X-Ratelimit-Remaining": "3"})
    assert (await store.get(got.key)).tokens == pytest.approx(3)
    await limiter.observe(got, 429, {"X-Ratelimit-Retry": "7"})
    assert (await store.get(got.key)).blocked_until == pytest.approx(clock.now + 7)
    with pytest.raises(RateLimitExceeded):
        limiter.max_wait_s = 1
        await limiter.acquire("s", MARKET, TokenType.SERVICE)
    limiter.max_wait_s = 120
    clock.sleeps.clear()
    await limiter.acquire("s", MARKET, TokenType.SERVICE)
    assert clock.sleeps and clock.sleeps[0] >= 6.9
    # 429 без заголовков — блок на один интервал; мусорные заголовки игнорируются
    await limiter.observe(got, 429, {"X-Ratelimit-Retry": "abc"})
    assert (await store.get(got.key)).blocked_until >= clock.now + 0.2
    await limiter.observe(got, 429, {"X-Ratelimit-Reset": "2"})
    assert (await store.get(got.key)).blocked_until >= clock.now + 2
    # 4XX без штрафа (error_cost 1)
    slow = await limiter.acquire("s", SLOW, TokenType.PERSONAL)
    before = (await store.get(slow.key)).tokens
    await limiter.observe(slow, 404, {})
    assert (await store.get(slow.key)).tokens == before


async def test_quota_and_keys():
    clock = FakeClock()
    limiter = RateLimiter(clock=clock, sleep=clock.sleep)
    await limiter.acquire("s", MARKET, TokenType.PERSONAL)
    only_personal = LimitRule("op:p", "", (LimitRow("персональный", 60, 1, 60, 1),))
    report = await limiter.quota("s", {"market": MARKET, "p": only_personal}, TokenType.SERVICE)
    assert report[0]["available"] == 19 and report[0]["burst"] == 20
    assert report[0]["per_period"] == "300/60 с" and report[0]["error_cost"] == 10
    assert report[1]["limit"] is None
    assert await limiter.known_keys("s") == ["s:group:маркетплейс"]
    assert await limiter.known_keys("other") == []


async def test_memory_store_lock_and_state_dataclass():
    store = MemoryLimiterStore()
    async with store.lock("k"):
        await store.set("k", BucketState(1.0, 0.0))
    assert (await store.get("k")).tokens == 1.0
    assert await store.get("missing") is None
    acquired = Acquired(key="x", bucket=None, rule=None)
    assert acquired.waited_s == 0.0


async def test_sandbox_rule_one_per_second():
    from wbmcp.ratelimit.limiter import sandbox_rule

    rule = sandbox_rule("https://marketplace-api-sandbox.wildberries.ru/")
    assert rule.key == "sandbox:marketplace-api-sandbox.wildberries.ru" and rule.error_cost == 1
    clock = FakeClock()
    limiter = RateLimiter(clock=clock, sleep=clock.sleep)
    await limiter.acquire("s", rule, TokenType.TEST)
    got = await limiter.acquire("s", rule, TokenType.TEST)
    assert clock.sleeps == [pytest.approx(1.0)] and got.waited_s == pytest.approx(1.0)
