from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any, Dict, Iterable


_SUCCESS_STATES = {"SUCCESS", "COMPLETE", "COMPLETED"}
_FAILURE_STATES = {"FAILURE", "FAILED", "ERROR"}
_RUNNING_STATES = {"RUNNING", "PROGRESS", "STARTED"}
_QUEUED_STATES = {"QUEUED", "PENDING", "RECEIVED", "RETRY", "SCHEDULED"}
_CANCELLED_STATES = {"REVOKED", "CANCELLED", "CANCELED"}


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _state_bucket(value: Any) -> str:
    state = str(value or "").strip().upper()
    if state in _SUCCESS_STATES:
        return "success"
    if state in _FAILURE_STATES:
        return "failure"
    if state in _RUNNING_STATES:
        return "running"
    if state in _QUEUED_STATES:
        return "queued"
    if state in _CANCELLED_STATES:
        return "cancelled"
    return "other"


def _safe_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) and parsed >= 0 else None


def build_task_statistics(
    rows: Iterable[Dict[str, Any]],
    *,
    window_hours: int,
    now: datetime | None = None,
    row_limit: int = 10000,
    recent_limit: int = 20,
) -> Dict[str, Any]:
    generated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    hours = max(1, min(24 * 31, int(window_hours or 24)))
    window_start = generated_at - timedelta(hours=hours)
    interval_hours = 1 if hours <= 48 else 24
    interval_count = max(1, (hours + interval_hours - 1) // interval_hours)
    timeline = []
    for index in range(interval_count):
        start = window_start + timedelta(hours=index * interval_hours)
        timeline.append({
            "start": start.isoformat(),
            "total": 0,
            "success": 0,
            "failure": 0,
        })

    state_counts: Counter[str] = Counter()
    backend_counts: Dict[str, Counter[str]] = {}
    durations: list[float] = []
    recent_tasks: list[Dict[str, Any]] = []
    accepted_rows = 0
    input_rows = list(rows or [])

    for raw_row in input_rows:
        row = dict(raw_row or {})
        task_id = str(row.get("task_id") or "").strip()
        if not task_id:
            continue
        created_at = (
            _parse_datetime(row.get("submitted_at"))
            or _parse_datetime(row.get("created_at"))
        )
        if created_at is None or created_at < window_start or created_at > generated_at + timedelta(minutes=5):
            continue

        accepted_rows += 1
        bucket = _state_bucket(row.get("task_state"))
        state_counts[bucket] += 1
        backend = str(row.get("backend") or "unknown").strip().lower() or "unknown"
        backend_counter = backend_counts.setdefault(backend, Counter())
        backend_counter["total"] += 1
        backend_counter[bucket] += 1

        duration = _safe_float(row.get("duration_seconds"))
        if duration is None:
            completed_at = _parse_datetime(row.get("completed_at"))
            if completed_at is not None and completed_at >= created_at:
                duration = (completed_at - created_at).total_seconds()
        if duration is not None and bucket in {"success", "failure"}:
            durations.append(duration)

        elapsed_hours = max(0.0, (created_at - window_start).total_seconds() / 3600.0)
        interval_index = min(interval_count - 1, int(elapsed_hours // interval_hours))
        timeline[interval_index]["total"] += 1
        if bucket == "success":
            timeline[interval_index]["success"] += 1
        elif bucket == "failure":
            timeline[interval_index]["failure"] += 1

        if len(recent_tasks) < max(1, recent_limit):
            recent_tasks.append({
                "id": str(row.get("id") or ""),
                "project_id": str(row.get("project_id") or ""),
                "task_id": task_id,
                "name": str(row.get("name") or task_id),
                "backend": backend,
                "state": str(row.get("task_state") or "").strip().upper(),
                "bucket": bucket,
                "submitted_at": created_at.isoformat(),
                "completed_at": (_parse_datetime(row.get("completed_at")) or None).isoformat()
                if _parse_datetime(row.get("completed_at"))
                else None,
                "duration_seconds": duration,
                "status_text": str(row.get("status_text") or ""),
                "error_text": str(row.get("error_text") or ""),
            })

    terminal_total = state_counts["success"] + state_counts["failure"]
    by_backend = []
    for backend, counter in backend_counts.items():
        by_backend.append({
            "backend": backend,
            "total": counter["total"],
            "queued": counter["queued"],
            "running": counter["running"],
            "success": counter["success"],
            "failure": counter["failure"],
            "cancelled": counter["cancelled"],
            "other": counter["other"],
        })
    by_backend.sort(key=lambda item: (-int(item["total"]), str(item["backend"])))

    return {
        "generated_at": generated_at.isoformat(),
        "window_start": window_start.isoformat(),
        "window_hours": hours,
        "total": accepted_rows,
        "states": {
            "queued": state_counts["queued"],
            "running": state_counts["running"],
            "success": state_counts["success"],
            "failure": state_counts["failure"],
            "cancelled": state_counts["cancelled"],
            "other": state_counts["other"],
        },
        "terminal_total": terminal_total,
        "success_rate": (float(state_counts["success"]) / float(terminal_total)) if terminal_total else None,
        "average_duration_seconds": (sum(durations) / len(durations)) if durations else None,
        "by_backend": by_backend,
        "timeline": timeline,
        "recent_tasks": recent_tasks,
        "truncated": len(input_rows) >= max(1, row_limit),
    }
