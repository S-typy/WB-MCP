"""Чистка текста из спецификаций: там HTML вперемешку с Markdown."""

from __future__ import annotations

import html
import re

_TAG = re.compile(r"<[^>]+>")
_SUMMARY_TAIL = re.compile(r"\{\{.*?\}\}")
_BLANK_LINES = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t]+")


def strip_html(text: str) -> str:
    """Убрать теги и сущности, схлопнуть лишние пробелы, оставить переносы строк."""
    if not text:
        return ""
    plain = _TAG.sub("", text)
    plain = html.unescape(plain)
    plain = _SPACES.sub(" ", plain)
    plain = "\n".join(line.strip() for line in plain.splitlines())
    return _BLANK_LINES.sub("\n\n", plain).strip()


def clean_summary(summary: str) -> str:
    """У summary в снимках хвост вида ``{{ /path }}`` — он нам ни к чему."""
    return _SUMMARY_TAIL.sub("", summary or "").strip()


def normalize_scope(text: str) -> str:
    """Ключ группы лимита из текста: без тегов, в нижнем регистре, один пробел."""
    plain = strip_html(text).lower()
    return re.sub(r"\s+", " ", plain).strip(" :")
