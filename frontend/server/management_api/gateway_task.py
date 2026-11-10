from __future__ import annotations

import time
from typing import Any, Dict, Iterator, Optional, Tuple
from urllib.parse import quote

import requests
from flask import Response, jsonify, request, stream_with_context
from management_api.runtime_proxy import RuntimeProxyBusyError
from management_api.rfd4_consistency import maybe_enrich_view
from management_api.task_status_stream import task_status_sse_frames


def forward_task_read(
    gateway: Any,
    task_id: str,
    upstream_prefix: str,
    action: str,
    *,
    upstream_suffix: str = "",
) -> Tuple[Response, int]:
    started = time.perf_counter()
    project_id: Optional[str] = None
    token = None

    try:
        project_id = gateway._read_project_id_from_query()
        token_plain = (request.headers.get("X-API-Token") or "").strip()
        token = gateway._authorize_project_read(project_id, token_plain)

        if not token.is_platform:
            task_row = gateway.task_store.find_project_task(task_id, project_id)
            if not task_row:
                raise PermissionError("Task not found in this project")

        passthrough_query = {key: value for key, value in request.args.items() if key != "project_id"}
        upstream = gateway._proxy_get(f"{upstream_prefix}/{quote(task_id, safe='')}{upstream_suffix}", passthrough_query)
        status_code = upstream.status_code
        if action == "read_results_view" and 200 <= status_code < 300 and project_id:
            enriched = maybe_enrich_view(gateway, task_id, project_id, upstream)
            if enriched is not None:
                response = Response(enriched, status=status_code, content_type="application/zip")
                gateway._record_usage(
                    token,
                    action=action,
                    status_code=status_code,
                    succeeded=True,
                    started_at=started,
                    project_id=project_id,
                    task_id=task_id,
                )
                return response, status_code
        response, status_code = gateway._build_flask_response(upstream)
        succeeded = 200 <= status_code < 400

        gateway._record_usage(
            token,
            action=action,
            status_code=status_code,
            succeeded=succeeded,
            started_at=started,
            project_id=project_id,
            task_id=task_id,
        )
        return response, status_code
    except PermissionError as exc:
        return gateway._forbidden(str(exc), token, action, started, project_id)
    except requests.Timeout:
        gateway._record_usage(
            token,
            action=action,
            status_code=504,
            succeeded=False,
            started_at=started,
            project_id=project_id,
            task_id=task_id,
        )
        return jsonify({"error": "Upstream runtime request timed out"}), 504
    except RuntimeProxyBusyError as exc:
        gateway._record_usage(
            token,
            action=action,
            status_code=429,
            succeeded=False,
            started_at=started,
            project_id=project_id,
            task_id=task_id,
        )
        return jsonify({"error": str(exc)}), 429
    except Exception as exc:  # noqa: BLE001
        gateway.logger.exception("Task read forward failed")
        gateway._record_usage(
            token,
            action=action,
            status_code=500,
            succeeded=False,
            started_at=started,
            project_id=project_id,
            task_id=task_id,
        )
        return jsonify({"error": "Internal server error"}), 500


def forward_task_status_stubs(gateway: Any) -> Tuple[Response, int]:
    """增量轮询第一阶段：每任务的轻量状态指纹（无 reconcile、无 usage 放大）。"""
    started = time.perf_counter()
    project_id: Optional[str] = None
    token = None

    try:
        project_id = gateway._read_project_id_from_query()
        token_plain = (request.headers.get("X-API-Token") or "").strip()
        token = gateway._authorize_project_read(project_id, token_plain)

        payload = request.get_json(silent=True) or {}
        task_ids_input = payload.get("task_ids")
        if not isinstance(task_ids_input, list):
            raise PermissionError("task_ids must be a list")
        task_ids = list(dict.fromkeys(str(item or "").strip() for item in task_ids_input if str(item or "").strip()))
        if not task_ids:
            return jsonify({"stubs": []}), 200

        if not token.is_platform:
            found_by_task_id = gateway.task_store.find_project_tasks(task_ids, project_id)
            missing = [task_id for task_id in task_ids if task_id not in found_by_task_id]
            if missing:
                raise PermissionError("One or more tasks were not found in this project")

        upstream = gateway._proxy_post_json("/status/stubs", {"task_ids": task_ids})
        response, status_code = gateway._build_flask_response(upstream)
        succeeded = 200 <= status_code < 400
        gateway._record_usage(
            token,
            action="read_status_stubs",
            status_code=status_code,
            succeeded=succeeded,
            started_at=started,
            project_id=project_id,
            task_id=None,
        )
        return response, status_code
    except PermissionError as exc:
        return gateway._forbidden(str(exc), token, "read_status_stubs", started, project_id)
    except requests.Timeout:
        gateway._record_usage(
            token,
            action="read_status_stubs",
            status_code=504,
            succeeded=False,
            started_at=started,
            project_id=project_id,
            task_id=None,
        )
        return jsonify({"error": "Upstream runtime timed out."}), 504


