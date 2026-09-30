"""Проверка допуска до расхода квоты.

Смотрим на разобраный токен и расширения операции: категория, режим «только
чтение», тип токена, песочница, срок. Ошибка говорит, какой токен нужен.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field

from wbmcp.catalog.models import Operation
from wbmcp.sellers.models import Seller
from wbmcp.tokens import CATEGORY_TITLES, TOKEN_TYPE_TITLES, TokenType

# x-token-types в спецификациях -> наши типы. Базовый «с секретом» по JWT не отличить,
# считаем, что базовый токен подходит.
SPEC_TOKEN_TYPES: dict[str, TokenType] = {
    "personal": TokenType.PERSONAL,
    "service": TokenType.SERVICE,
    "base-with-secret": TokenType.BASIC,
    "basic": TokenType.BASIC,
    "test": TokenType.TEST,
}


@dataclass
class PreflightReport:
    """Итог проверки: хост для запроса, проблемы (блокируют) и заметки (нет)."""

    host: str
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Можно ли отправлять запрос."""
        return not self.problems


def check_access(
    op: Operation, seller: Seller, *, server_read_only: bool = False, host: str | None = None,
    allowed_hosts: Collection[str] | None = None,
) -> PreflightReport:
    """Проверить, что токен кабинета вообще может вызвать операцию.

    ``host`` — явно заданный хост (например, для ping). Он обязан быть из ``allowed_hosts``,
    то есть из каталога: токен в заголовке уходит только на хосты WB. Песочный хост с
    тестовым токеном допустим, даже если у операции в спецификации песочницы нет.
    """
    info = seller.info
    default_host = op.sandbox_host if info.sandbox and op.sandbox_host else op.host
    report = PreflightReport(host=host or default_host)
    if host and allowed_hosts is not None and host not in allowed_hosts:
        report.problems.append(f"хост {host!r} не из каталога WB, запрос туда не уйдёт")
    if info.is_expired():
        report.problems.append(f"токен кабинета {seller.alias!r} истёк, выпустите новый")
    if info.sandbox and op.sandbox_host is None and not (host and "sandbox" in host):
        report.problems.append("метод недоступен в песочнице, а токен тестовый")
    if not info.allows(op.token_category):
        title = CATEGORY_TITLES[op.token_category] if op.token_category else "?"
        report.problems.append(f"в токене нет категории «{title}»")
    if info.read_only and op.read_only is False:
        report.problems.append("токен «только на чтение», а метод меняет данные")
    if server_read_only and op.mutating:
        report.problems.append("сервер запущен в режиме только чтения")
    if op.token_types:
        allowed = {SPEC_TOKEN_TYPES.get(t) for t in op.token_types} - {None}
        if info.token_type not in allowed:
            names = ", ".join(TOKEN_TYPE_TITLES[t] for t in sorted(allowed, key=str))
            report.problems.append(
                f"метод доступен только токенам: {names}; у кабинета "
                f"{TOKEN_TYPE_TITLES[info.token_type]}"
            )
    if "jam" in op.flags:
        report.notes.append("метод требует подписку «Джем», без неё WB вернёт 403")
    if op.deprecated:
        report.notes.append("метод помечен устаревшим, ищите замену в каталоге")
    days = info.days_left()
    if days is not None and 0 <= days <= 14:
        report.notes.append(f"токен истекает через {days} дн.")
    return report
