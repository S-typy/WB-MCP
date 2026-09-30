"""HTTP-клиент WB: допуск, лимиты, повторы, файлы, журнал записей."""

from wbmcp.client.errors import (
    AmbiguousWriteError,
    DuplicateWriteError,
    FileTooLargeError,
    PreflightError,
    WbError,
    WbHttpError,
)
from wbmcp.client.files import FileStore, SavedFile
from wbmcp.client.http import CallResult, RetryPolicy, WbClient
from wbmcp.client.journal import JournalEntry, WriteJournal
from wbmcp.client.preflight import PreflightReport, check_access

__all__ = [
    "AmbiguousWriteError",
    "CallResult",
    "DuplicateWriteError",
    "FileStore",
    "FileTooLargeError",
    "JournalEntry",
    "PreflightError",
    "PreflightReport",
    "RetryPolicy",
    "SavedFile",
    "WbClient",
    "WbError",
    "WbHttpError",
    "WriteJournal",
    "check_access",
]
