"""Авторизация MCP-клиентов: ключи, scope и разрешённые кабинеты.

Агент авторизуется у сервера, а не токеном WB. В stdio-режиме клиент один и
ему можно всё; в HTTP-режиме каждый ключ из настроек — свой принципал.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from mcp.server.auth.provider import AccessToken

from wbmcp.catalog.index import Catalog, UnknownOperationError
from wbmcp.catalog.models import Operation
from wbmcp.config import ClientConfig

SCOPES = ("read", "write", "finance", "admin")
FINANCE_CATEGORIES = frozenset({"finance", "documents"})
ADMIN_CATEGORIES = frozenset({"usermanagement"})


class AccessDeniedError(PermissionError):
    """Клиенту не хватает scope или кабинет ему не разрешён."""


@dataclass(frozen=True)
class ClientPrincipal:
    """Кто зовёт инструменты и что ему можно."""

    name: str
    scopes: frozenset[str]
    sellers: frozenset[str] | None = None

    def allows_seller(self, alias: str) -> bool:
        """Разрешён ли кабинет; None в ``sellers`` значит все."""
        return self.sellers is None or alias in self.sellers

    def has_scopes(self, required: Iterable[str]) -> bool:
        """Есть ли все нужные scope."""
        return set(required) <= self.scopes

    def check_seller(self, alias: str) -> None:
        """Ошибка, если кабинет запрещён."""
        if not self.allows_seller(alias):
            raise AccessDeniedError(f"клиенту {self.name!r} кабинет {alias!r} не разрешён")

    def check_scopes(self, required: Iterable[str]) -> None:
        """Ошибка, если не хватает scope."""
        missing = sorted(set(required) - self.scopes)
        if missing:
            raise AccessDeniedError(
                f"клиенту {self.name!r} нужны scope {', '.join(missing)}"
            )


LOCAL_PRINCIPAL = ClientPrincipal(name="local", scopes=frozenset(SCOPES), sellers=None)


def required_scopes(op: Operation) -> set[str]:
    """Какие scope нужны, чтобы звать операцию."""
    scopes = {"write" if op.mutating else "read"}
    if op.category in FINANCE_CATEGORIES:
        scopes.add("finance")
    if op.category in ADMIN_CATEGORIES:
        scopes.add("admin")
    return scopes


def scopes_for_result(catalog: Catalog, operation_id: str) -> set[str]:
    """Scope для чтения сохранённой выгрузки: те же, что у операции; неизвестная — только admin."""
    try:
        return required_scopes(catalog.get(operation_id))
    except UnknownOperationError:
        return {"admin"}


class ClientRegistry:
    """Ключи клиентов из настроек."""

    def __init__(
        self, clients: Iterable[ClientConfig], env: Mapping[str, str] | None = None
    ) -> None:
        """Ключи достаются сразу; неизвестные scope — ошибка настроек."""
        env = os.environ if env is None else env
        self._by_key: dict[str, ClientPrincipal] = {}
        for entry in clients:
            unknown = set(entry.scopes) - set(SCOPES)
            if unknown:
                raise ValueError(f"клиент {entry.name!r}: неизвестные scope {sorted(unknown)}")
            sellers = None if "*" in entry.sellers else frozenset(entry.sellers)
            principal = ClientPrincipal(entry.name, frozenset(entry.scopes), sellers)
            self._by_key[entry.resolve_key(env)] = principal

    def __len__(self) -> int:
        """Сколько клиентов настроено."""
        return len(self._by_key)

    def by_key(self, key: str) -> ClientPrincipal | None:
        """Принципал по ключу; сравнение без утечки по времени."""
        for known, principal in self._by_key.items():
            if hmac.compare_digest(known.encode(), key.encode()):
                return principal
        return None


class StaticTokenVerifier:
    """TokenVerifier для MCP: bearer-ключ клиента -> AccessToken."""

    def __init__(self, registry: ClientRegistry) -> None:
        """Проверяет ключи по реестру клиентов."""
        self._registry = registry

    async def verify_token(self, token: str) -> AccessToken | None:
        """AccessToken с именем клиента и его scope или None."""
        principal = self._registry.by_key(token)
        if principal is None:
            return None
        return AccessToken(token=token, client_id=principal.name, scopes=sorted(principal.scopes))


def principal_from_access_token(
    token: AccessToken | None, registry: ClientRegistry | None
) -> ClientPrincipal:
    """Принципал текущего запроса: по bearer-ключу или локальный."""
    if token is None or registry is None:
        return LOCAL_PRINCIPAL
    principal = registry.by_key(token.token)
    if principal is None:
        raise AccessDeniedError("ключ клиента не найден")
    return principal
