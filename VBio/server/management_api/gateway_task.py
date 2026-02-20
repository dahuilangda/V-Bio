from __future__ import annotations

import time
from typing import Any, Optional, Tuple
from urllib.parse import quote

import requests
from flask import Response, jsonify, request
from management_api.runtime_proxy import RuntimeProxyBusyError


def forward_task_read(gateway: Any, task_id: str, upstream_prefix: str, action: str) -> Tuple[Response, int]:
    started = time.perf_counter()
    project_id: Optional[str] = None
    token = None

    try:
        project_id = gateway._read_project_id_from_query()
        token_plain = (request.headers.get("X-API-Token") or "").strip()
        token = gateway._authorize_project_read(project_id, token_plain)

        task_row = gateway.task_store.find_project_task(task_id, project_id)
        if not task_row:
            raise PermissionError("Task not found in this project")

        passthrough_query = {key: value for key, value in request.args.items() if key != "project_id"}
        upstream = gateway._proxy_get(f"{upstream_prefix}/{quote(task_id, safe='')}", passthrough_query)
        response, status_code = gateway._build_flask_response(upstream)
        succeeded = 200 <= status_code < 300

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
        return jsonify({"error": str(exc)}), 500


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

        task_row = gateway.task_store.find_project_task(task_id, project_id)
        if not task_row:
            raise PermissionError("Task not found in this project")

        passthrough_query = {
            key: value for key, value in request.args.items() if key not in {"project_id", "operation_mode"}
        }
        upstream = gateway._proxy_delete(f"/tasks/{quote(task_id, safe='')}", passthrough_query)
        response, status_code = gateway._build_flask_response(upstream)

        succeeded = 200 <= status_code < 300
        if succeeded:
            if require_delete:
                gateway.task_store.delete_task_row(task_row["id"])
            else:
                gateway.task_store.mark_task_cancelled(task_row["id"])

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
        return jsonify({"error": str(exc)}), 500