def forward_task_status_stream(gateway: Any) -> Tuple[Response, int]:
    """任务状态 SSE：订阅本项目终态变更，changed 帧 + 心跳 + 周期重连。"""
    started = time.perf_counter()
    project_id: Optional[str] = None
    token = None

    try:
        project_id = gateway._read_project_id_from_query()
        token_plain = (request.headers.get("X-API-Token") or "").strip()
        token = gateway._authorize_project_read(project_id, token_plain)
    except PermissionError as exc:
        return gateway._forbidden(str(exc), token, "read_status_stream", started, project_id)

    hub = gateway.task_status_hub
    if hub is None:
        return jsonify({"error": "Task status stream is not available."}), 503

    subscription = hub.subscribe(project_id)
    gateway.logger.info("Task status stream opened (project %s).", project_id)

    from flask import stream_with_context

    def generate() -> Iterator[str]:
        try:
            yield ": stream opened\n\n"
            for frame in task_status_sse_frames(subscription):
                yield frame
        finally:
            gateway.logger.info("Task status stream closed (project %s).", project_id)

    response = Response(stream_with_context(generate()), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    gateway._record_usage(
        token,
        action="read_status_stream",
        status_code=200,
        succeeded=True,
        started_at=started,
        project_id=project_id,
        task_id=None,
    )
    return response


def forward_task_status_batch(gateway: Any) -> Tuple[Response, int]:
    started = time.perf_counter()
    project_id: Optional[str] = None
    token = None

    try:
        project_id = gateway._read_project_id_from_query()
        token_plain = (request.headers.get("X-API-Token") or "").strip()
        token = gateway._authorize_project_read(project_id, token_plain)

        payload = request.get_json(silent=True) or {}
        task_ids_input = payload.get("task_ids")
        if not isinstance(task_ids_input, list):
            raise PermissionError("task_ids must be a list")
        task_ids = list(dict.fromkeys(str(item or "").strip() for item in task_ids_input if str(item or "").strip()))
        if not task_ids:
            return jsonify({"statuses": []}), 200

        if not token.is_platform:
            found_by_task_id: Dict[str, Dict[str, Any]] = gateway.task_store.find_project_tasks(task_ids, project_id)
            missing = [task_id for task_id in task_ids if task_id not in found_by_task_id]
            if missing:
                raise PermissionError("One or more tasks were not found in this project")

        upstream = gateway._proxy_post_json("/status/batch", {"task_ids": task_ids})
        response, status_code = gateway._build_flask_response(upstream)
        succeeded = 200 <= status_code < 400
        if succeeded:
            # 服务端对账: 运行时已终态而行未推进的(页面没开着/经 API 直提),
            # 在任意一次状态轮询时自愈。仅处理明确终态, 失败不影响读取。
            try:
                terminal = {
                    str(entry.get("task_id") or ""): str(entry.get("state") or "")
                    for entry in (upstream.json() or {}).get("statuses", [])
                    if entry.get("state") in ("SUCCESS", "FAILURE", "REVOKED")
                    and entry.get("task_id")
                }
                if terminal:
                    gateway.task_store.reconcile_terminal_rows(project_id, terminal)
                    hub = getattr(gateway, "task_status_hub", None)
                    if hub is not None:
                        try:
                            hub.publish(
                                project_id,
                                [{"task_id": task_id, "state": state} for task_id, state in terminal.items()],
                            )
                        except Exception:
                            gateway.logger.debug("task status publish failed", exc_info=True)
            except Exception:
                gateway.logger.debug("task state reconcile failed", exc_info=True)
        gateway._record_usage(
            token,
            action="read_status_batch",
            status_code=status_code,
            succeeded=succeeded,
            started_at=started,
            project_id=project_id,
            task_id=None,
        )
        return response, status_code
    except PermissionError as exc:
        return gateway._forbidden(str(exc), token, "read_status_batch", started, project_id)
    except requests.Timeout:
        gateway._record_usage(
            token,
            action="read_status_batch",
            status_code=504,
            succeeded=False,
            started_at=started,
            project_id=project_id,
            task_id=None,
        )
        return jsonify({"error": "Upstream runtime request timed out"}), 504
    except RuntimeProxyBusyError as exc:
        gateway._record_usage(
            token,
            action="read_status_batch",
            status_code=429,
            succeeded=False,
            started_at=started,
            project_id=project_id,
            task_id=None,
        )
        return jsonify({"error": str(exc)}), 429
    except Exception as exc:  # noqa: BLE001
        gateway.logger.exception("Task batch read forward failed")
        gateway._record_usage(
            token,
            action="read_status_batch",
            status_code=500,
            succeeded=False,
            started_at=started,
            project_id=project_id,
            task_id=None,
        )
        return jsonify({"error": "Internal server error"}), 500


def cancel_or_delete_task(gateway: Any, task_id: str) -> Tuple[Response, int]:
    started = time.perf_counter()
    project_id: Optional[str] = None
    token = None

    try:
        project_id = gateway._read_project_id_from_query()
        mode = (request.args.get("operation_mode") or "cancel").strip().lower()
        require_delete = mode == "delete"

        token_plain = (request.headers.get("X-API-Token") or "").strip()
        token = gateway._authorize_task_action(project_id, token_plain, require_delete=require_delete)

        # Project tokens always carry a non-empty project_id; the platform token without one
        # mirrors forward_task_read and skips the project-scoped lookup (the runtime task row
        # is the authority) — previously this path 403'd "Task not found in this project".
        task_row = None
        if project_id:
            task_row = gateway.task_store.find_project_task(task_id, project_id)
            if not task_row:
                raise PermissionError("Task not found in this project")

        passthrough_query = {
            key: value for key, value in request.args.items() if key not in {"project_id", "operation_mode"}
        }
        upstream = gateway._proxy_delete(f"/tasks/{quote(task_id, safe='')}", passthrough_query)
        response, status_code = gateway._build_flask_response(upstream)

        succeeded = 200 <= status_code < 300
        if succeeded and task_row is not None:
            # The upstream action already happened — a transient store failure here must not
            # report 500 (the client would retry a cancel that succeeded) or leave the row
            # stuck at its old state silently; log it and still return the upstream outcome.
            try:
                if require_delete:
                    gateway.task_store.delete_task_row(task_row["id"])
                else:
                    gateway.task_store.mark_task_cancelled(task_row["id"])
            except Exception as exc:  # noqa: BLE001
                gateway.logger.warning(
                    "Upstream %s for task %s succeeded but the local task row update failed: %s",
                    "delete" if require_delete else "cancel",
                    task_id,
                    exc,
                )

        gateway._record_usage(
            token,
            action="delete_task" if require_delete else "cancel_task",
            status_code=status_code,
            succeeded=succeeded,
            started_at=started,
            project_id=project_id,
            task_id=task_id,
        )
        return response, status_code
    except PermissionError as exc:
        action = "delete_task" if (request.args.get("operation_mode") or "").strip().lower() == "delete" else "cancel_task"
        return gateway._forbidden(str(exc), token, action, started, project_id)
    except requests.Timeout:
        gateway._record_usage(
            token,
            action="cancel_or_delete_task",
            status_code=504,
            succeeded=False,
            started_at=started,
            project_id=project_id,
            task_id=task_id,
        )
        return jsonify({"error": "Upstream runtime request timed out"}), 504
    except RuntimeProxyBusyError as exc:
        gateway._record_usage(
            token,
            action="cancel_or_delete_task",
            status_code=429,
            succeeded=False,
            started_at=started,
            project_id=project_id,
            task_id=task_id,
        )
        return jsonify({"error": str(exc)}), 429
    except Exception as exc:  # noqa: BLE001
        gateway.logger.exception("Cancel/Delete forward failed")
        gateway._record_usage(
            token,
            action="cancel_or_delete_task",
            status_code=500,
            succeeded=False,
            started_at=started,
            project_id=project_id,
            task_id=task_id,
        )
        return jsonify({"error": "Internal server error"}), 500
