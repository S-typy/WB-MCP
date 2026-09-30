# Copyright (c) 2026 S-typy
#
# Облачный режим wb-mcp распространяется на условиях коммерческой лицензии,
# см. wbmcp/cloud/LICENSE. Ядро сервера лицензировано под GNU AGPL-3.0,
# см. LICENSE в корне репозитория.
"""Правила облачного режима из оферты WB и требований Каталога решений."""

from __future__ import annotations

from wbmcp.sellers.registry import SellerRegistry
from wbmcp.tokens import TOKEN_TYPE_TITLES, TokenInfo, TokenType

# Персональные токены в облачных сервисах запрещены офертой, тестовые бесполезны.
ALLOWED_CLOUD_TOKENS = frozenset({TokenType.SERVICE, TokenType.BASIC})
DEFAULT_AGENT_PREFIX = "wb-mcp/"


class CloudPolicyError(ValueError):
    """Нарушение правил облачного режима."""


class CloudPolicy:
    """Проверки, которые облако делает при старте и при добавлении кабинета."""

    def __init__(self, allowed: frozenset[TokenType] = ALLOWED_CLOUD_TOKENS) -> None:
        """Набор допустимых типов токена можно сузить до одного сервисного."""
        self.allowed = allowed

    def check_token(self, info: TokenInfo, alias: str = "") -> None:
        """Ошибка, если тип токена в облаке недопустим."""
        if info.token_type not in self.allowed:
            title = TOKEN_TYPE_TITLES[info.token_type]
            raise CloudPolicyError(
                f"кабинет {alias!r}: {title} токен в облачном режиме запрещён, "
                "нужен сервисный токен из Каталога решений"
            )

    async def check_registry(self, registry: SellerRegistry) -> None:
        """Проверить все кабинеты реестра."""
        for seller in await registry.list():
            self.check_token(seller.info, seller.alias)

    def check_user_agent(self, user_agent: str) -> None:
        """Каталог требует уникальный User-Agent сервиса, умолчание не годится."""
        if not user_agent or user_agent.startswith(DEFAULT_AGENT_PREFIX):
            raise CloudPolicyError(
                "в облачном режиме задайте свой user_agent в настройках: "
                "WB идентифицирует сервис по нему"
            )
