"""HTTP-режим: приложение MCP плюс /health и /metrics."""

from __future__ import annotations

from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from wbmcp import __version__
from wbmcp.server import WbMcpServer


def build_http_app(server: WbMcpServer, *, host: str = "127.0.0.1") -> Starlette:
    """ASGI-приложение сервера с добавленными служебными маршрутами."""
    app = server.app
    app.remote = True
    asgi = server.streamable_http_app(host=host)

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({
            "status": "ok",
            "name": app.config.server.name,
            "version": __version__,
            "mode": app.config.server.mode,
            "sellers": len(await app.sellers.aliases()),
            "storage": app.storage,
            "limits": app.limits,
            "operations": len(app.catalog),
            "snapshot": app.catalog.snapshot,
            "generated_tools": len(server.generated),
        })

    async def metrics(request: Request) -> PlainTextResponse:
        return PlainTextResponse(app.metrics.render(),
                                 media_type="text/plain; version=0.0.4; charset=utf-8")

    asgi.router.routes.insert(0, Route("/health", health, methods=["GET"]))
    asgi.router.routes.insert(0, Route("/metrics", metrics, methods=["GET"]))
    return asgi


async def serve_http(server: WbMcpServer, *, host: str, port: int,
                     log_level: str = "info") -> Any:  # pragma: no cover - сетевой запуск
    """Поднять uvicorn с приложением сервера."""
    import uvicorn

    config = uvicorn.Config(build_http_app(server, host=host), host=host, port=port,
                            log_level=log_level)
    await uvicorn.Server(config).serve()
