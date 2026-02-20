from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

import requests
from flask import Response, jsonify, request

from management_api.runtime_proxy import RuntimeProxyBusyError
from management_api.task_snapshot import (
    build_affinity_task_snapshot,
    build_prediction_task_snapshot_from_yaml,
    read_seed,
    read_task_name,
    read_task_summary,
)


def _build_submit_snapshot(gateway: Any, upstream_path: str) -> Dict[str, Any]:
    if upstream_path == "/predict":
        return build_prediction_task_snapshot_from_yaml(request, gateway.logger)
    return build_affinity_task_snapshot(request, upstream_path)


def forward_submit(gateway: Any, upstream_path: str, action: str) -> Tuple[Response, int]:
    started = time.perf_counter()
    project_id: Optional[str] = None
    token = None

    try:
        project_id = gateway._read_project_id_from_form()
        token_plain = (request.headers.get("X-API-Token") or "").strip()
        token = gateway._authorize_submit(project_id, token_plain)
        backend = gateway._task_backend_label(upstream_path, request.form.get("backend", ""))
        extra_snapshot_payload = _build_submit_snapshot(gateway, upstream_path)

        upstream = gateway._proxy_multipart(upstream_path)
        response, status_code = gateway._build_flask_response(upstream)

        task_id: Optional[str] = None
        succeeded = 200 <= status_code < 300
        if succeeded:
            try:
                payload = upstream.json()
                task_id = str(payload.get("task_id") or "").strip() or None
            except Exception:  # noqa: BLE001
                task_id = None

            if task_id:
                gateway.task_store.insert_snapshot(
                    project_id=project_id,
                    task_id=task_id,
                    task_name=read_task_name(request, task_id),
                    task_summary=read_task_summary(request),
                    backend=backend,
                    seed=read_seed(
                        request,
                        backend=backend,
                        default_protenix_predict_seed=gateway.default_protenix_predict_seed,
                    ),
                    extra_payload=extra_snapshot_payload,
                )

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
            task_id=None,
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
            task_id=None,
        )
        return jsonify({"error": str(exc)}), 429
    except Exception as exc:  # noqa: BLE001
        gateway.logger.exception("Submit forward failed")
        gateway._record_usage(
            token,
            action=action,
            status_code=500,
            succeeded=False,
            started_at=started,
            project_id=project_id,
            task_id=None,
        )
        return jsonify({"error": str(exc)}), 500
