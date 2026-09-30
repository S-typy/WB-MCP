"""Шифрование токенов в хранилище.

Fernet берётся из пакета cryptography, если он установлен. Без ключа токены
лежат как есть — это допустимо только для локального режима, и сервер об этом
предупредит.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Protocol

PLAIN_PREFIX = "plain:"
FERNET_PREFIX = "fernet:"


class CipherError(ValueError):
    """Не удалось расшифровать или нет ключа."""


class TokenCipher(Protocol):
    """Обратимое преобразование токена в строку для хранения."""

    def encrypt(self, token: str) -> str:
        """Токен -> строка для базы."""
        ...

    def decrypt(self, stored: str) -> str:
        """Строка из базы -> токен."""
        ...


class PlainCipher:
    """Без шифрования, только пометка формата."""

    def encrypt(self, token: str) -> str:
        """Добавить пометку формата."""
        return PLAIN_PREFIX + token

    def decrypt(self, stored: str) -> str:
        """Снять пометку; зашифрованное без ключа прочитать нельзя."""
        if stored.startswith(PLAIN_PREFIX):
            return stored[len(PLAIN_PREFIX):]
        if stored.startswith(FERNET_PREFIX):
            raise CipherError("токен зашифрован, а ключ WBMCP_TOKEN_KEY не задан")
        return stored


class FernetCipher:
    """Симметричное шифрование Fernet (AES-128-CBC + HMAC)."""

    def __init__(self, key: str | bytes) -> None:
        """Создать шифр по ключу Fernet (urlsafe base64, 32 байта)."""
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:  # pragma: no cover - зависит от окружения
            raise CipherError("для шифрования нужен пакет cryptography") from exc
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError) as exc:
            raise CipherError("ключ WBMCP_TOKEN_KEY не похож на ключ Fernet") from exc

    @staticmethod
    def generate_key() -> str:
        """Новый ключ для WBMCP_TOKEN_KEY."""
        from cryptography.fernet import Fernet

        return Fernet.generate_key().decode("ascii")

    def encrypt(self, token: str) -> str:
        """Зашифровать и пометить формат."""
        return FERNET_PREFIX + self._fernet.encrypt(token.encode("utf-8")).decode("ascii")

    def decrypt(self, stored: str) -> str:
        """Расшифровать; незашифрованные записи проходят как есть."""
        from cryptography.fernet import InvalidToken

        if stored.startswith(PLAIN_PREFIX):
            return stored[len(PLAIN_PREFIX):]
        if not stored.startswith(FERNET_PREFIX):
            return stored
        try:
            payload = stored[len(FERNET_PREFIX):].encode("ascii")
            return self._fernet.decrypt(payload).decode("utf-8")
        except InvalidToken as exc:
            raise CipherError("токен не расшифрован: ключ не тот или запись повреждена") from exc


def cipher_from_env(
    env: Mapping[str, str] | None = None, key_env: str = "WBMCP_TOKEN_KEY"
) -> TokenCipher:
    """Fernet при наличии ключа в окружении, иначе PlainCipher."""
    env = os.environ if env is None else env
    key = env.get(key_env, "").strip()
    if key:
        return FernetCipher(key)
    return PlainCipher()
