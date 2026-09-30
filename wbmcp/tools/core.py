"""Базовые инструменты: кабинеты, ping, справка о продавце, новости, квота, журналы."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from wbmcp.app import AppContext
from wbmcp.auth import AccessDeniedError, ClientPrincipal, scopes_for_result
from wbmcp.catalog.models import LimitRule
from wbmcp.client import WbError
from wbmcp.results import StoredResult
from wbmcp.sellers import ReadOnlyStoreError, UnknownSellerError
from wbmcp.tools.confirm import Confirmer
from wbmcp.tools.runner import run_operation

READ_ONLY = ToolAnnotations(read_only_hint=True)
NEWS_DEFAULT_DAYS = 30


def default_news_from() -> str:
    """Дата начала выборки новостей по умолчанию: 30 дней назад."""
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(days=NEWS_DEFAULT_DAYS)).strftime("%Y-%m-%d")


def result_viewer(app: AppContext, principal: ClientPrincipal) -> Callable[[StoredResult], bool]:
    """Видна ли клиенту сохранённая выгрузка.

    Нужны разрешённый кабинет, scope из индекса и scope операции по каталогу сейчас: второе
    условие не даёт устаревшему индексу ослабить ограничения.
    """
    def visible(stored: StoredResult) -> bool:
        current = scopes_for_result(app.catalog, stored.operation)
        return (principal.allows_seller(stored.seller) and principal.has_scopes(stored.scopes)
                and principal.has_scopes(current))

    return visible


def register(server: Any, app: AppContext, confirmer: Confirmer) -> None:
    """Зарегистрировать базовые инструменты."""

    def _principal():
        return app.principal()

    async def wb_sellers() -> dict[str, Any]:
        """Список кабинетов: алиасы, тип токена, категории, срок. Токены не показываются."""
        principal = _principal()
        sellers = [s.summary() for s in await app.sellers.list()
                   if principal.allows_seller(s.alias)]
        return {"client": principal.name, "sellers": sellers}

    async def wb_seller_add(alias: str, token: str, note: str = "") -> dict[str, Any]:
        """Добавить кабинет в реестр сервера по токену API (нужен scope admin)."""
        principal = _principal()
        try:
            principal.check_scopes(["admin"])
            seller = await app.sellers.add(alias, token, note)
        except (AccessDeniedError, ReadOnlyStoreError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return seller.summary()

    async def wb_seller_remove(alias: str) -> dict[str, Any]:
        """Убрать кабинет из реестра сервера (нужен scope admin)."""
        principal = _principal()
        try:
            principal.check_scopes(["admin"])
        except AccessDeniedError as exc:
            raise ToolError(str(exc)) from exc
        return {"alias": alias, "removed": await app.sellers.remove(alias)}

    async def wb_ping(seller: str | None = None, host: str | None = None,
                      ctx: Context | None = None) -> dict[str, Any]:
        """Проверить доступность хостов WB API токеном кабинета.

        Без host опрашиваются все хосты категорий, которые есть в токене.
        """
        principal = _principal()
        try:
            chosen = await app.seller_for(seller, principal)
        except (UnknownSellerError, AccessDeniedError) as exc:
            raise ToolError(str(exc)) from exc
        ping_op = app.catalog.by_route("GET", "/ping")
        if host:
            target = host.rstrip("/")
            if target not in app.catalog.hosts(chosen.info.sandbox):
                raise ToolError(f"хост {target!r} не из каталога WB: токен туда не отправится; "
                                f"без host опрашиваются все хосты кабинета")
            hosts = [target]
        else:
            hosts = sorted({
                (op.sandbox_host if chosen.info.sandbox and op.sandbox_host else op.host)
                for op in app.catalog if chosen.info.allows(op.token_category)
                and not (chosen.info.sandbox and op.sandbox_host is None)
            })
        results = []
        for target in hosts:
            try:
                result = await app.client.call(ping_op, chosen, host=target)
                results.append({"host": target, "status": result.status, "data": result.data,
                                "text": result.text})
            except WbError as exc:
                results.append({"host": target, "error": str(exc)})
        await app.audit.record(client=principal.name, seller=chosen.alias, sid=chosen.sid,
                         tool="wb_ping", operation="ping", arguments={"host": host},
                         outcome="ok")
        return {"seller": chosen.alias, "sandbox": chosen.info.sandbox, "hosts": results}

    async def wb_seller_info(seller: str | None = None,
                             ctx: Context | None = None) -> dict[str, Any]:
        """Информация о продавце из WB (название, sid) плюс разбор токена."""
        op = app.catalog.by_route("GET", "/api/v1/seller-info")
        result = await run_operation(app, op, {"seller": seller}, tool_name="wb_seller_info",
                                     ctx=ctx, confirmer=confirmer)
        principal = _principal()
        result["token"] = (await app.seller_for(seller, principal)).summary()
        return result

    async def wb_news(seller: str | None = None, from_date: str | None = None,
                      from_id: int | None = None, ctx: Context | None = None) -> dict[str, Any]:
        """Новости WB API: изменения методов, новые поля, отключения.

        WB требует хотя бы один параметр; без from_date и from_id берутся последние 30 дней.
        """
        op = app.catalog.by_route("GET", "/api/communications/v2/news")
        arguments: dict[str, Any] = {"seller": seller}
        if from_id is not None:
            arguments["fromID"] = from_id
        else:
            arguments["from"] = from_date or default_news_from()
        return await run_operation(app, op, arguments, tool_name="wb_news", ctx=ctx,
                                   confirmer=confirmer)

    async def wb_quota(seller: str | None = None, operation: str | None = None,
                       all_classes: bool = False) -> dict[str, Any]:
        """Остаток лимитов кабинета: по операции, по затронутым классам или по всем."""
        principal = _principal()
        try:
            chosen = await app.seller_for(seller, principal)
        except (UnknownSellerError, AccessDeniedError) as exc:
            raise ToolError(str(exc)) from exc
        rules: dict[str, LimitRule] = {}
        if chosen.info.sandbox:
            from wbmcp.ratelimit.limiter import sandbox_rule

            touched = set(await app.limiter.known_keys(chosen.sid))
            for host in sorted({op.sandbox_host for op in app.catalog if op.sandbox_host}):
                rule = sandbox_rule(host)
                if all_classes or app.limiter.key_for(chosen.sid, rule) in touched:
                    rules[rule.scope] = rule
            report = await app.limiter.quota(chosen.sid, rules, chosen.info.token_type)
            note = None if rules else "кабинет ещё ничего не вызывал; all_classes=true покажет всё"
            return {"seller": chosen.alias, "token_type": chosen.info.token_type.value,
                    "sandbox": True, "quota": report, "note": note}
        if operation:
            op = app.catalog.get(operation)
            if op.limit:
                rules[f"{op.method} {op.path}"] = op.limit
        else:
            touched = set(await app.limiter.known_keys(chosen.sid))
            for op in app.catalog:
                if not op.limit:
                    continue
                key = app.limiter.key_for(chosen.sid, op.limit)
                if all_classes or key in touched:
                    rules.setdefault(f"{op.limit.scope} ({op.method} {op.path})", op.limit)
        report = await app.limiter.quota(chosen.sid, rules, chosen.info.token_type)
        note = None if rules else "кабинет ещё ничего не вызывал; all_classes=true покажет всё"
        return {"seller": chosen.alias, "token_type": chosen.info.token_type.value,
                "quota": report, "note": note}

    async def wb_journal(seller: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Журнал записей кабинета: что отправлялось, чем кончилось, что зависло."""
        principal = _principal()
        try:
            chosen = await app.seller_for(seller, principal)
        except (UnknownSellerError, AccessDeniedError) as exc:
            raise ToolError(str(exc)) from exc
        entries = await app.journal.recent(chosen.sid, limit)
        return {"seller": chosen.alias, "entries": [
            {"key": e.key, "operation": e.operation, "state": e.state, "status": e.status,
             "note": e.note, "updated_at": e.updated_at.isoformat(timespec="seconds")}
            for e in entries
        ]}

    async def wb_result(result_id: str | None = None, select: str | None = None,
                        offset: int = 0, limit: int = 100) -> dict[str, Any]:
        """Сохранённые большие выгрузки: без result_id — список, с ним — срез данных.

        select — выражение JMESPath (например [?brand=='XBOX'].{nmID: nmID, title: title});
        для списков действуют offset и limit. Полный файл лежит по пути из списка.
        """
        from wbmcp.results import ResultQueryError, UnknownResultError

        viewer = result_viewer(app, _principal())
        if result_id is None:
            return {"results": [r.as_dict() for r in app.results.list(viewer=viewer)]}
        try:
            return app.results.query(result_id, select, offset, limit, viewer=viewer)
        except (UnknownResultError, ResultQueryError) as exc:
            raise ToolError(str(exc)) from exc

    async def wb_cache(action: str = "stats", seller: str | None = None,
                       section: str | None = None) -> dict[str, Any]:
        """Кэш ответов: stats — сводка и правила TTL, clear — сбросить (кабинет и/или раздел).

        Сброс всего кэша без seller доступен только со scope admin; остальным без seller
        сбрасываются лишь кабинеты, разрешённые клиенту.
        """
        principal = _principal()
        if action == "clear":
            if seller:
                try:
                    sid = (await app.seller_for(seller, principal)).sid
                except (UnknownSellerError, AccessDeniedError) as exc:
                    raise ToolError(str(exc)) from exc
                return {"cleared": await app.cache.invalidate(sid, section)}
            if principal.has_scopes({"admin"}):
                return {"cleared": await app.cache.invalidate(None, section)}
            cleared = 0
            for own in await app.sellers.list():
                if principal.allows_seller(own.alias):
                    cleared += await app.cache.invalidate(own.sid, section)
            return {"cleared": cleared,
                    "note": "без scope admin сброшены только кабинеты, разрешённые клиенту"}
        if action != "stats":
            raise ToolError("action должен быть stats или clear")
        return await app.cache.stats()

    async def wb_audit(limit: int = 50, seller: str | None = None) -> dict[str, Any]:
        """Аудит вызовов сервера и сводка (нужен scope admin)."""
        principal = _principal()
        try:
            principal.check_scopes(["admin"])
        except AccessDeniedError as exc:
            raise ToolError(str(exc)) from exc
        return {"stats": await app.audit.stats(),
                "recent": [r.as_dict() for r in await app.audit.recent(limit, seller)]}

    server.add_tool(wb_sellers, annotations=READ_ONLY)
    server.add_tool(wb_seller_add, annotations=ToolAnnotations(read_only_hint=False))
    server.add_tool(wb_seller_remove,
                    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True))
    server.add_tool(wb_ping,
                    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    server.add_tool(wb_seller_info,
                    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    server.add_tool(wb_news,
                    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    server.add_tool(wb_quota, annotations=READ_ONLY)
    server.add_tool(wb_journal, annotations=READ_ONLY)
    server.add_tool(wb_cache, annotations=ToolAnnotations(read_only_hint=False))
    server.add_tool(wb_result, annotations=READ_ONLY)
    server.add_tool(wb_audit, annotations=READ_ONLY)
