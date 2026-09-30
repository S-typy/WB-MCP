"""Профили: какие разделы каталога выдавать агенту сгенерированными инструментами."""

from __future__ import annotations

ALL_SECTIONS = (
    "analytics",
    "api-information",
    "customer-communication",
    "dbs",
    "documents-and-accounting",
    "in-store-pickup",
    "item-management",
    "orders-dbw",
    "orders-fbs",
    "orders-fbw",
    "promotion",
    "rates",
    "reports",
)

PROFILES: dict[str, tuple[str, ...]] = {
    "core": (),
    "orders": ("orders-fbs", "orders-dbw", "dbs", "in-store-pickup", "orders-fbw"),
    "content": ("item-management",),
    "promo": ("promotion",),
    "analytics": ("analytics", "reports"),
    "communication": ("customer-communication",),
    "finance": ("documents-and-accounting",),
    "common": ("api-information", "rates"),
    "full": ALL_SECTIONS,
}


class UnknownProfileError(ValueError):
    """Такого профиля нет."""


def sections_for_profiles(names: list[str] | tuple[str, ...]) -> frozenset[str]:
    """Объединение разделов выбранных профилей."""
    sections: set[str] = set()
    for name in names:
        if name not in PROFILES:
            raise UnknownProfileError(f"профиль {name!r} неизвестен; есть: {', '.join(PROFILES)}")
        sections.update(PROFILES[name])
    return frozenset(sections)
