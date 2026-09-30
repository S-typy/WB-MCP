"""Асинхронные отчёты WB: создать задание, дождаться, скачать."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from wbmcp.client.errors import WbError


class ReportTimeoutError(WbError):
    """Отчёт не приготовился за отведённое время."""


class ReportFailedError(WbError):
    """WB сообщил, что отчёт не получился."""


async def wait_until(
    poll: Callable[[], Awaitable[Any]],
    *,
    done: Callable[[Any], bool],
    failed: Callable[[Any], str | None] = lambda _: None,
    interval_s: float = 5.0,
    timeout_s: float = 600.0,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    clock: Callable[[], float] | None = None,
) -> Any:
    """Опрашивать ``poll`` до готовности; вернуть последний ответ."""
    sleep = sleep or asyncio.sleep
    clock = clock or time.monotonic
    started = clock()
    while True:
        response = await poll()
        if done(response):
            return response
        reason = failed(response)
        if reason:
            raise ReportFailedError(f"отчёт не сформирован: {reason}")
        if clock() - started >= timeout_s:
            raise ReportTimeoutError(f"отчёт не готов за {timeout_s:.0f} с, попробуйте позже")
        await sleep(interval_s)
