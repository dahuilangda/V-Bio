from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from management_api.admin_monitor import build_task_statistics
from management_api.task_store import ProjectTaskStore


class _FakePostgrest:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, table_or_view: str, **kwargs: Any) -> Any:
        self.calls.append({
            "method": method,
            "table_or_view": table_or_view,
            **kwargs,
        })
        return list(self.rows)


class AdminTaskStatisticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)

    def _row(
        self,
        task_id: str,
        state: str,
        hours_ago: float,
        *,
        backend: str = "boltz2",
        duration: float | None = None,
        completed_after: float | None = None,
    ) -> dict[str, Any]:
        submitted = self.now - timedelta(hours=hours_ago)
        completed = submitted + timedelta(seconds=completed_after) if completed_after is not None else None
        return {
            "id": f"row-{task_id}",
            "project_id": "project-1",
            "task_id": task_id,
            "name": f"Task {task_id}",
            "backend": backend,
            "task_state": state,
            "status_text": state.title(),
            "error_text": "boom" if state == "FAILURE" else "",
            "submitted_at": submitted.isoformat(),
            "completed_at": completed.isoformat() if completed else None,
            "duration_seconds": duration,
            "created_at": submitted.isoformat(),
        }

    def test_aggregates_states_backends_duration_and_timeline(self) -> None:
        rows = [
            self._row("success", "SUCCESS", 1, duration=120),
            self._row("failure", "FAILURE", 2, backend="nesso", completed_after=60),
            self._row("running", "RUNNING", 3),
            self._row("queued", "QUEUED", 4, backend="nesso"),
            self._row("cancelled", "REVOKED", 5, duration=5),
            self._row("other", "DRAFT", 6),
            self._row("", "SUCCESS", 1, duration=1),
            self._row("old", "SUCCESS", 30, duration=1),
        ]

        result = build_task_statistics(
            rows,
            window_hours=24,
            now=self.now,
            recent_limit=3,
        )

        self.assertEqual(result["total"], 6)
        self.assertEqual(result["states"], {
            "queued": 1,
            "running": 1,
            "success": 1,
            "failure": 1,
            "cancelled": 1,
            "other": 1,
        })
        self.assertEqual(result["terminal_total"], 2)
        self.assertEqual(result["success_rate"], 0.5)
        self.assertEqual(result["average_duration_seconds"], 90)
        self.assertEqual(len(result["timeline"]), 24)
        self.assertEqual(sum(point["total"] for point in result["timeline"]), 6)
        self.assertEqual([item["backend"] for item in result["by_backend"]], ["boltz2", "nesso"])
        self.assertEqual(len(result["recent_tasks"]), 3)
        self.assertEqual(result["recent_tasks"][0]["task_id"], "success")

    def test_uses_daily_buckets_for_long_windows_and_marks_truncation(self) -> None:
        rows = [
            self._row("one", "COMPLETED", 2, duration=10),
            self._row("two", "ERROR", 50, duration=float("inf")),
        ]
        result = build_task_statistics(
            rows,
            window_hours=24 * 7,
            now=self.now,
            row_limit=2,
        )

        self.assertEqual(len(result["timeline"]), 7)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["states"]["success"], 1)
        self.assertEqual(result["states"]["failure"], 1)
        self.assertEqual(result["average_duration_seconds"], 10)

    def test_rejects_invalid_and_far_future_timestamps(self) -> None:
        rows = [
            {**self._row("invalid", "SUCCESS", 1), "submitted_at": "not-a-date", "created_at": ""},
            {**self._row("future", "SUCCESS", 1), "submitted_at": (self.now + timedelta(minutes=6)).isoformat()},
        ]
        result = build_task_statistics(rows, window_hours=24, now=self.now)
        self.assertEqual(result["total"], 0)
        self.assertIsNone(result["success_rate"])


class ProjectTaskStoreStatisticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_patch = patch.dict(
            os.environ,
            {
                "VBIO_ADMIN_STATS_CACHE_FILE": str(
                    Path(self.temp_dir.name) / "admin-statistics.json"
                )
            },
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.temp_dir.cleanup()

    def test_queries_compact_task_view_with_bounded_window(self) -> None:
        submitted = datetime.now(timezone.utc) - timedelta(hours=1)
        postgrest = _FakePostgrest([
            {
                "id": "row-1",
                "project_id": "project-1",
                "task_id": "task-1",
                "name": "Prediction",
                "backend": "protenix",
                "task_state": "SUCCESS",
                "submitted_at": submitted.isoformat(),
                "completed_at": (submitted + timedelta(seconds=42)).isoformat(),
                "duration_seconds": 42,
                "created_at": submitted.isoformat(),
                "status_text": "Complete",
                "error_text": "",
            }
        ])
        store = ProjectTaskStore(postgrest)  # type: ignore[arg-type]

        result = store.get_admin_statistics(window_hours=48, row_limit=100)
        cached_result = store.get_admin_statistics(window_hours=48, row_limit=100)

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["average_duration_seconds"], 42)
        self.assertIs(cached_result, result)
        self.assertEqual(len(postgrest.calls), 1)
        call = postgrest.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["table_or_view"], "project_tasks_list")
        self.assertEqual(call["query"]["task_id"], "neq.")
        self.assertEqual(call["query"]["limit"], "100")
        self.assertIn("submitted_at.gte.", call["query"]["or"])
        self.assertIn("duration_seconds", call["query"]["select"])

    def test_shared_cache_is_reused_by_another_store_instance(self) -> None:
        first_postgrest = _FakePostgrest([])
        second_postgrest = _FakePostgrest([])
        first_store = ProjectTaskStore(first_postgrest)  # type: ignore[arg-type]
        second_store = ProjectTaskStore(second_postgrest)  # type: ignore[arg-type]

        first = first_store.get_admin_statistics(window_hours=24, row_limit=100)
        second = second_store.get_admin_statistics(window_hours=24, row_limit=100)

        self.assertEqual(first, second)
        self.assertEqual(len(first_postgrest.calls), 1)
        self.assertEqual(len(second_postgrest.calls), 0)


if __name__ == "__main__":
    unittest.main()
