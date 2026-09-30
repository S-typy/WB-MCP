"""Общий путь выполнения операции для сгенерированных инструментов и wb_request."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from wbmcp.app import AppContext
from wbmcp.auth import AccessDeniedError, required_scopes, scopes_for_result
from wbmcp.cache import make_cache_key
from wbmcp.catalog.models import Operation
from wbmcp.catalog.validate import ArgumentValidationError, validate_call
from wbmcp.client import (
    AmbiguousWriteError,
    DuplicateWriteError,
    PreflightError,
    WbError,
    WbHttpError,
)
from wbmcp.client.files import check_upload_paths
from wbmcp.client.preflight import check_access
from wbmcp.ratelimit import RateLimitExceeded
from wbmcp.results import ResultQueryError, apply_query
from wbmcp.sellers import UnknownSellerError
from wbmcp.tools.confirm import Confirmer, is_dangerous

CONTROL_ARGS = ("seller", "body", "dry_run", "force", "fresh", "select")


def split_arguments(op: Operation, arguments: dict[str, Any]) -> dict[str, Any]:
    """Разложить аргументы инструмента по местам: путь, запрос, заголовки, тело."""
    path_names = {p.name for p in op.path_params}
    query_names = {p.name for p in op.query_params}
    header_names = {p.name.lower(): p.name for p in op.header_params}
    parts: dict[str, Any] = {"path_params": {}, "query": {}, "headers": {}, "unknown": []}
    for name, value in arguments.items():
        if name in CONTROL_ARGS:
            continue
        base = name[:-6] if name.endswith("_param") else name
        if base in path_names:
            parts["path_params"][base] = value
        elif base in query_names:
            parts["query"][base] = value
        elif base.lower() in header_names:
            parts["headers"][header_names[base.lower()]] = value
        else:
            parts["unknown"].append(name)
    return parts


def extract_path_params(template: str, path: str) -> dict[str, str]:
    """Значения ``{param}`` из реального пути по шаблону операции."""
    names = re.findall(r"\{([^/}]+)\}", template)
    pattern = "^" + re.sub(r"\{[^/}]+\}", r"([^/]+)", re.escape(template)
                           .replace(r"\{", "{").replace(r"\}", "}")) + "$"
    match = re.match(pattern, path)
    if not match:
        return {}
    return dict(zip(names, match.groups(), strict=True))


async def run_operation(
    app: AppContext,
    op: Operation,
    arguments: dict[str, Any],
    *,
    tool_name: str,
    ctx: Any,
    confirmer: Confirmer,
    path_params: dict[str, Any] | None = None,
    priority: int = 0,
    compact: bool = True,
    query: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Проверить права, при необходимости подтвердить, вызвать и записать в аудит.

    ``query`` и ``headers`` — параметры WB, переданные отдельно (так делает wb_request).
    Они идут прямо в запрос и никогда не трогают управляющие аргументы вроде dry_run.
    """
    principal = app.principal()
    seller_alias = arguments.get("seller")
    audit_seller = seller_alias or ""
    sid = ""
    shown = dict(arguments)
    if query:
        shown["query"] = dict(query)
    if headers:
        shown["headers"] = dict(headers)
    try:
        principal.check_scopes(required_scopes(op))
        seller = await app.seller_for(seller_alias, principal)
        audit_seller, sid = seller.alias, seller.sid
        parts = split_arguments(op, arguments)
        if path_params:
            parts["path_params"].update(path_params)
        if query:
            parts["query"].update(query)
        if headers:
            parts["headers"].update(headers)
        dry_run = bool(arguments.get("dry_run"))
        force = bool(arguments.get("force"))
        fresh = bool(arguments.get("fresh"))
        # Допуск токена проверяется до кэша: истёкший или урезанный токен того же
        # продавца не должен читать чужие кэшированные ответы. Клиент проверит ещё раз.
        access = check_access(op, seller, server_read_only=app.config.server.read_only)
        if not access.ok:
            raise PreflightError(access.problems)
        ttl_s = app.cache.ttl(op) if not dry_run else 0.0
        cache_key = None
        if ttl_s > 0:
            # Песочница и бой живут в разных ключах даже при одном sid.
            cache_owner = seller.sid + (":sandbox" if seller.info.sandbox else "")
            cache_key = make_cache_key(cache_owner, op.id, {
                "path": parts["path_params"], "query": parts["query"],
                "headers": parts["headers"], "body": arguments.get("body"),
            })
            if not fresh:
                entry = await app.cache.lookup(cache_key)
                if entry is not None:
                    now = app.cache.clock()
                    payload = dict(entry.payload)
                    payload["cache"] = {"hit": True, "age_s": round(now - entry.stored_at, 1),
                                        "expires_in_s": round(entry.expires_at - now, 1)}
                    await app.audit.record(
                        client=principal.name, seller=seller.alias, sid=sid, tool=tool_name,
                        operation=op.id, arguments=shown, outcome="cache",
                    )
                    _count(app, seller.alias, op, "cache")
                    app.metrics.inc("cache_total", {"result": "hit"})
                    return finish_payload(app, payload, arguments, tool_name=tool_name,
                                          operation=op.id, seller=seller.alias,
                                          compact=compact)
                app.metrics.inc("cache_total", {"result": "miss"})
        notes = []
        if parts["unknown"]:
            notes.append(f"неизвестные аргументы проигнорированы: {parts['unknown']}")
        if not force:
            problems = validate_call(
                op, path_params=parts["path_params"], query=parts["query"],
                headers=parts["headers"], body=arguments.get("body"),
            )
            if problems:
                raise ArgumentValidationError(
                    "аргументы не прошли проверку по спецификации: "
                    + "; ".join(problems[:12])
                    + "; схема — в wb_api_describe, отправить как есть можно с force=true"
                )
        if op.mutating and not dry_run and not force and is_dangerous(op):
            question = (f"Выполнить {op.method} {op.path} для кабинета {seller.alias}? "
                        f"Действие необратимо. Аргументы: "
                        f"{json.dumps(shown, ensure_ascii=False, default=str)[:800]}")
            if not await confirmer.confirm(ctx, question):
                await app.audit.record(client=principal.name, seller=seller.alias, sid=sid,
                                 tool=tool_name, operation=op.id, arguments=shown,
                                 outcome="cancelled")
                raise ToolError(
                    "действие не подтверждено; если клиент не умеет спрашивать, повторите "
                    "с force=true после проверки аргументов через dry_run"
                )
        if "multipart" in op.flags and isinstance(arguments.get("body"), dict):
            check_upload_paths(arguments["body"], remote=app.remote,
                               uploads=app.data_dir / "uploads")
        result = await app.client.call(
            op, seller,
            path_params=parts["path_params"], query=parts["query"], headers=parts["headers"],
            body=arguments.get("body"), dry_run=dry_run, force=force, priority=priority,
        )
        outcome = "dry_run" if dry_run else "ok"
        await app.audit.record(
            client=principal.name, seller=seller.alias, sid=sid, tool=tool_name,
            operation=op.id, arguments=shown, outcome=outcome,
            status=result.status, waited_s=result.waited_s, elapsed_s=result.elapsed_s,
        )
        _count(app, seller.alias, op, outcome, status=result.status if not dry_run else None,
               waited_s=result.waited_s, elapsed_s=result.elapsed_s)
        payload = result.as_dict(app.inline_limit)
        if notes:
            payload["notes"] = notes + payload.get("notes", [])
        payload["operation"] = op.id
        if cache_key and result.status < 300 and result.file is None:
            await app.cache.remember(op, seller.sid, cache_key, dict(payload), ttl_s)
            payload["cache"] = {"hit": False, "ttl_s": ttl_s}
        if op.mutating and not dry_run and app.cache.invalidate_on_write:
            dropped = await app.cache.invalidate(seller.sid, op.section)
            if dropped:
                payload["cache"] = {"invalidated": dropped, "section": op.section}
        if dry_run:
            return payload
        return finish_payload(app, payload, arguments, tool_name=tool_name, operation=op.id,
                              seller=seller.alias, compact=compact)
    except (AccessDeniedError, UnknownSellerError, PreflightError,
            ArgumentValidationError, ResultQueryError) as exc:
        await app.audit.record(client=principal.name, seller=audit_seller, sid=sid,
                               tool=tool_name,
                         operation=op.id, arguments=shown, outcome="denied", error=str(exc))
        _count(app, audit_seller, op, "denied")
        raise ToolError(str(exc)) from exc
    except WbHttpError as exc:
        await app.audit.record(client=principal.name, seller=audit_seller, sid=sid,
                               tool=tool_name,
                         operation=op.id, arguments=shown, outcome="error",
                         status=exc.status, error=str(exc))
        _count(app, audit_seller, op, "error", status=exc.status)
        body = json.dumps(exc.data, ensure_ascii=False) if exc.data is not None else exc.text
        raise ToolError(f"{exc}\nтело ответа: {body}") from exc
    except (AmbiguousWriteError, DuplicateWriteError, RateLimitExceeded, WbError,
            ValueError) as exc:
        await app.audit.record(client=principal.name, seller=audit_seller, sid=sid,
                               tool=tool_name,
                         operation=op.id, arguments=shown, outcome="error", error=str(exc))
        _count(app, audit_seller, op, "error")
        raise ToolError(str(exc)) from exc


