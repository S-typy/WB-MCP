"""Метрики в формате Prometheus без сторонних библиотек.

Счётчики и простые суммы с числом наблюдений: этого хватает, чтобы видеть
вызовы по кабинетам и разделам, коды ответов WB (в том числе 429) и время
ожидания лимитов.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from threading import Lock

LabelKey = tuple[tuple[str, str], ...]


def _labels(labels: Mapping[str, object] | None) -> LabelKey:
    return tuple(sorted((k, str(v)) for k, v in (labels or {}).items()))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Metrics:
    """Реестр метрик процесса."""

    def __init__(self, prefix: str = "wbmcp") -> None:
        """Все имена метрик получают общий префикс."""
        self.prefix = prefix
        self._counters: dict[str, dict[LabelKey, float]] = defaultdict(lambda: defaultdict(float))
        self._sums: dict[str, dict[LabelKey, list[float]]] = defaultdict(
            lambda: defaultdict(lambda: [0.0, 0.0])
        )
        self._help: dict[str, str] = {}
        self._lock = Lock()

    def describe(self, name: str, text: str) -> None:
        """Подпись метрики для строки HELP."""
        self._help[name] = text

    def inc(self, name: str, labels: Mapping[str, object] | None = None,
            value: float = 1.0) -> None:
        """Увеличить счётчик."""
        with self._lock:
            self._counters[name][_labels(labels)] += value

    def observe(self, name: str, value: float, labels: Mapping[str, object] | None = None) -> None:
        """Добавить наблюдение: сумма и количество."""
        with self._lock:
            entry = self._sums[name][_labels(labels)]
            entry[0] += value
            entry[1] += 1

    def counter(self, name: str, labels: Mapping[str, object] | None = None) -> float:
        """Текущее значение счётчика."""
        return self._counters.get(name, {}).get(_labels(labels), 0.0)

    def render(self) -> str:
        """Текст экспозиции Prometheus."""
        lines: list[str] = []
        with self._lock:
            for name in sorted(self._counters):
                full = f"{self.prefix}_{name}"
                lines += self._header(full, name, "counter")
                for key, value in sorted(self._counters[name].items()):
                    lines.append(f"{full}{self._fmt(key)} {value:g}")
            for name in sorted(self._sums):
                full = f"{self.prefix}_{name}"
                lines += self._header(full, name, "summary")
                for key, (total, count) in sorted(self._sums[name].items()):
                    lines.append(f"{full}_sum{self._fmt(key)} {total:g}")
                    lines.append(f"{full}_count{self._fmt(key)} {count:g}")
        return "\n".join(lines) + ("\n" if lines else "")

    def _header(self, full: str, name: str, kind: str) -> list[str]:
        lines = []
        if name in self._help:
            lines.append(f"# HELP {full} {self._help[name]}")
        lines.append(f"# TYPE {full} {kind}")
        return lines

    @staticmethod
    def _fmt(key: LabelKey) -> str:
        if not key:
            return ""
        return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in key) + "}"


def default_metrics() -> Metrics:
    """Метрики сервера с подписями."""
    metrics = Metrics()
    metrics.describe("calls_total", "вызовы инструментов по кабинету, разделу и исходу")
    metrics.describe("wb_status_total", "коды ответов WB по кабинету")
    metrics.describe("wait_seconds", "ожидание лимитов перед запросами")
    metrics.describe("elapsed_seconds", "длительность вызовов WB")
    return metrics
