"""Хранилище файлов из ответов WB: отчёты, ярлыки, документы."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wbmcp.client.errors import FileTooLargeError

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class SavedFile:
    """Сохранённый файл и что о нём известно."""

    path: Path
    size: int
    content_type: str
    sha256: str

    def as_dict(self, inline_limit: int = 0) -> dict[str, Any]:
        """Описание для агента; маленькие файлы можно отдать и в base64."""
        result: dict[str, Any] = {
            "path": str(self.path),
            "size": self.size,
            "content_type": self.content_type,
            "sha256": self.sha256,
        }
        if 0 < self.size <= inline_limit:
            result["base64"] = base64.b64encode(self.path.read_bytes()).decode("ascii")
        return result


class FileStore:
    """Кладёт байты в каталог с безопасным именем и ограничением размера."""

    def __init__(self, directory: Path, max_bytes: int = 20 * 1024 * 1024) -> None:
        """Каталог создаётся сразу."""
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes

    def save(self, content: bytes, content_type: str, name_hint: str = "file") -> SavedFile:
        """Сохранить содержимое; расширение берётся из типа, если в подсказке его нет."""
        if len(content) > self.max_bytes:
            raise FileTooLargeError(
                f"файл {len(content)} байт больше лимита {self.max_bytes}; "
                "поднимите max_file_bytes"
            )
        stem = _SAFE.sub("_", name_hint).strip("._") or "file"
        if "." not in stem:
            ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".bin"
            stem += ext
        path = self.directory / f"{uuid.uuid4().hex[:8]}_{stem}"
        path.write_bytes(content)
        return SavedFile(
            path=path,
            size=len(content),
            content_type=content_type,
            sha256=hashlib.sha256(content).hexdigest(),
        )

    @staticmethod
    def read_part(spec: dict[str, Any]) -> tuple[str, bytes]:
        """Файл для multipart из описания агента: путь или base64."""
        if "path" in spec:
            path = Path(str(spec["path"])).expanduser()
            if not path.is_file():
                raise ValueError(f"файла {path} нет или это не файл")
            return path.name, path.read_bytes()
        if "base64" in spec:
            return spec.get("filename") or "upload.bin", base64.b64decode(spec["base64"])
        raise ValueError(
            "файл задаётся как {\"path\": ...} или {\"base64\": ..., \"filename\": ...}"
        )


def check_upload_paths(body: Mapping[str, Any], *, remote: bool, uploads: Path) -> None:
    """Пути к файлам в multipart: локально любые, удалённым клиентам только из ``uploads``.

    Иначе клиент по сети мог бы отправить в WB любой файл с диска сервера. Путь сравнивается
    с каталогом загрузок после раскрытия симлинков, base64 не ограничивается.
    """
    if not remote:
        return
    root = uploads.expanduser().resolve()
    for name, value in body.items():
        if isinstance(value, dict) and "path" in value:
            real = Path(str(value["path"])).expanduser().resolve()
            if not real.is_relative_to(root):
                raise ValueError(
                    f"поле {name}: удалённым клиентам доступны только файлы из каталога "
                    f"{root} или содержимое в base64"
                )