def finish_payload(app: AppContext, payload: dict[str, Any], arguments: dict[str, Any], *,
                   tool_name: str, operation: str, seller: str,
                   key: str = "data", compact: bool = True) -> dict[str, Any]:
    """Проекция select по JMESPath и вынос большого ответа в файл.

    Агрегаторы страниц зовут с compact=False: они выносят в файл уже собранный список.
    """
    expression = arguments.get("select")
    if expression and key in payload:
        payload[key] = apply_query(payload[key], str(expression))
        payload["select"] = expression
    if compact and app.results.compact(payload, key, tool=tool_name, operation=operation,
                                       seller=seller, scopes=result_scopes(app, operation)):
        app.metrics.inc("results_total", {"kind": "stored"})
    return payload


def result_scopes(app: AppContext, operation: str) -> set[str]:
    """Какие scope нужны, чтобы читать сохранённую выгрузку: те же, что у операции."""
    return scopes_for_result(app.catalog, operation)


def _count(app: AppContext, seller: str, op: Operation, outcome: str, *,
           status: int | None = None, waited_s: float = 0.0, elapsed_s: float = 0.0) -> None:
    """Обновить метрики вызова."""
    labels = {"seller": seller or "-", "section": op.section}
    app.metrics.inc("calls_total", {**labels, "outcome": outcome})
    if status is not None:
        app.metrics.inc("wb_status_total", {"seller": seller or "-", "status": status})
    if outcome in ("ok", "error"):
        app.metrics.observe("wait_seconds", waited_s, labels)
        app.metrics.observe("elapsed_seconds", elapsed_s, labels)
