from __future__ import annotations

import time
from typing import Any, Optional, Tuple

import requests
from flask import Response, jsonify, request
from management_api.runtime_proxy import RuntimeProxyBusyError


def forward_quick_json(
    gateway: Any,
    upstream_path: str,
    action: str,
    *,
    require_submit: bool = False,
) -> Tuple[Response, int]:
    started = time.perf_counter()
    token = None
    project_id: Optional[str] = None

    try:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            payload = {}
        raw_project_id = str(payload.get("project_id") or request.args.get("project_id") or "").strip()
        project_id = raw_project_id or None
        token_plain = (request.headers.get("X-API-Token") or "").strip()
        token = gateway._authorize_quick_project_action(token_plain, project_id, require_submit=require_submit)
        effective_project_id = project_id or token.project_id

        upstream_payload = dict(payload)
        upstream_payload.pop("project_id", None)

        upstream = gateway._proxy_post_json(upstream_path, upstream_payload)
        response, status_code = gateway._build_flask_response(upstream)
        succeeded = 200 <= status_code < 300

        gateway._record_usage(
            token,
            action=action,
            status_code=status_code,
            succeeded=succeeded,
            started_at=started,
            project_id=effective_project_id,
            task_id=None,
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
            project_id=project_id or (token.project_id if token else None),
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
            project_id=project_id or (token.project_id if token else None),
            task_id=None,
        )
        return jsonify({"error": str(exc)}), 429
    except Exception as exc:  # noqa: BLE001
        gateway.logger.exception("Quick JSON forward failed")
        gateway._record_usage(
            token,
            action=action,
            status_code=500,
            succeeded=False,
            started_at=started,
            project_id=project_id or (token.project_id if token else None),
            task_id=None,
        )
        return jsonify({"error": str(exc)}), 500


def forward_quick_multipart(
    gateway: Any,
    upstream_path: str,
    action: str,
    *,
    require_submit: bool = False,
) -> Tuple[Response, int]:
    started = time.perf_counter()
    token = None
    project_id: Optional[str] = None

    try:
        raw_project_id = str(request.form.get("project_id") or request.args.get("project_id") or "").strip()
        project_id = raw_project_id or None
        token_plain = (request.headers.get("X-API-Token") or "").strip()
        token = gateway._authorize_quick_project_action(token_plain, project_id, require_submit=require_submit)
        effective_project_id = project_id or token.project_id

        upstream = gateway._proxy_multipart(upstream_path)
        response, status_code = gateway._build_flask_response(upstream)
        succeeded = 200 <= status_code < 300

        gateway._record_usage(
            token,
            action=action,
            status_code=status_code,
            succeeded=succeeded,
            started_at=started,
            project_id=effective_project_id,
            task_id=None,
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
            project_id=project_id or (token.project_id if token else None),
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
            project_id=project_id or (token.project_id if token else None),
            task_id=None,
        )
        return jsonify({"error": str(exc)}), 429
    except Exception as exc:  # noqa: BLE001
        gateway.logger.exception("Quick multipart forward failed")
        gateway._record_usage(
            token,
            action=action,
            status_code=500,
            succeeded=False,
            started_at=started,
            project_id=project_id or (token.project_id if token else None),
            task_id=None,
        )
        return jsonify({"error": str(exc)}), 500


def forward_quick_get(gateway: Any, upstream_path: str, action: str) -> Tuple[Response, int]:
    started = time.perf_counter()
    token = None
    project_id: Optional[str] = None

    try:
        raw_project_id = str(request.args.get("project_id") or "").strip()
        project_id = raw_project_id or None
        token_plain = (request.headers.get("X-API-Token") or "").strip()
        token = gateway._authorize_quick_project_action(token_plain, project_id, require_submit=False)
        effective_project_id = project_id or token.project_id

        passthrough_query = {key: value for key, value in request.args.items() if key != "project_id"}
        upstream = gateway._proxy_get(upstream_path, passthrough_query)
        response, status_code = gateway._build_flask_response(upstream)
        succeeded = 200 <= status_code < 300

        gateway._record_usage(
            token,
            action=action,
            status_code=status_code,
            succeeded=succeeded,
            started_at=started,
            project_id=effective_project_id,
            task_id=None,
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
            project_id=project_id or (token.project_id if token else None),
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
            project_id=project_id or (token.project_id if token else None),
            task_id=None,
        )
        return jsonify({"error": str(exc)}), 429
    except Exception as exc:  # noqa: BLE001
        gateway.logger.exception("Quick GET forward failed")
        gateway._record_usage(
            token,
            action=action,
            status_code=500,
            succeeded=False,
            started_at=started,
            project_id=project_id or (token.project_id if token else None),
            task_id=None,
        )
        return jsonify({"error": str(exc)}), 500
