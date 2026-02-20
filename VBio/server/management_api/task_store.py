from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from management_api.postgrest_client import PostgrestClient


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProjectTaskStore:
    def __init__(self, postgrest: PostgrestClient) -> None:
        self.postgrest = postgrest

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
        rows = self.postgrest.request(
            "GET",
            "project_tasks",
            query={
                "select": "id,project_id,task_id",
                "task_id": f"eq.{task_id}",
                "project_id": f"eq.{project_id}",
                "order": "created_at.desc",
                "limit": "1",
            },
        )
        return rows[0] if rows else None

    def mark_task_cancelled(self, task_row_id: str) -> None:
        self.postgrest.request(
            "PATCH",
            "project_tasks",
            query={"id": f"eq.{task_row_id}"},
            payload={
                "task_state": "REVOKED",
                "status_text": "Cancelled via API",
                "completed_at": _now_iso(),
            },
            headers={"Prefer": "return=minimal"},
            expect_json=False,
        )

    def delete_task_row(self, task_row_id: str) -> None:
        self.postgrest.request(
            "DELETE",
            "project_tasks",
            query={"id": f"eq.{task_row_id}"},
            headers={"Prefer": "return=minimal"},
            expect_json=False,
        )
