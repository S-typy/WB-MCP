"""Разбор токена API продавца Wildberries.

Токен — это JWT. Подпись не проверяем: ключа у нас нет, да и незачем, WB сам
отвергнет испорченый токен. Из payload нужны поля sid, s, t, acc и exp.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum, StrEnum


class TokenType(StrEnum):
    """Тип токена по полю ``acc``."""

    BASIC = "basic"
    TEST = "test"
    PERSONAL = "personal"
    SERVICE = "service"


ACC_TO_TYPE = {
    1: TokenType.BASIC,
    2: TokenType.TEST,
    3: TokenType.PERSONAL,
    4: TokenType.SERVICE,
}

TOKEN_TYPE_TITLES = {
    TokenType.BASIC: "базовый",
    TokenType.TEST: "тестовый",
    TokenType.PERSONAL: "персональный",
    TokenType.SERVICE: "сервисный",
}


class Category(IntEnum):
    """Категории доступа токена, значение — номер бита в маске ``s``."""

    CONTENT = 1
    ANALYTICS = 2
    PRICES = 3
    MARKETPLACE = 4
    STATISTICS = 5
    PROMOTION = 6
    FEEDBACKS = 7
    CHAT = 9
    SUPPLIES = 10
    RETURNS = 11
    DOCUMENTS = 12
    FINANCE = 13
    USERS = 16


READ_ONLY_BIT = 30

CATEGORY_TITLES = {
    Category.CONTENT: "Контент",
    Category.ANALYTICS: "Аналитика",
    Category.PRICES: "Цены и скидки",
    Category.MARKETPLACE: "Маркетплейс",
    Category.STATISTICS: "Статистика",
    Category.PROMOTION: "Продвижение",
    Category.FEEDBACKS: "Вопросы и отзывы",
    Category.CHAT: "Чат с покупателями",
    Category.SUPPLIES: "Поставки",
    Category.RETURNS: "Возвраты",
    Category.DOCUMENTS: "Документы",
    Category.FINANCE: "Финансы",
    Category.USERS: "Пользователи",
}

# Значения x-category из спецификаций WB. commonapi (ping, новости, seller-info)
# доступен любому токену, поэтому категории у него нет.
SPEC_CATEGORY: dict[str, Category | None] = {
    "commonapi": None,
    "content": Category.CONTENT,
    "contentanalytics": Category.ANALYTICS,
    "discountsandprices": Category.PRICES,
    "marketplace": Category.MARKETPLACE,
    "statistics": Category.STATISTICS,
    "advert": Category.PROMOTION,
    "questionsandfeedback": Category.FEEDBACKS,
    "buyerchat": Category.CHAT,
    "supplies": Category.SUPPLIES,
    "returns": Category.RETURNS,
    "documents": Category.DOCUMENTS,
    "finance": Category.FINANCE,
    "usermanagement": Category.USERS,
}


class TokenFormatError(ValueError):
    """Строка не похожа на токен WB."""


@dataclass(frozen=True)
class TokenInfo:
    """Что известно о токене без единого запроса к WB."""

    sid: str
    token_type: TokenType
    scopes: int
    sandbox: bool
    expires_at: datetime | None

    @property
    def categories(self) -> frozenset[Category]:
        """Категории, включённые в маске ``s``."""
        return frozenset(c for c in Category if self.scopes & (1 << c))

    @property
    def read_only(self) -> bool:
        """Токен «только на чтение» (бит 30)."""
        return bool(self.scopes & (1 << READ_ONLY_BIT))

    def allows(self, category: Category | None) -> bool:
        """Есть ли у токена доступ к категории; ``None`` — категория не нужна.

        У тестового токена маска пустая, но песочница пускает его на все свои методы
        (проверено 29.09.2026), поэтому для него категории не проверяются.
        """
        if self.token_type is TokenType.TEST:
            return True
        return category is None or category in self.categories

    def is_expired(self, now: datetime | None = None) -> bool:
        """Истёк ли срок токена на момент ``now`` (по умолчанию сейчас)."""
        if self.expires_at is None:
            return False
        return (now or datetime.now(UTC)) >= self.expires_at

    def days_left(self, now: datetime | None = None) -> int | None:
        """Сколько полных дней осталось до истечения, отрицательное — уже истёк."""
        if self.expires_at is None:
            return None
        delta = self.expires_at - (now or datetime.now(UTC))
        return delta.days

    def summary(self) -> dict[str, object]:
        """Описание токена для инструментов; самого токена здесь нет."""
        return {
            "sid": self.sid,
            "token_type": self.token_type.value,
            "token_type_title": TOKEN_TYPE_TITLES[self.token_type],
            "categories": (
                "все методы песочницы" if self.token_type is TokenType.TEST
                else [CATEGORY_TITLES[c] for c in sorted(self.categories)]
            ),
            "read_only": self.read_only,
            "sandbox": self.sandbox,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "days_left": self.days_left(),
        }


def _b64url_decode(part: str) -> bytes:
    padding = "=" * (-len(part) % 4)
    return base64.urlsafe_b64decode(part + padding)


def decode_token(token: str) -> TokenInfo:
    """Разобрать payload токена; подпись не проверяется."""
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise TokenFormatError("ожидается JWT из трёх частей через точку")
    try:
        payload = json.loads(_b64url_decode(parts[1]))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise TokenFormatError("payload токена не читается") from exc
    if not isinstance(payload, dict) or "sid" not in payload:
        raise TokenFormatError("в payload токена нет sid")
    acc = payload.get("acc")
    token_type = ACC_TO_TYPE.get(acc) if isinstance(acc, int) else None
    if token_type is None:
        raise TokenFormatError(f"неизвестный тип токена acc={acc!r}")
    scopes = payload.get("s", 0)
    if not isinstance(scopes, int):
        raise TokenFormatError("маска категорий s должна быть числом")
    exp = payload.get("exp")
    expires_at = None
    if isinstance(exp, (int, float)) and not isinstance(exp, bool):
        expires_at = datetime.fromtimestamp(exp, tz=UTC)
    return TokenInfo(
        sid=str(payload["sid"]),
        token_type=token_type,
        scopes=scopes,
        sandbox=bool(payload.get("t")),
        expires_at=expires_at,
    )


def short_token(token: str) -> str:
    """Безопасный вид токена для логов: только начало и конец."""
    if len(token) <= 12:
        return "***"
    return f"{token[:4]}...{token[-4:]}"
