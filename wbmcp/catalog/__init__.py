"""Каталог операций WB API, собранный из снимков официальных спецификаций."""

from wbmcp.catalog.index import Catalog, UnknownOperationError
from wbmcp.catalog.models import LimitRow, LimitRule, Operation, Parameter
from wbmcp.catalog.profiles import PROFILES, sections_for_profiles

__all__ = [
    "PROFILES",
    "Catalog",
    "LimitRow",
    "LimitRule",
    "Operation",
    "Parameter",
    "UnknownOperationError",
    "sections_for_profiles",
]
