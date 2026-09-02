"""Routes for asynchronous task-list Excel exports.

POST   /api/export/tasks_excel                  -> dispatch Celery export job (202 + export_id)
GET    /api/export/tasks_excel/<id>/status      -> {status, total, done, error, file_name, file_ready}
GET    /api/export/tasks_excel/<id>/download    -> the finished .xlsx (streamed from disk)

The export job itself runs on the CPU worker via the `cap.export.default`
queue so large exports never block API requests or the browser.
"""
from __future__ import annotations

import os
import re
import uuid
from typing import Any, Dict

from flask import jsonify, request, send_file
from werkzeug.utils import secure_filename

from backend.services.export_job_store import ExportJobStore

_EXPORT_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")

XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _is_valid_export_id(export_id: str) -> bool:
    return bool(_EXPORT_ID_PATTERN.match(str(export_id or "").strip().lower()))


def register_export_routes(
    app,
    *,
    require_api_token,
    celery_app,
    logger,
    config_module,
    export_tasks_excel_task,
    select_queue_for_capability,
    get_redis_client_fn,
) -> None:
    export_store = ExportJobStore(
        get_redis_client_fn=get_redis_client_fn,
        logger=logger,
        ttl_seconds=int(getattr(config_module, "EXPORT_JOB_TTL_SECONDS", 48 * 3600)),
    )

    @app.route("/api/export/tasks_excel", methods=["POST"])
    @require_api_token
    def create_tasks_excel_export():
        max_request_bytes = int(getattr(config_module, "EXPORT_REQUEST_MAX_BYTES", 64 * 1024 * 1024))
        if request.content_length and request.content_length > max_request_bytes:
            return jsonify({
                "error": (
                    f"Export request body exceeds the limit ({max_request_bytes} bytes). "
                    "Narrow the filters or export fewer tasks."
                )
            }), 413
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Request body must be a JSON object."}), 400
        raw_tasks = payload.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            return jsonify({"error": "'tasks' must be a non-empty list."}), 400
        max_rows = int(getattr(config_module, "EXPORT_MAX_TASK_ROWS", 50000))
        if len(raw_tasks) > max_rows:
            return jsonify({"error": f"Too many task rows ({len(raw_tasks)} > {max_rows})."}), 400

        queue_selection = select_queue_for_capability("export", "default")
        if not bool(queue_selection.get("online", False)):
            return jsonify({
                "error": "No online workers available for the export capability.",
                "capability": "export",
                "queue_selection": queue_selection,
            }), 503
        target_queue = str(queue_selection.get("queue") or "").strip()
        if not target_queue:
            return jsonify({"error": "Resolved queue is empty for the export capability."}), 500

        export_id = uuid.uuid4().hex
        # Truncate once: the job record AND the broker message must both carry
        # the bounded name (an unbounded project_name must not reach the worker).
        project_name = str(payload.get("project_name") or "Tasks").strip()[:120] or "Tasks"
        dispatch_payload = {
            "export_id": export_id,
            "project_name": project_name,
            "tasks": raw_tasks,
        }
        # Create the job record BEFORE dispatching so a fast worker can never
        # update a record that does not exist yet (its updates would be lost).
        export_store.create(
            export_id=export_id,
            celery_task_id="",
            project_name=project_name,
            total=len(raw_tasks),
            queue=target_queue,
        )
        try:
            async_result = export_tasks_excel_task.apply_async(
                args=[dispatch_payload],
                queue=target_queue,
            )
        except Exception as exc:
            export_store.update(export_id, status="failure", error=f"Failed to dispatch: {exc}"[:2000])
            logger.exception("Failed to dispatch Excel export task: %s", exc)
            return jsonify({"error": "Failed to dispatch Excel export task."}), 500
        export_store.update(export_id, celery_task_id=async_result.id)
        logger.info(
            "Dispatched Excel export %s (%d rows) to queue %s (celery task %s).",
            export_id,
            len(raw_tasks),
            target_queue,
            async_result.id,
        )
        return jsonify({
            "export_id": export_id,
            "celery_task_id": async_result.id,
            "queue": target_queue,
            "total": len(raw_tasks),
        }), 202

    def _export_file_path(export_id: str) -> str:
        return os.path.join(config_module.EXPORTS_BASE_DIR, f"{export_id}.xlsx")

    def _resolve_job_status(job: Dict[str, Any], export_id: str) -> Dict[str, Any]:
        """Store status with Celery-state reconciliation.

        FAILURE/REVOKED surface a dead export instead of endless polling; SUCCESS
        plus an on-disk file recovers a finished export whose final Redis write
        was lost (Redis blip during a long-running export).
        """
        job = dict(job)
        status = str(job.get("status") or "queued")
        if status in {"queued", "running"}:
            from celery.result import AsyncResult

            celery_result = None
            try:
                celery_result = AsyncResult(str(job.get("celery_task_id")), app=celery_app)
                celery_state = str(celery_result.state)
            except Exception as exc:
                logger.debug("Failed to query Celery state for export %s: %s", export_id, exc)
                celery_state = ""
            if celery_state in {"FAILURE", "REVOKED"}:
                status = "failure"
            elif celery_state == "SUCCESS":
                candidate_path = _export_file_path(export_id)
                if os.path.isfile(candidate_path):
                    status = "success"
                    if not str(job.get("file_name") or ""):
                        result_info = getattr(celery_result, "result", None) if celery_result is not None else None
                        job["file_name"] = (
                            str(result_info.get("file_name"))
                            if isinstance(result_info, dict) and result_info.get("file_name")
                            else f"{export_id}.xlsx"
                        )
                    if not int(job.get("done") or 0):
                        job["done"] = job.get("total") or 0
        job["status"] = status
        return job

    @app.route("/api/export/tasks_excel/<export_id>/status", methods=["GET"])
    @require_api_token
    def get_tasks_excel_export_status(export_id):
        if not _is_valid_export_id(export_id):
            return jsonify({"error": "Invalid export id."}), 400
        job = export_store.load(export_id)
        if job is None:
            return jsonify({"error": "Export job not found or expired."}), 404
        job = _resolve_job_status(job, export_id)

        file_name = str(job.get("file_name") or "")
        file_ready = bool(
            job.get("status") == "success"
            and file_name
            and os.path.isfile(_export_file_path(export_id))
        )
        return jsonify({
            "export_id": export_id,
            "status": job.get("status"),
            "total": int(job.get("total") or 0),
            "done": int(job.get("done") or 0),
            "file_name": file_name,
            "file_bytes": int(job.get("file_bytes") or 0),
            "file_ready": file_ready,
            "warning": str(job.get("warning") or ""),
            "error": str(job.get("error") or ""),
        })

    @app.route("/api/export/tasks_excel/<export_id>/cancel", methods=["POST"])
    @require_api_token
    def cancel_tasks_excel_export(export_id):
        if not _is_valid_export_id(export_id):
            return jsonify({"error": "Invalid export id."}), 400
        job = export_store.load(export_id)
        if job is None:
            return jsonify({"error": "Export job not found or expired."}), 404
        status = str(job.get("status") or "queued")
        if status in {"success", "failure"}:
            # Terminal already — idempotent no-op.
            return jsonify({"export_id": export_id, "status": status, "cancelled": False}), 200

        # Real cancellation: revoke (and terminate) the Celery task so the CPU
        # worker stops building instead of burning the slot to completion.
        celery_task_id = str(job.get("celery_task_id") or "").strip()
        if celery_task_id:
            try:
                celery_app.control.revoke(celery_task_id, terminate=True)
            except Exception as exc:
                logger.warning("Revoke failed for export %s (task %s): %s", export_id, celery_task_id, exc)

        export_store.update(export_id, status="failure", error="Cancelled by user.")
        for suffix in ("", ".tmp"):
            try:
                path = f"{_export_file_path(export_id)}{suffix}"
                if os.path.isfile(path):
                    os.remove(path)
            except Exception as exc:
                logger.debug("Cleanup after cancel skipped for %s: %s", export_id, exc)
        logger.info("Excel export %s cancelled by client.", export_id)
        return jsonify({"export_id": export_id, "status": "failure", "cancelled": True}), 200

    @app.route("/api/export/tasks_excel/<export_id>/download", methods=["GET"])
    @require_api_token
    def download_tasks_excel_export(export_id):
        if not _is_valid_export_id(export_id):
            return jsonify({"error": "Invalid export id."}), 400
        job = export_store.load(export_id)
        if job is None:
            return jsonify({"error": "Export job not found or expired."}), 404
        job = _resolve_job_status(job, export_id)
        if str(job.get("status")) != "success":
            return jsonify({"error": f"Export is not finished (status={job.get('status')})."}), 409

        file_path = _export_file_path(export_id)
        if not os.path.isfile(file_path):
            return jsonify({"error": "Export file is missing on the server."}), 404
        download_name = secure_filename(str(job.get("file_name") or "")) or f"{export_id}.xlsx"
        if not download_name.endswith(".xlsx"):
            download_name = f"{download_name}.xlsx"
        response = send_file(
            file_path,
            mimetype=XLSX_MIME_TYPE,
            as_attachment=True,
            download_name=download_name,
            max_age=0,
        )
        # secure_filename is ASCII-only and strips the (often Chinese) project name;
        # send the original as an RFC 5987 filename* so browsers keep the full name.
        try:
            from urllib.parse import quote as _url_quote

            unicode_name = str(job.get("file_name") or download_name)
            if unicode_name != download_name:
                response.headers["Content-Disposition"] = (
                    f'attachment; filename="{download_name}"; '
                    f"filename*=UTF-8''{_url_quote(unicode_name)}"
                )
        except Exception:
            logger.debug("Could not attach RFC 5987 filename header for export %s.", export_id)
        return response
