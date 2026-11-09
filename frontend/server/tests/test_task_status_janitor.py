"""任务状态对账清扫器：终态对账、丢失任务标记 FAILURE、按项目分组契约。"""
from __future__ import annotations

import unittest
from unittest import mock

from management_api.task_status_janitor import TaskStatusJanitor


class FakePostgrest:
    def __init__(self, rows):
        self.rows = rows
        self.scans = 0
        self.patches = []

    def request(self, method, path, **kwargs):
        if method == "GET" and "task_state=in" in path:
            self.scans += 1
            return self.rows
        if method == "PATCH":
            self.patches.append({"query": kwargs.get("query"), "payload": kwargs.get("payload")})
        return []


class FakeTaskStore:
    def __init__(self):
        self.reconciled = []
        self.failed = []

    def reconcile_terminal_rows(self, project_id, terminal):
        self.reconciled.append((project_id, dict(terminal)))
        return len(terminal)

    def fail_lost_rows(self, project_id, rows):
        self.failed.append((project_id, list(rows)))
        return len(rows)


def task_row(row_id, project_id, task_id, state, age_hours):
    import datetime

    created = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=age_hours)).isoformat()
    return {
        "id": row_id,
        "task_id": task_id,
        "project_id": project_id,
        "task_state": state,
        "backend": "mol1",
        "created_at": created,
    }


def make_janitor(rows, store=None):
    postgrest = FakePostgrest(rows)
    store = store or FakeTaskStore()
    store.postgrest = postgrest
    janitor = TaskStatusJanitor(
        postgrest=postgrest,
        task_store=store,
        runtime_api_base_url="http://127.0.0.1:5000",
        runtime_api_token="tok",
        lock_path="/tmp/vbio-janitor-test.lock",
        logger=mock.Mock(),
        interval_seconds=600,
        stale_after_seconds=1800,
        lost_after_seconds=24 * 3600,
    )
    return janitor, postgrest, store


class SweepTests(unittest.TestCase):
    def test_terminal_rows_reconciled(self):
        rows = [
            task_row("r1", "p-1", "cel-1", "QUEUED", 2),
            task_row("r2", "p-1", "cel-2", "QUEUED", 2),
        ]
        janitor, postgrest, store = make_janitor(rows)
        with mock.patch.object(
            TaskStatusJanitor, "_ask_runtime",
            return_value=({"cel-1": "SUCCESS", "cel-2": "RUNNING"}, []),
        ):
            janitor._sweep_once()
        # RUNNING 不是终态：只推进 cel-1
        self.assertEqual(store.reconciled, [("p-1", {"cel-1": "SUCCESS"})])
        self.assertEqual(postgrest.patches, [])

    def test_lost_row_marked_failure_after_threshold(self):
        rows = [task_row("r2", "p-1", "cel-2", "QUEUED", 48)]
        janitor, postgrest, store = make_janitor(rows)
        with mock.patch.object(
            TaskStatusJanitor, "_ask_runtime",
            return_value=({"cel-2": "PENDING"}, [{"row_id": "r2", "task_id": "cel-2"}]),
        ):
            janitor._sweep_once()
        self.assertEqual(len(store.failed), 1)
        self.assertEqual(store.failed[0][0], "p-1")
        self.assertEqual(store.failed[0][1][0]["task_id"], "cel-2")

    def test_running_rows_left_alone(self):
        rows = [task_row("r3", "p-1", "cel-3", "RUNNING", 1)]
        janitor, postgrest, store = make_janitor(rows)
        with mock.patch.object(
            TaskStatusJanitor, "_ask_runtime",
            return_value=({"cel-3": "RUNNING"}, []),
        ):
            janitor._sweep_once()
        self.assertEqual(store.reconciled, [])
        self.assertEqual(store.failed, [])
        self.assertEqual(postgrest.patches, [])

    def test_rows_grouped_by_project_for_batch_query(self):
        rows = [
            task_row("r-a", "p-a", "cel-a", "QUEUED", 30),
            task_row("r-b", "p-b", "cel-b", "QUEUED", 30),
        ]
        janitor, postgrest, store = make_janitor(rows)
        projects_queried = []

        def fake_ask(project_rows):
            projects_queried.append(sorted({r["project_id"] for r in project_rows}))
            return {}, []

        with mock.patch.object(TaskStatusJanitor, "_ask_runtime", side_effect=fake_ask):
            janitor._sweep_once()
        self.assertEqual(len(projects_queried), 2)
        for projects in projects_queried:
            self.assertEqual(len(projects), 1)


if __name__ == "__main__":
    unittest.main()
