"""Общение с покупателями: отзывы, вопросы, чат, заявки на возврат."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from wbmcp.client.paging import collect_pages
from wbmcp.tools.curated.kit import READ, WRITE, Kit, data_of

FEEDBACK_PAGE = 1000          # take; сумма skip и take у WB не больше 10 000
CLAIMS_PAGE = 200
QUESTION_STATES = ("wbRu", "none")


def register(server: Any, kit: Kit) -> None:
    """Инструменты отзывов, вопросов, чата и возвратов."""

    async def paged_list(tool: str, path: str, items_key: str, *, seller: str | None,
                         query: dict[str, Any], take: int, skip: int, all_pages: bool,
                         max_pages: int, ctx: Any) -> tuple[dict[str, Any], list[Any], int, bool]:
        """Список отзывов или вопросов: одна страница или все подряд по skip/take."""
        first: dict[str, Any] = {}

        async def fetch(offset: int) -> dict[str, Any]:
            nonlocal first
            result = await kit.call("GET", path, {**query, "seller": seller, "take": take,
                                                  "skip": offset},
                                    tool=tool, ctx=ctx, compact=False)
            first = first or result
            return result

        items, pages, capped = await kit.collect_offset(
            fetch, items_of=lambda r: data_of(r).get("data", {}).get(items_key) or [],
            limit=take, max_pages=max_pages if all_pages else 1, start=skip,
        )
        return first, items, pages, capped and all_pages

    async def wb_feedbacks(
        seller: str | None = None,
        is_answered: bool = False,
        take: int = 100,
        skip: int = 0,
        nm_id: int | None = None,
        order: str | None = None,
        date_from: int | None = None,
        date_to: int | None = None,
        all_pages: bool = False,
        max_pages: int = 10,
        fresh: bool = False,
        select: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Отзывы покупателей: по умолчанию неотвеченные, одна страница.

        all_pages=true собирает страницы подряд по skip/take (у WB сумма skip и take не больше
        10 000). Даты — unix time в секундах.
        """
        owner = await kit.owner_of(seller)
        query = {"isAnswered": is_answered, "nmId": nm_id, "order": order,
                 "dateFrom": date_from, "dateTo": date_to, "fresh": fresh}
        first, items, pages, capped = await paged_list(
            "wb_feedbacks", "/api/v1/feedbacks", "feedbacks", seller=seller, query=query,
            take=take, skip=skip, all_pages=all_pages, max_pages=max_pages, ctx=ctx)
        counters = {k: v for k, v in (data_of(first).get("data") or {}).items()
                    if k != "feedbacks"}
        payload = {"feedbacks": items, "count": len(items), "pages": pages, "capped": capped,
                   **counters}
        return kit.finish(payload, select, tool="wb_feedbacks",
                          operation=kit.op("GET", "/api/v1/feedbacks").id, seller=owner,
                          key="feedbacks")

    async def wb_feedback_reply(
        feedback_id: str,
        text: str,
        seller: str | None = None,
        edit: bool = False,
        dry_run: bool = False,
        force: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Ответить на отзыв покупателя (edit=true — изменить уже данный ответ).

        Текст от 2 до 5000 символов. Ответ уходит покупателю сразу, dry_run покажет запрос.
        """
        if not feedback_id or not text or len(text.strip()) < 2:
            raise ToolError("нужны feedback_id и text не короче 2 символов")
        method = "PATCH" if edit else "POST"
        return await kit.call(method, "/api/v1/feedbacks/answer",
                              {"seller": seller, "body": {"id": feedback_id, "text": text},
                               "dry_run": dry_run, "force": force},
                              tool="wb_feedback_reply", ctx=ctx)

    async def wb_questions(
        seller: str | None = None,
        is_answered: bool = False,
        take: int = 100,
        skip: int = 0,
        nm_id: int | None = None,
        order: str | None = None,
        date_from: int | None = None,
        date_to: int | None = None,
        all_pages: bool = False,
        max_pages: int = 10,
        fresh: bool = False,
        select: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Вопросы покупателей: по умолчанию неотвеченные; all_pages=true собирает все."""
        owner = await kit.owner_of(seller)
        query = {"isAnswered": is_answered, "nmId": nm_id, "order": order,
                 "dateFrom": date_from, "dateTo": date_to, "fresh": fresh}
        first, items, pages, capped = await paged_list(
            "wb_questions", "/api/v1/questions", "questions", seller=seller, query=query,
            take=take, skip=skip, all_pages=all_pages, max_pages=max_pages, ctx=ctx)
        counters = {k: v for k, v in (data_of(first).get("data") or {}).items()
                    if k != "questions"}
        payload = {"questions": items, "count": len(items), "pages": pages, "capped": capped,
                   **counters}
        return kit.finish(payload, select, tool="wb_questions",
                          operation=kit.op("GET", "/api/v1/questions").id, seller=owner,
                          key="questions")

    async def wb_question_answer(
        question_id: str,
        text: str | None = None,
        seller: str | None = None,
        state: str = "wbRu",
        dry_run: bool = False,
        force: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Ответить на вопрос покупателя или отклонить его.

        state=wbRu публикует ответ (нужен text), state=none отклоняет вопрос, он пропадает
        с сайта покупателей.
        """
        if state not in QUESTION_STATES:
            raise ToolError(f"state должен быть одним из: {', '.join(QUESTION_STATES)}")
        if state == "wbRu" and not (text and text.strip()):
            raise ToolError("для публикации ответа нужен text")
        # WB требует объект answer и при отклонении, тогда текст пустой
        body: dict[str, Any] = {"id": question_id, "state": state,
                                "answer": {"text": text or ""}}
        return await kit.call("PATCH", "/api/v1/questions",
                              {"seller": seller, "body": body, "dry_run": dry_run,
                               "force": force},
                              tool="wb_question_answer", ctx=ctx)

    async def wb_chats(seller: str | None = None, fresh: bool = False,
                       select: str | None = None,
                       ctx: Context | None = None) -> dict[str, Any]:
        """Чаты с покупателями: id чата, покупатель, товар и подпись replySign для ответа."""
        return await kit.call("GET", "/api/v1/seller/chats",
                              {"seller": seller, "fresh": fresh, "select": select},
                              tool="wb_chats", ctx=ctx)

    async def wb_chat_events(seller: str | None = None, next_ts: int | None = None,
                             max_pages: int = 10, select: str | None = None,
                             ctx: Context | None = None) -> dict[str, Any]:
        """События чатов (новые сообщения) с автоматической пагинацией по next.

        next_ts — с какого момента читать, unix time в миллисекундах из прошлого ответа.
        """
        owner = await kit.owner_of(seller)
        last: dict[str, Any] = {}

        async def fetch(cursor: Any) -> dict[str, Any]:
            nonlocal last
            arguments: dict[str, Any] = {"seller": seller}
            if cursor:
                arguments["next"] = cursor
            last = await kit.call("GET", "/api/v1/seller/events", arguments,
                                  tool="wb_chat_events", ctx=ctx, compact=False)
            return last

        def next_of(response: dict[str, Any]) -> Any:
            result = data_of(response).get("result") or {}
            return result.get("next") if result.get("events") else None

        events, pages, capped = await collect_pages(
            fetch, items_of=lambda r: (data_of(r).get("result") or {}).get("events") or [],
            next_of=next_of, max_pages=max_pages, start=next_ts,
        )
        result = data_of(last).get("result") or {}
        payload = {"events": events, "count": len(events), "pages": pages, "capped": capped,
                   "next": result.get("next"), "totalEvents": result.get("totalEvents")}
        return kit.finish(payload, select, tool="wb_chat_events",
                          operation=kit.op("GET", "/api/v1/seller/events").id, seller=owner,
                          key="events")

    async def reply_sign_of(seller: str | None, chat_id: str, ctx: Any, fresh: bool) -> str:
        """Подпись чата из списка чатов; fresh=true перечитывает список мимо кэша."""
        chats = await kit.call("GET", "/api/v1/seller/chats", {"seller": seller, "fresh": fresh},
                               tool="wb_chat_send", ctx=ctx, compact=False)
        for chat in data_of(chats).get("result") or []:
            if str(chat.get("chatID")) == str(chat_id):
                sign = chat.get("replySign")
                if sign:
                    return str(sign)
                break
        raise ToolError(f"чат {chat_id!r} не найден или у него нет replySign")

    async def wb_chat_send(
        text: str | None = None,
        seller: str | None = None,
        chat_id: str | None = None,
        reply_sign: str | None = None,
        files: list[dict[str, Any]] | None = None,
        dry_run: bool = False,
        force: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Написать покупателю в чат: по chat_id (подпись найдётся сама) или по reply_sign.

        Текст до 1000 символов; files — до нескольких файлов JPEG, PDF или PNG в виде
        {"path": ...} или {"base64": ..., "filename": ...}. Если подпись протухла, инструмент
        перечитает чаты и повторит отправку один раз.
        """
        if not (text and text.strip()) and not files:
            raise ToolError("нужен text или files")
        if not reply_sign and not chat_id:
            raise ToolError("укажите chat_id или reply_sign")
        sign = reply_sign or await reply_sign_of(seller, str(chat_id), ctx, fresh=False)

        async def send(current_sign: str) -> dict[str, Any]:
            body: dict[str, Any] = {"replySign": current_sign}
            if text:
                body["message"] = text
            if files:
                body["file"] = files
            return await kit.call("POST", "/api/v1/seller/message",
                                  {"seller": seller, "body": body, "dry_run": dry_run,
                                   "force": force},
                                  tool="wb_chat_send", ctx=ctx)

        try:
            return await send(sign)
        except ToolError as exc:
            # подпись чата протухает; при ошибке про sign берём свежую и пробуем ещё раз
            if dry_run or not chat_id or "sign" not in str(exc).lower():
                raise
            fresh_sign = await reply_sign_of(seller, str(chat_id), ctx, fresh=True)
            if fresh_sign == sign:
                raise
            result = await send(fresh_sign)
            result.setdefault("notes", []).append("подпись чата обновлена, отправка повторена")
            return result

    async def wb_returns(
        seller: str | None = None,
        archive: bool = False,
        nm_id: int | None = None,
        claim_id: str | None = None,
        limit: int = CLAIMS_PAGE,
        offset: int = 0,
        all_pages: bool = True,
        max_pages: int = 10,
        fresh: bool = False,
        select: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Заявки покупателей на возврат: на рассмотрении (по умолчанию) или архив.

        Страницы по offset/limit собираются подряд, пока all_pages=true.
        """
        owner = await kit.owner_of(seller)
        query: dict[str, Any] = {"is_archive": archive, "nm_id": nm_id, "id": claim_id,
                                 "fresh": fresh}

        async def fetch(current: int) -> dict[str, Any]:
            return await kit.call("GET", "/api/v1/claims",
                                  {**query, "seller": seller, "limit": limit, "offset": current},
                                  tool="wb_returns", ctx=ctx, compact=False)

        claims, pages, capped = await kit.collect_offset(
            fetch, items_of=lambda r: data_of(r).get("claims") or [], limit=limit,
            max_pages=max_pages if all_pages else 1, start=offset,
        )
        payload = {"claims": claims, "count": len(claims), "pages": pages,
                   "capped": capped and all_pages, "archive": archive}
        return kit.finish(payload, select, tool="wb_returns",
                          operation=kit.op("GET", "/api/v1/claims").id, seller=owner,
                          key="claims")

    for fn in (wb_feedbacks, wb_questions, wb_chats, wb_chat_events, wb_returns):
        server.add_tool(fn, annotations=READ)
    for fn in (wb_feedback_reply, wb_question_answer, wb_chat_send):
        server.add_tool(fn, annotations=WRITE)
