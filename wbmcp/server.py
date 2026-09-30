"""MCP-сервер wb-mcp: базовые инструменты плюс сгенерированные по профилям."""

from __future__ import annotations

import json
from typing import Any

from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ResourceError
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as MCPTool

from wbmcp import __version__
from wbmcp.app import AppContext
from wbmcp.results import UnknownResultError
from wbmcp.tools import api, changes, core, curated
from wbmcp.tools.confirm import Confirmer, ElicitConfirmer
from wbmcp.tools.core import result_viewer
from wbmcp.tools.generated import GeneratedTools

INSTRUCTIONS = """Сервер даёт доступ к API продавца Wildberries от имени кабинетов из реестра.

Как работать:
- wb_sellers показывает кабинеты (алиасы) и права их токенов; в инструментах кабинет
  указывается аргументом seller, при одном кабинете его можно опустить.
- wb_api_search и wb_api_describe — поиск операции и её полная карточка со схемой аргументов.
- wb_request зовёт любую операцию по методу и пути; wb_op_<operationId> — то же, но с готовой
  схемой аргументов для операций из включённых профилей.
- Лимиты WB соблюдаются автоматически: при нехватке квоты вызов ждёт; wb_quota показывает остаток.
- Записи: dry_run собирает запрос без отправки, force повторяет запись вопреки журналу,
  опасные действия (отмена, удаление, бюджеты, пользователи) просят подтверждение.
- Ответы WB отдаются целиком, ничего не урезается. Ответ больше порога сохраняется в файл
  целиком, в ответе остаётся образец и stored.id: срезы и проекции через wb_result (JMESPath),
  полный текст через ресурс wb://results/<id>. У читающих инструментов есть select (JMESPath).
  Файлы (отчёты, ярлыки) сохраняются на диск, в ответе путь и sha256; маленькие ещё и в base64.
"""


class WbMcpServer(MCPServer):
    """MCPServer с динамическим списком инструментов из каталога."""

    def __init__(
        self,
        app: AppContext,
        *,
        confirmer: Confirmer | None = None,
        token_verifier: TokenVerifier | None = None,
        auth: AuthSettings | None = None,
    ) -> None:
        """Регистрирует базовые инструменты и собирает сгенерированные."""
        super().__init__(
            name=app.config.server.name,
            version=__version__,
            instructions=INSTRUCTIONS,
            token_verifier=token_verifier,
            auth=auth,
        )
        self.app = app
        self.confirmer: Confirmer = confirmer or ElicitConfirmer()
        core.register(self, app, self.confirmer)
        api.register(self, app, self.confirmer)
        curated.register(self, app, self.confirmer)
        changes.register(self, app, self.confirmer)
        self.generated = GeneratedTools(app, self.confirmer)
        self._register_resources()

    def _register_resources(self) -> None:
        """Ресурс wb://results/{id}: полная сохранённая выгрузка для удалённых клиентов."""
        app = self.app

        @self.resource("wb://results/{result_id}", name="wb_result_file",
                       description="Полная сохранённая выгрузка WB по id из wb_result",
                       mime_type="application/json")
        async def stored_result(result_id: str) -> str:
            # тот же фильтр, что у wb_result: чужой кабинет или scope — «не найдено»
            try:
                _, data = app.results.load(result_id, result_viewer(app, app.principal()))
            except UnknownResultError as exc:
                raise ResourceError(str(exc)) from exc
            return json.dumps(data, ensure_ascii=False, default=str)

    async def list_tools(self) -> list[MCPTool]:
        """Базовые инструменты плюс сгенерированные."""
        return await super().list_tools() + self.generated.list_tools()

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context | None = None
    ) -> Any:
        """Сгенерированные инструменты обрабатываются здесь, остальные — как обычно."""
        if self.generated.owns(name):
            payload = await self.generated.call(name, arguments, context)
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False,
                                                                  indent=1, default=str))],
                structured_content=payload,
            )
        return await super().call_tool(name, arguments, context)
