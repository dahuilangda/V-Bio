"""Project-scoped runtime index forward (F1 hardening).

`/tasks/runtime_index` on the runtime backend is GLOBAL (every worker's tasks across all
projects). The SPA only ever consumes the intersection with its own project's rows, so the
gateway serves a project-filtered view behind the caller's project-bound token — a project
token must not learn other tenants' task ids.

"""
from __future__ import annotations

import time
from typing import Any, Tuple

from flask import Response, request

from management_api.runtime_proxy import RuntimeProxyBusyError


def handle_tasks_runtime_index(gateway: Any) -> Tuple[Response, int]:
    """Runtime worker index filtered to the caller's project (token-authenticated)."""
    from flask import jsonify

    started = time.perf_counter()
    token = None
    try:
        token_plain = (request.headers.get("X-API-Token") or "").strip()
        raw_project_id = str(request.args.get("project_id") or "").strip() or None
        token = gateway._authorize_quick_project_action(token_plain, raw_project_id, require_submit=False)
        project_id = raw_project_id or token.project_id

        upstream = gateway._proxy_get("/tasks/runtime_index", {})
        if not (200 <= upstream.status_code < 300):
            response, status = gateway._build_flask_response(upstream)
            return response, status
        try:
            payload = upstream.json()
        except Exception:
            return jsonify({"error": "Runtime returned an invalid runtime index."}), 502

        if project_id:
            # Only ids this project owns may cross the gateway (upstream field names:
            # active_task_ids / reserved_task_ids / scheduled_task_ids).
            known_ids = set(gateway.task_store.list_project_task_ids(project_id))
            payload = {
                "active_task_ids": [tid for tid in _id_list(payload.get("active_task_ids")) if tid in known_ids],
                "reserved_task_ids": [tid for tid in _id_list(payload.get("reserved_task_ids")) if tid in known_ids],
                "scheduled_task_ids": [
                    tid for tid in _id_list(payload.get("scheduled_task_ids")) if tid in known_ids
                ],
            }
        # else: the trusted platform caller (shared runtime token, not project-bound) gets
        # the full index — the same view it had before the runtime moved behind the gateway.
        gateway._record_usage(
            None if token.is_platform else token,
            action="read_tasks_runtime_index",
            status_code=200,
            succeeded=True,
            started_at=started,
            project_id=project_id,
            task_id=None,
        )
        return jsonify(payload), 200
    except RuntimeProxyBusyError:
        return jsonify({"error": "Runtime is busy; retry shortly."}), 429
    except Exception:
        gateway.logger.exception("runtime index forward failed")
        return jsonify({"error": "Internal server error"}), 500


def _id_list(value: Any) -> list:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]
