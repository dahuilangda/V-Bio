"""Redis-backed job store for asynchronous Excel exports.

The API server creates a job record here when it dispatches the Celery export
task; the worker mutates the same record as the export progresses. Keeping the
state in Redis (instead of only in Celery's result backend) lets the status and
download endpoints serve progress counters and the final file name without
loading the worker's return payload.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, Optional

EXPORT_JOB_KEY_PREFIX = "boltz_export:tasks_excel:"
EXPORT_JOB_STATUSES = ("queued", "running", "success", "failure")


def export_job_key(export_id: str) -> str:
    normalized = str(export_id or "").strip()
    if not normalized:
        raise ValueError("export_id must be a non-empty string.")
    return f"{EXPORT_JOB_KEY_PREFIX}{normalized}"


def _blank_job(export_id: str, **overrides: Any) -> Dict[str, Any]:
    """Canonical empty record — create() and update()-recreate share it so the
    field set can never drift between the two."""
    job: Dict[str, Any] = {
        "export_id": export_id,
        "celery_task_id": "",
        "project_name": "",
        "queue": "",
        "status": "queued",
        "total": 0,
        "done": 0,
        "file_name": "",
        "file_bytes": 0,
        "warning": "",
        "error": "",
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    job.update(overrides)
    return job


class ExportJobStore:
    def __init__(self, *, get_redis_client_fn, logger, ttl_seconds: int = 48 * 3600) -> None:
        self.get_redis_client = get_redis_client_fn
        self.logger = logger
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._local_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_lock = threading.Lock()

    def create(
        self,
        *,
        export_id: str,
        celery_task_id: str,
        project_name: str,
        total: int,
        queue: str,
    ) -> Dict[str, Any]:
        job = _blank_job(
            export_id,
            celery_task_id=celery_task_id,
            project_name=project_name,
            total=int(total),
            queue=queue,
        )
        self._write(job)
        return job

    def load(self, export_id: str) -> Optional[Dict[str, Any]]:
        key = export_job_key(export_id)
        redis_failed = False
        try:
            raw = self.get_redis_client().get(key)
        except Exception as exc:
            self.logger.warning("Failed to read export job %s from Redis: %s", export_id, exc)
            raw = None
            redis_failed = True
        if raw is None:
            with self._cache_lock:
                cached = self._local_cache.get(export_id)
            if cached is None:
                return None
            # The cache exists only to survive Redis blips; it must not extend the
            # job's life past the TTL (expired jobs should 404 like the Redis copy).
            if time.time() - float(cached.get("updated_at") or 0) > self.ttl_seconds:
                with self._cache_lock:
                    self._local_cache.pop(export_id, None)
                return None
            if not redis_failed:
                # Redis answered (key genuinely absent) and the record is not in
                # Redis — treat as expired/deleted rather than resurrecting cache.
                return None
            return dict(cached)
        try:
            payload = json.loads(raw)
        except Exception:
            self.logger.warning("Corrupted export job record for %s; ignoring.", export_id)
            return None
        if not isinstance(payload, dict):
            return None
        with self._cache_lock:
            self._local_cache[export_id] = dict(payload)
        return dict(payload)

    def update(self, export_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        job = self.load(export_id)
        if job is None:
            # The record vanished (TTL expiry or a Redis flush mid-export). Write a
            # fresh record from the provided fields instead of silently dropping
            # the update — a lost progress write would freeze the job's state.
            job = _blank_job(export_id, status="running")
        job.update(fields)
        job["updated_at"] = time.time()
        status = job.get("status")
        if status not in EXPORT_JOB_STATUSES:
            job["status"] = "running"
        self._write(job)
        return job

    def _write(self, job: Dict[str, Any]) -> None:
        key = export_job_key(str(job.get("export_id") or ""))
        try:
            self.get_redis_client().set(key, json.dumps(job), ex=self.ttl_seconds)
        except Exception as exc:
            # A Redis blip must not abort the export: the worker keeps building
            # the file and the in-process cache keeps status queries answerable.
            self.logger.warning("Failed to persist export job %s to Redis: %s", job.get("export_id"), exc)
        with self._cache_lock:
            self._local_cache[str(job.get("export_id"))] = dict(job)
            if len(self._local_cache) > 128:
                self._local_cache.clear()
