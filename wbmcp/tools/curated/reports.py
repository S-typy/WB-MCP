"""Асинхронные отчёты WB одним вызовом: задание, ожидание, скачивание."""

from __future__ import annotations

import uuid
from typing import Any

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from wbmcp.client.reports import ReportFailedError, ReportTimeoutError, wait_until
from wbmcp.tools.curated.kit import READ, Kit

TASK_REPORTS = {
    "warehouse_remains": "/api/v1/warehouse_remains",
    "paid_storage": "/api/v1/paid_storage",
    "acceptance_report": "/api/v1/acceptance_report",
}
DONE_STATUSES = {"done", "success", "succeeded", "ready"}
FAILED_STATUSES = {"error", "fail", "failed", "canceled", "cancelled"}


def _status_of(response: dict[str, Any]) -> str:
    data = response.get("data") or {}
    if isinstance(data, list):
        data = data[0] if data else {}
    return str(data.get("status") or "").lower()


def register(server: Any, kit: Kit) -> None:
    """Инструмент отчётов."""

    async def wb_report(
        kind: str,
        seller: str | None = None,
        params: dict[str, Any] | None = None,
        wait: bool = True,
        timeout_s: float = 600.0,
        poll_interval_s: float = 5.0,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Асинхронный отчёт целиком: создать задание, дождаться, скачать.

        kind: warehouse_remains, paid_storage, acceptance_report (params — параметры
        запроса, например dateFrom/dateTo) или nm_report (params — тело запроса
        POST /api/v2/nm-report/downloads; id можно не задавать). Файлы отчётов
        сохраняются на диск, путь в ответе.
        """
        params = dict(params or {})
        if kind in TASK_REPORTS:
            base = TASK_REPORTS[kind]
            created = await kit.call("GET", base, {"seller": seller, **params}, tool="wb_report",
                                 ctx=ctx)
            task_id = (created.get("data") or {}).get("data", {}).get("taskId")
            if not task_id:
                raise ToolError(f"WB не вернул taskId: {created.get('data')}")
            if not wait:
                return {"kind": kind, "task_id": task_id, "created": created}

            async def poll() -> dict[str, Any]:
                result = await kit.call("GET", base + "/tasks/{task_id}/status",
                                        {"seller": seller}, tool="wb_report", ctx=ctx,
                                        path_params={"task_id": task_id}, priority=1)
                return result.get("data") or {}

            try:
                status = await wait_until(
                    poll, done=lambda r: _status_of(r) in DONE_STATUSES,
                    failed=lambda r: _status_of(r) if _status_of(r) in FAILED_STATUSES else None,
                    interval_s=poll_interval_s, timeout_s=timeout_s,
                )
            except (ReportFailedError, ReportTimeoutError) as exc:
                raise ToolError(f"{exc} (task_id {task_id})") from exc
            download = await kit.call("GET", base + "/tasks/{task_id}/download",
                                      {"seller": seller}, tool="wb_report", ctx=ctx,
                                      path_params={"task_id": task_id})
            return {"kind": kind, "task_id": task_id, "status": status, "download": download}
        if kind == "nm_report":
            report_id = params.get("id") or str(uuid.uuid4())
            params["id"] = report_id
            created = await kit.call("POST", "/api/v2/nm-report/downloads",
                                 {"seller": seller, "body": params}, tool="wb_report", ctx=ctx)
            if not wait:
                return {"kind": kind, "download_id": report_id, "created": created}

            async def poll_list() -> dict[str, Any]:
                result = await kit.call("GET", "/api/v2/nm-report/downloads",
                                    {"seller": seller, "filter[downloadIds]": [report_id]},
                                    tool="wb_report", ctx=ctx, priority=1)
                return result.get("data") or {}

            try:
                status = await wait_until(
                    poll_list, done=lambda r: _status_of(r) in DONE_STATUSES,
                    failed=lambda r: _status_of(r) if _status_of(r) in FAILED_STATUSES else None,
                    interval_s=poll_interval_s, timeout_s=timeout_s,
                )
            except (ReportFailedError, ReportTimeoutError) as exc:
                raise ToolError(f"{exc} (download_id {report_id})") from exc
            download = await kit.call("GET", "/api/v2/nm-report/downloads/file/{downloadId}",
                                  {"seller": seller}, tool="wb_report", ctx=ctx,
                                  path_params={"downloadId": report_id})
            return {"kind": kind, "download_id": report_id, "status": status,
                    "download": download}
        raise ToolError(f"неизвестный вид отчёта {kind!r}; есть: "
                        f"{', '.join([*TASK_REPORTS, 'nm_report'])}")

    server.add_tool(wb_report, annotations=READ)
