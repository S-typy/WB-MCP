"""Вызов операции WB: сборка запроса, лимиты, повторы, разбор ответа."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from wbmcp.catalog.models import Operation
from wbmcp.client.errors import (
    AmbiguousWriteError,
    DuplicateWriteError,
    PreflightError,
    WbHttpError,
)
from wbmcp.client.files import FileStore, SavedFile
from wbmcp.client.journal import JournalStore, make_key
from wbmcp.client.preflight import check_access
from wbmcp.ratelimit import RateLimiter
from wbmcp.ratelimit.limiter import sandbox_rule
from wbmcp.sellers.models import Seller

RATELIMIT_HEADERS = ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
                     "x-ratelimit-retry")


@dataclass(frozen=True)
class RetryPolicy:
    """Сколько и как повторять."""

    max_attempts: int = 4
    backoff_base_s: float = 1.0
    backoff_cap_s: float = 30.0
    retry_statuses: tuple[int, ...] = (429, 500, 502, 503, 504)

    def delay(self, attempt: int) -> float:
        """Пауза перед повтором номер ``attempt`` (с 1)."""
        return min(self.backoff_cap_s, self.backoff_base_s * (2 ** (attempt - 1)))


@dataclass
class CallResult:
    """Результат вызова операции."""

    status: int
    url: str
    method: str
    data: Any = None
    text: str = ""
    file: SavedFile | None = None
    ratelimit: dict[str, str] = field(default_factory=dict)
    waited_s: float = 0.0
    elapsed_s: float = 0.0
    attempts: int = 1
    dry_run: bool = False
    notes: list[str] = field(default_factory=list)
    journal_key: str | None = None
    request: dict[str, Any] = field(default_factory=dict)

    def as_dict(self, inline_limit: int = 0) -> dict[str, Any]:
        """Полный ответ для агента: ничего не режем и не маскируем."""
        result: dict[str, Any] = {
            "status": self.status,
            "method": self.method,
            "url": self.url,
            "attempts": self.attempts,
            "waited_s": round(self.waited_s, 3),
            "elapsed_s": round(self.elapsed_s, 3),
        }
        if self.dry_run:
            result["dry_run"] = True
            result["request"] = self.request
        if self.data is not None:
            result["data"] = self.data
        elif self.text:
            result["text"] = self.text
        if self.file is not None:
            result["file"] = self.file.as_dict(inline_limit)
        if self.ratelimit:
            result["ratelimit"] = self.ratelimit
        if self.notes:
            result["notes"] = self.notes
        if self.journal_key:
            result["journal_key"] = self.journal_key
        return result


class WbClient:
    """Асинхронный клиент к API продавца."""

    def __init__(
        self,
        *,
        limiter: RateLimiter | None = None,
        user_agent: str = "wb-mcp",
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        journal: JournalStore | None = None,
        files: FileStore | None = None,
        retry: RetryPolicy | None = None,
        server_read_only: bool = False,
        hosts: Collection[str] | None = None,
        sandbox_hosts: Collection[str] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Всё, что ходит наружу или спит, подменяется в тестах.

        ``hosts`` и ``sandbox_hosts`` — хосты из каталога: явный ``host`` в ``call``
        обязан быть одним из них, иначе токен мог бы уйти на чужой адрес.
        """
        self.limiter = limiter or RateLimiter()
        self.user_agent = user_agent
        self.journal = journal
        self.files = files
        self.retry = retry or RetryPolicy()
        self.server_read_only = server_read_only
        self.hosts = frozenset(hosts) if hosts is not None else None
        self.sandbox_hosts = frozenset(sandbox_hosts) if sandbox_hosts is not None else None
        self._sleep = sleep
        self._clock = clock
        self._http = httpx.AsyncClient(
            transport=transport, timeout=timeout_s, follow_redirects=False
        )

    async def aclose(self) -> None:
        """Закрыть соединения."""
        await self._http.aclose()

    @staticmethod
    def build_url(host: str, path_template: str, path_params: Mapping[str, Any]) -> str:
        """Подставить параметры пути с экранированием."""
        path = path_template
        for name, value in path_params.items():
            path = path.replace("{" + name + "}", quote(str(value), safe=""))
        if "{" in path:
            missing = path[path.index("{") + 1:path.index("}")]
            raise ValueError(f"не задан параметр пути {missing!r}")
        return host + path

    def _prepare_body(self, op: Operation, body: Any) -> dict[str, Any]:
        if body is None:
            return {}
        content_type = (op.request_body or {}).get("content_type", "application/json")
        if content_type.startswith("multipart/"):
            if not isinstance(body, dict):
                raise ValueError("для multipart тело должно быть объектом с полями")
            files: dict[str, tuple[str, bytes]] = {}
            data: dict[str, Any] = {}
            for name, value in body.items():
                if isinstance(value, dict) and ("path" in value or "base64" in value):
                    files[name] = FileStore.read_part(value)
                else:
                    data[name] = value
            return {"files": files, "data": data}
        return {"json": body}

    async def call(
        self,
        op: Operation,
        seller: Seller,
        *,
        path_params: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        body: Any = None,
        dry_run: bool = False,
        force: bool = False,
        priority: int = 0,
        host: str | None = None,
    ) -> CallResult:
        """Вызвать операцию от имени кабинета."""
        allowed = self.sandbox_hosts if seller.info.sandbox else self.hosts
        report = check_access(op, seller, server_read_only=self.server_read_only, host=host,
                              allowed_hosts=allowed)
        if not report.ok:
            raise PreflightError(report.problems)
        url = self.build_url(report.host, op.path, path_params or {})
        request_headers = {
            "Authorization": seller.token,
            "User-Agent": self.user_agent,
            "Accept": "application/json, */*",
            **{k: str(v) for k, v in (headers or {}).items()},
        }
        clean_query = {k: v for k, v in (query or {}).items() if v is not None}
        body_kwargs = self._prepare_body(op, body)
        journal_key = None
        if op.mutating and self.journal is not None:
            journal_key = make_key(op.id, seller.sid, {
                "path": dict(path_params or {}), "query": clean_query, "body": body,
            })
        if dry_run:
            return CallResult(
                status=0, url=url, method=op.method, dry_run=True, notes=report.notes,
                journal_key=journal_key,
                request={
                    "headers": {k: v for k, v in request_headers.items() if k != "Authorization"},
                    "query": clean_query,
                    "body": body,
                    "multipart": "files" in body_kwargs,
                },
            )
        if journal_key and not force:
            blocking = await self.journal.reserve(journal_key, op.id, seller.sid)
            if blocking is not None:
                raise DuplicateWriteError(journal_key, blocking.state,
                                          blocking.updated_at.isoformat(timespec="seconds"))
        elif journal_key:
            await self.journal.mark(journal_key, op.id, seller.sid, "sending")
        started = self._clock()
        result = await self._send(op, seller, url, request_headers, clean_query, body_kwargs,
                                  priority, journal_key)
        result.elapsed_s = self._clock() - started
        result.notes = report.notes + result.notes
        result.journal_key = journal_key
        return result

    async def _send(
        self,
        op: Operation,
        seller: Seller,
        url: str,
        headers: dict[str, str],
        query: dict[str, Any],
        body_kwargs: dict[str, Any],
        priority: int,
        journal_key: str | None,
    ) -> CallResult:
        waited = 0.0
        attempt = 0
        rule = sandbox_rule(url.split("/", 3)[2]) if seller.info.sandbox else op.limit
        while True:
            attempt += 1
            acquired = await self.limiter.acquire(
                seller.sid, rule, seller.info.token_type, priority=priority
            )
            waited += acquired.waited_s
            try:
                response = await self._http.request(
                    op.method, url, params=query, headers=headers, **body_kwargs
                )
            except httpx.HTTPError as exc:
                reason = f"{type(exc).__name__}: {exc}"
                if op.mutating:
                    await self._journal_mark(journal_key, op, seller, "ambiguous", note=reason)
                    raise AmbiguousWriteError(journal_key or "-", reason) from exc
                if attempt >= self.retry.max_attempts:
                    raise WbHttpError(0, url, None, reason) from exc
                pause = self.retry.delay(attempt)
                await self._sleep(pause)
                waited += pause
                continue
            await self.limiter.observe(acquired, response.status_code, response.headers)
            if response.status_code in self.retry.retry_statuses:
                if response.status_code >= 500 and op.mutating:
                    text = response.text[:500]
                    await self._journal_mark(journal_key, op, seller, "ambiguous",
                                             status=response.status_code, note=text)
                    raise AmbiguousWriteError(journal_key or "-", f"HTTP {response.status_code}")
                if attempt < self.retry.max_attempts:
                    pause = self.retry.delay(attempt) if response.status_code != 429 else 0.0
                    if pause:
                        await self._sleep(pause)
                        waited += pause
                    continue
            return await self._finish(op, seller, url, response, waited, attempt, journal_key)

    async def _journal_mark(self, key: str | None, op: Operation, seller: Seller, state: str,
                            *, status: int | None = None, note: str = "") -> None:
        if key and self.journal is not None:
            await self.journal.mark(key, op.id, seller.sid, state, status=status, note=note)

    async def _finish(
        self,
        op: Operation,
        seller: Seller,
        url: str,
        response: httpx.Response,
        waited: float,
        attempts: int,
        journal_key: str | None,
    ) -> CallResult:
        ratelimit = {k: v for k, v in response.headers.items() if k.lower() in RATELIMIT_HEADERS}
        content_type = response.headers.get("content-type", "")
        data: Any = None
        text = ""
        file: SavedFile | None = None
        if "json" in content_type:
            try:
                data = response.json()
            except ValueError:
                text = response.text
        elif content_type.startswith("text/") or not response.content:
            text = response.text
        elif response.status_code < 400 and self.files is not None:
            name_hint = url.rsplit("/", 1)[-1] or op.id
            file = self.files.save(response.content, content_type.split(";")[0], name_hint)
        else:
            text = response.text
        if response.status_code >= 400:
            await self._journal_mark(journal_key, op, seller, "failed",
                                     status=response.status_code)
            raise WbHttpError(response.status_code, url, data, text)
        await self._journal_mark(journal_key, op, seller, "sent", status=response.status_code)
        return CallResult(
            status=response.status_code, url=url, method=op.method, data=data, text=text,
            file=file, ratelimit=ratelimit, waited_s=waited, attempts=attempts,
        )
