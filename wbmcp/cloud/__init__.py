# Copyright (c) 2026 S-typy
#
# Облачный режим wb-mcp распространяется на условиях коммерческой лицензии,
# см. wbmcp/cloud/LICENSE. Ядро сервера лицензировано под GNU AGPL-3.0,
# см. LICENSE в корне репозитория.
"""Облачный многоарендный режим: несколько продавцов на одном сервере.

Что здесь есть: правила облака (какие токены допустимы, обязательный свой
User-Agent), реестр кабинетов с разделением по арендаторам (SQLite и Postgres)
и хранилище лимитера поверх Redis для нескольких экземпляров. Подключение кабинетов через OAuth 2.0
Каталога решений появится, когда WB одобрит заявку и откроет детали протокола.
"""

from wbmcp.cloud.policy import CloudPolicy, CloudPolicyError
from wbmcp.cloud.redis_store import RedisLimiterStore
from wbmcp.cloud.tenants import (
    PostgresTenantRegistry,
    PostgresTenantStore,
    TenantRegistry,
    TenantSellerStore,
)

__all__ = [
    "CloudPolicy",
    "CloudPolicyError",
    "PostgresTenantRegistry",
    "PostgresTenantStore",
    "RedisLimiterStore",
    "TenantRegistry",
    "TenantSellerStore",
]
