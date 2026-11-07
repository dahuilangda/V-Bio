from __future__ import annotations

import itertools
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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

# A single bounded thread pool for snapshot persistence — replaces the per-submit daemon thread
# that could spawn unboundedly under load and was silently dropped on process exit. The pool is
# shared across all submit calls. 信号量限制 executor 内部队列长度，PostgREST 变慢时
# 丢弃快照（仅展示回填，任务行仍是权威数据），不无限积压。
_snapshot_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="vbio-snapshot")
_SNAPSHOT_QUEUE_MAX_ENTRIES = 512
_snapshot_queue_slots = threading.BoundedSemaphore(_SNAPSHOT_QUEUE_MAX_ENTRIES)
_snapshot_dropped = itertools.count()


def _persist_submit_snapshot_bounded(**snapshot_kwargs: Any) -> None:
    try:
        _persist_submit_snapshot_async(**snapshot_kwargs)
    finally:
        _snapshot_queue_slots.release()


def _build_submit_snapshot(gateway: Any, upstream_path: str) -> Dict[str, Any]:
    if upstream_path == "/predict":
        return build_prediction_task_snapshot_from_yaml(request, gateway.logger)
    return build_affinity_task_snapshot(request, upstream_path)


def _persist_submit_snapshot_async(
    gateway: Any,
    *,
    project_id: str,
    task_id: str,
    task_name: str,
    task_summary: str,
    backend: str,
    seed: Optional[int],
    extra_snapshot_payload: Dict[str, Any],
) -> None:
    try:
        gateway.task_store.insert_snapshot(
            project_id=project_id,
            task_id=task_id,
            task_name=task_name,
            task_summary=task_summary,
            backend=backend,
            seed=seed,
            extra_payload=extra_snapshot_payload,
        )
    except Exception as exc:  # noqa: BLE001
        gateway.logger.warning(
            "Failed to persist API submit snapshot for task %s in project %s: %s",
            task_id,
            project_id,
            exc,
        )


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
                task_name = read_task_name(request, task_id)
                task_summary = read_task_summary(request)
                seed = read_seed(
                    request,
                    backend=backend,
                    default_protenix_predict_seed=gateway.default_protenix_predict_seed,
                )
                gateway.task_store.remember_task_alias(project_id, task_id)
                snapshot_kwargs = {
                    "gateway": gateway,
                    "project_id": project_id,
                    "task_id": task_id,
                    "task_name": task_name,
                    "task_summary": task_summary,
                    "backend": backend,
                    "seed": seed,
                    "extra_snapshot_payload": extra_snapshot_payload,
                }
                try:
                    if _snapshot_queue_slots.acquire(blocking=False):
                        try:
                            _snapshot_executor.submit(_persist_submit_snapshot_bounded, **snapshot_kwargs)
                        except RuntimeError:
                            # 已停机（进程退出）——退回同步写。
                            _snapshot_queue_slots.release()
                            _persist_submit_snapshot_async(**snapshot_kwargs)
                    else:
                        dropped = next(_snapshot_dropped) + 1
                        if dropped == 1 or dropped % 200 == 0:
                            gateway.logger.warning(
                                "Snapshot queue saturated; dropped %d snapshot(s) so far", dropped
                            )
                except Exception:  # noqa: BLE001 — 快照失败不得影响提交
                    gateway.logger.exception("Submit snapshot persistence failed")

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
        return jsonify({"error": "Internal server error"}), 500
