"""Реестр кабинетов, хранилища и шифрование токенов."""

import sqlite3

import pytest

from wbmcp.config import SellerConfig
from wbmcp.sellers import (
    ConfigSellerStore,
    ReadOnlyStoreError,
    SellerRecord,
    SellerRegistry,
    SqliteSellerStore,
    UnknownSellerError,
)
from wbmcp.sellers.crypto import CipherError, FernetCipher, PlainCipher, cipher_from_env


async def test_record_and_seller_hide_token(make_token):
    token = make_token()
    record = SellerRecord(alias="a", token=token)
    assert token not in repr(record)
    registry = SellerRegistry([ConfigSellerStore([SellerConfig(alias="a", token=token)])])
    seller = await registry.get("a")
    assert token not in repr(seller)
    assert seller.sid == "1234567"
    assert seller.summary()["alias"] == "a"
    assert token not in str(seller.summary())


async def test_config_store_is_read_only(make_token):
    store = ConfigSellerStore([SellerConfig(alias="a", token_env="T")], env={"T": make_token()})
    assert [r.alias for r in await store.list_records()] == ["a"]
    assert await store.get("a") is not None
    assert await store.get("zzz") is None
    with pytest.raises(ReadOnlyStoreError):
        await store.add(SellerRecord(alias="b", token="x"))
    with pytest.raises(ReadOnlyStoreError):
        await store.remove("a")


async def test_sqlite_store_roundtrip(tmp_path, make_token):
    key = FernetCipher.generate_key()
    store = SqliteSellerStore(tmp_path / "db" / "sellers.db", FernetCipher(key))
    token = make_token(sid="9")
    await store.add(SellerRecord(alias="s", token=token, note="n"))
    assert (await store.get("s")).token == token
    assert [r.note for r in await store.list_records()] == ["n"]
    # в базе токен лежит зашифрованым
    conn = sqlite3.connect(tmp_path / "db" / "sellers.db")
    raw = conn.execute("SELECT token FROM sellers").fetchone()[0]
    assert raw.startswith("fernet:") and token not in raw
    # другой ключ не прочитает
    other_key = FernetCipher(FernetCipher.generate_key())
    other = SqliteSellerStore(tmp_path / "db" / "sellers.db", other_key)
    with pytest.raises(CipherError):
        await other.get("s")
    # без ключа тоже
    plain = SqliteSellerStore(tmp_path / "db" / "sellers.db", PlainCipher())
    with pytest.raises(CipherError):
        await plain.get("s")
    assert await store.remove("s") is True
    assert await store.remove("s") is False
    assert await store.get("s") is None


async def test_plain_store_and_cipher_helpers(tmp_path):
    store = SqliteSellerStore(tmp_path / "p.db")
    await store.add(SellerRecord(alias="p", token="a.b.c"))
    assert (await store.get("p")).token == "a.b.c"
    assert PlainCipher().decrypt("legacy-raw") == "legacy-raw"
    fernet = FernetCipher(FernetCipher.generate_key())
    assert fernet.decrypt("plain:x.y.z") == "x.y.z"
    assert fernet.decrypt("raw") == "raw"
    with pytest.raises(CipherError):
        FernetCipher("not-a-key")
    assert isinstance(cipher_from_env({}), PlainCipher)
    key = FernetCipher.generate_key()
    assert isinstance(cipher_from_env({"WBMCP_TOKEN_KEY": key}), FernetCipher)


async def test_registry_lookup_and_resolve(tmp_path, make_token):
    config_store = ConfigSellerStore([SellerConfig(alias="main", token=make_token(sid="1"))])
    sqlite_store = SqliteSellerStore(tmp_path / "s.db")
    registry = SellerRegistry([config_store, sqlite_store])
    assert await registry.aliases() == ["main"]
    assert (await registry.resolve(None)).alias == "main"
    added = await registry.add("second", make_token(sid="2"), note="второй")
    assert added.sid == "2"
    assert sorted(await registry.aliases()) == ["main", "second"]
    assert [s.alias for s in await registry.list()] == ["main", "second"]
    with pytest.raises(UnknownSellerError) as info:
        await registry.resolve(None)
    assert "main" in str(info.value) and "second" in str(info.value)
    with pytest.raises(UnknownSellerError):
        await registry.get("ghost")
    # запись в файле настроек перекрывает одноимённую в базе
    await sqlite_store.add(SellerRecord(alias="main", token=make_token(sid="777")))
    assert (await registry.get("main")).sid == "1"
    assert await registry.remove("second") is True
    assert await registry.remove("second") is False


async def test_registry_add_validates_and_needs_writable_store(make_token):
    registry = SellerRegistry([ConfigSellerStore([])])
    with pytest.raises(ValueError):
        await registry.add("x", "not-a-token")
    with pytest.raises(ReadOnlyStoreError):
        await registry.add("x", make_token())
    with pytest.raises(UnknownSellerError):
        await registry.resolve(None)
