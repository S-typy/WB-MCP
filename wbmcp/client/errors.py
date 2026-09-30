"""Ошибки клиента WB. Тексты рассчитаны на агента: что не так и что делать."""

from __future__ import annotations

from typing import Any


class WbError(Exception):
    """Базовая ошибка клиента."""


class PreflightError(WbError):
    """Допуск не пройден: запрос к WB даже не отправлялся."""

    def __init__(self, problems: list[str]) -> None:
        """Список проблем склеивается в одно сообщение."""
        super().__init__("запрос не отправлен: " + "; ".join(problems))
        self.problems = problems


class WbHttpError(WbError):
    """WB ответил ошибкой (после всех повторов)."""

    def __init__(self, status: int, url: str, data: Any, text: str = "") -> None:
        """Сообщение включает статус, адрес и суть ошибки из тела."""
        self.status = status
        self.url = url
        self.data = data
        self.text = text
        super().__init__(f"WB ответил {status} на {url}: {summarize_error(data, text)}")


class AmbiguousWriteError(WbError):
    """Исход записи неизвестен: таймаут или 5xx. Повторять только после сверки."""

    def __init__(self, journal_key: str, reason: str) -> None:
        """Ключ журнала нужен, чтобы повторить с force после проверки."""
        super().__init__(
            "исход записи неизвестен (" + reason + "); проверьте результат в кабинете и "
            "повторите с force=true, ключ журнала " + journal_key
        )
        self.journal_key = journal_key
        self.reason = reason


class DuplicateWriteError(WbError):
    """Такая же запись уже отправлялась недавно или зависла в неоднозначном состоянии."""

    def __init__(self, journal_key: str, state: str, when: str) -> None:
        """Сообщение говорит, в каком состоянии предыдущая попытка."""
        super().__init__(
            f"такая же запись уже была ({state}, {when}); если это не повтор по ошибке, "
            f"передайте force=true, ключ журнала {journal_key}"
        )
        self.journal_key = journal_key
        self.state = state


class FileTooLargeError(WbError):
    """Файл из ответа больше разрешённого размера."""


def summarize_error(data: Any, text: str = "") -> str:
    """Короткая суть ошибки из тела ответа WB; полное тело остаётся в исключении."""
    if isinstance(data, dict):
        parts = []
        for key in ("title", "detail", "errorText", "message", "error", "code", "status"):
            value = data.get(key)
            if value not in (None, "", []):
                parts.append(f"{key}={value}")
        errors = data.get("errors") or data.get("additionalErrors")
        if errors:
            parts.append(f"errors={errors}")
        if parts:
            return ", ".join(str(p) for p in parts)[:600]
        return str(data)[:600]
    if isinstance(data, list):
        return str(data)[:600]
    return (text or "")[:600] or "без тела"
