"""Подтверждение опасных действий через elicitation MCP."""

from __future__ import annotations

from typing import Any, Protocol

from mcp.server.elicitation import AcceptedElicitation
from pydantic import BaseModel, Field

from wbmcp.catalog.models import Operation

DANGEROUS_MARKERS = ("cancel", "delete", "/budget", "/users", "autoreturn", "reject", "/deposit")


def is_dangerous(op: Operation) -> bool:
    """Отмена, удаление, бюджеты кампаний, доступы пользователей — только с подтверждением."""
    return op.method == "DELETE" or any(marker in op.path for marker in DANGEROUS_MARKERS)


class ConfirmSchema(BaseModel):
    """Ответ пользователя на вопрос «точно выполнить?»."""

    confirm: bool = Field(description="true — выполнить, false — отменить")


class Confirmer(Protocol):
    """Как спрашивать подтверждение."""

    async def confirm(self, ctx: Any, message: str) -> bool:
        """Вернуть True, если действие подтверждено."""
        ...


class ElicitConfirmer:
    """Спрашивает у клиента MCP; без сессии или без поддержки — считает отказом."""

    async def confirm(self, ctx: Any, message: str) -> bool:
        """Elicitation с одним полем confirm."""
        if ctx is None:
            return False
        try:
            capabilities = ctx.client_capabilities
        except (ValueError, AttributeError):
            return False
        if capabilities is None or getattr(capabilities, "elicitation", None) is None:
            return False
        result = await ctx.elicit(message, ConfirmSchema)
        return isinstance(result, AcceptedElicitation) and bool(result.data.confirm)


class AutoConfirmer:
    """Всегда один и тот же ответ: для тестов и флага --yes."""

    def __init__(self, answer: bool) -> None:
        """Запомнить ответ."""
        self.answer = answer

    async def confirm(self, ctx: Any, message: str) -> bool:
        """Заранее известный ответ."""
        return self.answer
