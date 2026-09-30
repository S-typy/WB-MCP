"""Курируемые инструменты: задачи продавца, собранные из нескольких операций.

Здесь нет ничего, чего нельзя сделать через wb_request, но здесь спрятаны пагинация,
ожидание отчётов и формы тел, которые иначе пришлось бы каждый раз вспоминать заново.
Модули по темам: заказы FBS, карточки, склады и цены, общение с покупателями, реклама,
поставки FBW, финансы и тарифы, отчёты.
"""

from __future__ import annotations

from typing import Any

from wbmcp.app import AppContext
from wbmcp.tools.confirm import Confirmer
from wbmcp.tools.curated import (
    communication,
    content,
    fbw,
    finance,
    inventory,
    orders,
    promo,
    reports,
)
from wbmcp.tools.curated.kit import Kit
from wbmcp.tools.curated.reports import DONE_STATUSES, FAILED_STATUSES, TASK_REPORTS

MODULES = (orders, content, inventory, communication, promo, fbw, finance, reports)

__all__ = ["DONE_STATUSES", "FAILED_STATUSES", "TASK_REPORTS", "register"]


def register(server: Any, app: AppContext, confirmer: Confirmer) -> None:
    """Зарегистрировать курируемые инструменты всех модулей."""
    kit = Kit(app, confirmer)
    for module in MODULES:
        module.register(server, kit)
