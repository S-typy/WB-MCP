"""Модели реестра кабинетов."""

from __future__ import annotations

from dataclasses import dataclass, field

from wbmcp.tokens import TokenInfo


@dataclass(frozen=True)
class SellerRecord:
    """Запись хранилища: алиас, токен и заметка. Токен в repr не попадает."""

    alias: str
    token: str = field(repr=False)
    note: str = ""


@dataclass(frozen=True)
class Seller:
    """Кабинет с уже разобраным токеном."""

    alias: str
    token: str = field(repr=False)
    info: TokenInfo = field(repr=False)
    note: str = ""

    @property
    def sid(self) -> str:
        """Идентификатор продавца из токена."""
        return self.info.sid

    def summary(self) -> dict[str, object]:
        """Сводка для инструментов: без токена."""
        return {"alias": self.alias, "note": self.note, **self.info.summary()}
