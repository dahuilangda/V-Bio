"""状态批量读取路径的服务端终态对账。

行状态的 QUEUED→SUCCESS 平台上由前端轮询回写; 页面没开着或任务经 API
直提时行会停留。对账让任意一次 /status/batch 读取把已有运行时终态的行
推进到终态, 仅推进非终态行, 失败不影响状态读取本身。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class _StubPostgrest:
    def __init__(self) -> None:
        self.patches: list[tuple[str, dict]] = []

    def request(self, method, resource, query=None, payload=None, headers=None, expect_json=True):
        if method == "PATCH":
            self.patches.append((query["task_id"], payload))
            return 204
        return 200


class _StubStore:
    """只暴露 reconcile 所需面的最小桩。"""

    def __init__(self, rows: dict, postgrest: _StubPostgrest) -> None:
        self.rows = rows
        self.postgrest = postgrest

    def find_project_tasks(self, task_ids, project_id):
        return {tid: self.rows[tid] for tid in task_ids if tid in self.rows}


def _make_store(rows: dict):
    from management_api.task_store import ProjectTaskStore as TaskStore

    pg = _StubPostgrest()
    store = TaskStore(pg)  # type: ignore[arg-type]
    # 重绑 find_project_tasks 到桩(避免构造真实 client)
    stub = _StubStore(rows, pg)
    store.find_project_tasks = stub.find_project_tasks  # type: ignore[method-assign]
    return store, pg


def test_reconcile_patches_non_terminal_row():
    rows = {"tid-1": {"task_id": "tid-1", "task_state": "QUEUED"}}
    store, pg = _make_store(rows)

    patched = store.reconcile_terminal_rows("proj-1", {"tid-1": "SUCCESS"})

    assert patched == 1
    assert len(pg.patches) == 1
    tid, payload = pg.patches[0]
    assert tid == "eq.tid-1"
    assert payload["task_state"] == "SUCCESS"
    assert payload["completed_at"]


def test_reconcile_skips_already_terminal_rows():
    rows = {
        "tid-a": {"task_id": "tid-a", "task_state": "SUCCESS"},
        "tid-b": {"task_id": "tid-b", "task_state": "FAILURE"},
        "tid-c": {"task_id": "tid-c", "task_state": "REVOKED"},
    }
    store, pg = _make_store(rows)

    patched = store.reconcile_terminal_rows(
        "proj-1", {"tid-a": "SUCCESS", "tid-b": "FAILURE", "tid-c": "REVOKED"})

    assert patched == 0
    assert pg.patches == []


def test_reconcile_maps_revoked_to_row_state():
    rows = {"tid-2": {"task_id": "tid-2", "task_state": "RUNNING"}}
    store, pg = _make_store(rows)

    store.reconcile_terminal_rows("proj-1", {"tid-2": "REVOKED"})

    assert pg.patches[0][1]["task_state"] == "REVOKED"


def test_reconcile_ignores_unknown_task_ids():
    store, pg = _make_store({})

    assert store.reconcile_terminal_rows("proj-1", {"ghost": "SUCCESS"}) == 0
    assert pg.patches == []
