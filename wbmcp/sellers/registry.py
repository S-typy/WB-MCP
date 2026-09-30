"""Реестр кабинетов поверх одного или нескольких хранилищ."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from wbmcp.sellers.models import Seller, SellerRecord
from wbmcp.sellers.store import ReadOnlyStoreError, SellerStore
from wbmcp.tokens import TokenFormatError, TokenInfo, decode_token


class UnknownSellerError(KeyError):
    """Кабинета с таким алиасом нет."""

    def __init__(self, alias: str | None, known: Sequence[str]) -> None:
        """Сообщение сразу подсказывает, какие алиасы есть."""
        options = ", ".join(known) if known else "ни одного"
        super().__init__(f"кабинет {alias!r} не найден; доступны: {options}")
        self.alias = alias
        self.known = list(known)


class SellerRegistry:
    """Единая точка доступа к кабинетам: хранилища опрашиваются по порядку."""

    def __init__(self, stores: Sequence[SellerStore],
                 guard: Callable[[TokenInfo, str], None] | None = None) -> None:
        """Первое хранилище с ``writable`` принимает новые кабинеты.

        ``guard`` — дополнительная проверка токена перед записью (политика облака): она
        обязана быть той же, что при старте, иначе кабинет пройдёт мимо правил.
        """
        self._stores = list(stores)
        self._guard = guard

    async def _records(self) -> list[SellerRecord]:
        seen: dict[str, SellerRecord] = {}
        for store in self._stores:
            for record in await store.list_records():
                seen.setdefault(record.alias, record)
        return list(seen.values())

    @staticmethod
    def _to_seller(record: SellerRecord) -> Seller:
        return Seller(alias=record.alias, token=record.token, info=decode_token(record.token),
                      note=record.note)

    async def aliases(self) -> list[str]:
        """Алиасы всех кабинетов."""
        return [r.alias for r in await self._records()]

    async def list(self) -> list[Seller]:
        """Все кабинеты с разобранными токенами."""
        return [self._to_seller(r) for r in await self._records()]

    async def get(self, alias: str) -> Seller:
        """Кабинет по алиасу."""
        for store in self._stores:
            record = await store.get(alias)
            if record is not None:
                return self._to_seller(record)
        raise UnknownSellerError(alias, await self.aliases())

    async def resolve(self, alias: str | None) -> Seller:
        """Кабинет по алиасу; без алиаса — единственный кабинет, если он один."""
        if alias:
            return await self.get(alias)
        records = await self._records()
        if len(records) == 1:
            return self._to_seller(records[0])
        raise UnknownSellerError(None, [r.alias for r in records])

    async def add(self, alias: str, token: str, note: str = "") -> Seller:
        """Проверить токен и положить в первое пишущее хранилище."""
        try:
            info = decode_token(token)
        except TokenFormatError as exc:
            raise ValueError(f"токен для {alias!r} не принят: {exc}") from exc
        if self._guard is not None:
            self._guard(info, alias)
        for store in self._stores:
            if store.writable:
                await store.add(SellerRecord(alias=alias, token=token, note=note))
                return await self.get(alias)
        raise ReadOnlyStoreError("нет хранилища, куда можно записать кабинет")

    async def remove(self, alias: str) -> bool:
        """Удалить кабинет из пишущих хранилищ."""
        removed = False
        for store in self._stores:
            if store.writable and await store.remove(alias):
                removed = True
        return removed
