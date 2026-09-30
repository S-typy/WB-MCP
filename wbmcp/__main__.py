"""Запуск сервера: stdio по умолчанию, streamable HTTP с bearer-ключами клиентов."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from wbmcp import __version__
from wbmcp.config import ConfigError, load_config


def build_parser() -> argparse.ArgumentParser:
    """Аргументы командной строки."""
    parser = argparse.ArgumentParser(prog="wb-mcp",
                                     description="MCP-сервер для API продавца Wildberries")
    parser.add_argument("--config", type=Path, help="файл настроек TOML (или WBMCP_CONFIG)")
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--host", help="адрес для HTTP-режима")
    parser.add_argument("--port", type=int, help="порт для HTTP-режима")
    parser.add_argument("--profiles",
                        help="профили инструментов через запятую, например orders,content")
    parser.add_argument("--specs", type=Path, help="каталог снимка спецификаций")
    parser.add_argument("--read-only", action="store_true", help="запретить все записи")
    parser.add_argument("--yes", action="store_true",
                        help="не спрашивать подтверждение опасных действий")
    parser.add_argument("--insecure", action="store_true",
                        help="разрешить HTTP без ключей клиентов (только для отладки)")
    parser.add_argument("--check", action="store_true",
                        help="собрать приложение, напечатать сводку и выйти")
    parser.add_argument("--smoke", action="store_true",
                        help="пробный прогон: кабинеты, ping, seller-info, новости, квота")
    parser.add_argument("--seller", help="алиас кабинета для --smoke")
    parser.add_argument("--version", action="version", version=f"wb-mcp {__version__}")
    return parser


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None,
         transport: Any = None) -> int:
    """Точка входа консольной команды; transport подменяется в тестах."""
    env = os.environ if env is None else env
    args = build_parser().parse_args(argv)
    import logging

    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        config = load_config(args.config, env)
    except ConfigError as exc:
        print(f"ошибка настроек: {exc}", file=sys.stderr)
        return 2
    server_cfg = config.server
    if args.profiles:
        server_cfg.profiles = [p.strip() for p in args.profiles.split(",") if p.strip()]
    if args.specs:
        server_cfg.specs_dir = args.specs
    if args.read_only:
        server_cfg.read_only = True
    if args.host:
        server_cfg.http_host = args.host
    if args.port:
        server_cfg.http_port = args.port
    import asyncio

    return asyncio.run(_run(args, config, env, transport))


async def _run(args: argparse.Namespace, config: Any, env: Mapping[str, str],
               transport: Any) -> int:
    """Собрать приложение и выполнить выбранный режим."""
    from wbmcp.app import build_app
    from wbmcp.server import WbMcpServer
    from wbmcp.tools.confirm import AutoConfirmer, ElicitConfirmer

    server_cfg = config.server
    try:
        app = await build_app(config, env=env, transport=transport)
    except Exception as exc:  # noqa: BLE001 - граница CLI: любая причина печатается человеку
        print(f"не удалось собрать сервер: {exc}", file=sys.stderr)
        return 2
    confirmer = AutoConfirmer(True) if args.yes else ElicitConfirmer()
    try:
        if args.transport == "http" and app.clients is None and not args.insecure:
            print("HTTP-режим без ключей клиентов запрещён: добавьте [[clients]] в настройки "
                  "или --insecure", file=sys.stderr)
            return 2
        if args.smoke:
            from wbmcp.smoke import run_smoke

            return await run_smoke(app, args.seller)
        if args.check:
            server = WbMcpServer(app, confirmer=confirmer)
            print(f"wb-mcp {__version__}: кабинетов {len(await app.sellers.aliases())}, "
                  f"операций {len(app.catalog)} (снимок {app.catalog.snapshot}), "
                  f"сгенерированных инструментов {len(server.generated)} "
                  f"по профилям {','.join(server_cfg.profiles)}, хранилище {app.storage}, "
                  f"лимитер {app.limits}, кэш {app.cache.backend}, "
                  f"данные в {app.data_dir}")
            return 0
        if args.transport == "http":  # pragma: no cover - сетевой запуск
            from mcp.server.auth.settings import AuthSettings

            from wbmcp.auth import StaticTokenVerifier
            from wbmcp.http import serve_http

            base = f"http://{server_cfg.http_host}:{server_cfg.http_port}"
            verifier = StaticTokenVerifier(app.clients) if app.clients else None
            auth = None
            if verifier:
                auth = AuthSettings(issuer_url=base, resource_server_url=f"{base}/mcp",
                                    validate_token_resource=False)
            server = WbMcpServer(app, confirmer=confirmer, token_verifier=verifier, auth=auth)
            await serve_http(server, host=server_cfg.http_host, port=server_cfg.http_port)
            return 0
        server = WbMcpServer(app, confirmer=confirmer)  # pragma: no cover - блокирующий запуск
        await server.run_stdio_async()  # pragma: no cover
        return 0  # pragma: no cover
    finally:
        if not args.smoke:
            await app.aclose()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
