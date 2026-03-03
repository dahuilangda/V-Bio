from __future__ import annotations

from datetime import datetime, timezone
import threading
import time
from typing import Any, Dict, Optional

from management_api.postgrest_client import PostgrestClient


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProjectTaskStore:
    def __init__(self, postgrest: PostgrestClient) -> None:
        self.postgrest = postgrest
        self._alias_lock = threading.Lock()
        self._task_alias_ttl_seconds = 24 * 60 * 60
        self._task_alias_max_entries = 20000
        self._task_aliases: Dict[str, Dict[str, Any]] = {}

    def _cleanup_task_aliases_locked(self, now: Optional[float] = None) -> None:
        current = float(now if now is not None else time.time())
        expired = [
            key
            for key, payload in self._task_aliases.items()
            if float(payload.get("expires_at") or 0.0) <= current
        ]
        for key in expired:
            self._task_aliases.pop(key, None)
        if len(self._task_aliases) <= self._task_alias_max_entries:
            return
        overflow = len(self._task_aliases) - self._task_alias_max_entries
        if overflow <= 0:
            return
        ordered = sorted(
            self._task_aliases.items(),
            key=lambda item: float(item[1].get("updated_at") or 0.0),
        )
        for key, _ in ordered[:overflow]:
            self._task_aliases.pop(key, None)

    def remember_task_alias(self, project_id: str, task_id: str) -> None:
        normalized_project_id = str(project_id or "").strip()
        normalized_task_id = str(task_id or "").strip()
        if not normalized_project_id or not normalized_task_id:
            return
        now = time.time()
        with self._alias_lock:
            self._cleanup_task_aliases_locked(now)
            self._task_aliases[normalized_task_id] = {
                "project_id": normalized_project_id,
                "task_id": normalized_task_id,
                "updated_at": now,
                "expires_at": now + float(self._task_alias_ttl_seconds),
            }

    def insert_snapshot(
        self,
        *,
        project_id: str,
        task_id: str,
        task_name: str,
        task_summary: str,
        backend: str,
        seed: Optional[int],
        extra_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "project_id": project_id,
            "name": task_name,
            "summary": task_summary,
            "task_id": task_id,
            "task_state": "QUEUED",
            "status_text": "Submitted via API",
            "error_text": "",
            "backend": backend,
            "seed": seed,
            "submitted_at": _now_iso(),
        }
        if isinstance(extra_payload, dict) and extra_payload:
            payload.update(extra_payload)

        self.postgrest.request(
            "POST",
            "project_tasks",
            payload=payload,
            headers={"Prefer": "return=minimal"},
            expect_json=False,
        )

    def find_project_task(self, task_id: str, project_id: str) -> Optional[Dict[str, Any]]:
        normalized_task_id = str(task_id or "").strip()
        normalized_project_id = str(project_id or "").strip()
        if not normalized_task_id or not normalized_project_id:
            return None
        rows = self.postgrest.request(
            "GET",
            "project_tasks",
            query={
                "select": "id,project_id,task_id",
                "task_id": f"eq.{normalized_task_id}",
                "project_id": f"eq.{normalized_project_id}",
                "order": "created_at.desc",
                "limit": "1",
            },
        )
        if rows:
            return rows[0]
        now = time.time()
        with self._alias_lock:
            self._cleanup_task_aliases_locked(now)
            alias = self._task_aliases.get(normalized_task_id)
            if not alias:
                return None
            if str(alias.get("project_id") or "").strip() != normalized_project_id:
                return None
            alias["updated_at"] = now
            return {
                "id": f"alias:{normalized_task_id}",
                "project_id": normalized_project_id,
                "task_id": normalized_task_id,
            }

    def mark_task_cancelled(self, task_row_id: str) -> None:
        normalized_task_row_id = str(task_row_id or "").strip()
        if not normalized_task_row_id:
            return
        if normalized_task_row_id.startswith("alias:"):
            return
        self.postgrest.request(
            "PATCH",
            "project_tasks",
            query={"id": f"eq.{normalized_task_row_id}"},
            payload={
                "task_state": "REVOKED",
                "status_text": "Cancelled via API",
                "completed_at": _now_iso(),
            },
            headers={"Prefer": "return=minimal"},
            expect_json=False,
        )

    def delete_task_row(self, task_row_id: str) -> None:
        normalized_task_row_id = str(task_row_id or "").strip()
        if not normalized_task_row_id:
            return
        if normalized_task_row_id.startswith("alias:"):
            return
        self.postgrest.request(
            "DELETE",
            "project_tasks",
            query={"id": f"eq.{normalized_task_row_id}"},
            headers={"Prefer": "return=minimal"},
            expect_json=False,
        )
