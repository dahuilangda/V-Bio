"""任务状态对账清扫器：不依赖页面打开的服务端定期对账。

行状态对账原先是"页面打开并轮询到这些 id"才触发的自愈——没人重新打开的
项目里，行会永远停留在 QUEUED/RUNNING（结果实际早已产出或随 worker 重启
丢失）。本清扫器每 interval 秒扫描非终态且创建超过 stale_after 的行，
按项目批量询问中心 API 的真实状态：

- 中心返回终态（SUCCESS/FAILURE/REVOKED）→ reconcile_terminal_rows 推进行；
- 中心报告 PENDING/UNKNOWN 且创建超过 lost_after → 标记 FAILURE
  （at-most-once 语义下消息已随 worker 重启丢失，需重新提交）；
- RUNNING → 不动（活跃任务）。

多 gunicorn worker 用同主机 fcntl 锁互斥，重复扫描是幂等的。中心不可达
时本轮跳过，下轮重试。
"""

from __future__ import annotations

import fcntl
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

import requests

LOGGER_NAME = "vbio-task-janitor"


class TaskStatusJanitor:
    def __init__(
        self,
        *,
        postgrest: Any,
        task_store: Any,
        runtime_api_base_url: str,
        runtime_api_token: str,
        lock_path: str,
        logger: Any,
        interval_seconds: float = 600.0,
        stale_after_seconds: float = 1800.0,
        lost_after_seconds: float = 24 * 3600.0,
        batch_size: int = 200,
    ) -> None:
        self.postgrest = postgrest
        self.task_store = task_store
        self.runtime_api_base_url = runtime_api_base_url.rstrip("/")
        self.runtime_api_token = runtime_api_token.strip()
        self.lock_path = lock_path
        self.logger = logger
        self.interval_seconds = max(60.0, float(interval_seconds))
        self.stale_after_seconds = max(60.0, float(stale_after_seconds))
        self.lost_after_seconds = max(600.0, float(lost_after_seconds))
        self.batch_size = max(1, int(batch_size))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="vbio-task-status-janitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # 首轮延迟，避免与启动期的批量加载争抢
        self._stop.wait(self.interval_seconds)
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                with open(self.lock_path, "w") as lock_handle:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    try:
                        self._sweep_once()
                    except Exception:
                        self.logger.exception("Task status janitor sweep failed")
                    finally:
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            elapsed = time.monotonic() - started
            self._stop.wait(max(1.0, self.interval_seconds - elapsed))

    def _sweep_once(self) -> None:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=self.stale_after_seconds)
        ).strftime("%Y-%m-%dT%H:%M:%S")
        rows = self._scan_rows(cutoff)
        if not rows:
            return
        by_project: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            project = str(row.get("project_id") or "").strip() or "<none>"
            by_project.setdefault(project, []).append(row)

        reconciled = lost = 0
        for project_id, project_rows in sorted(by_project.items()):
            terminal, lost_rows = self._ask_runtime(project_rows)
            # 防御：只有真终态才进对账（PENDING/RUNNING 不是终态）
            terminal = {
                task_id: state
                for task_id, state in terminal.items()
                if state in ("SUCCESS", "FAILURE", "REVOKED")
            }
            if terminal:
                reconciled += self.task_store.reconcile_terminal_rows(project_id, terminal)
            if lost_rows:
                lost += self.task_store.fail_lost_rows(project_id, lost_rows)
            self.logger.info("Task status janitor: reconciled %d rows, failed %d lost rows", reconciled, lost)

    def _scan_rows(self, cutoff: str) -> List[Dict[str, Any]]:
        query = (
            "project_tasks"
            "?select=id,task_id,project_id,name,backend,created_at,updated_at"
            "&task_state=in.(QUEUED,RUNNING)"
            f"&created_at=lt.{cutoff}"
            "&order=created_at.asc"
            f"&limit={self.batch_size}"
        )
        return self.postgrest.request("GET", query) or []

    def _ask_runtime(self, project_rows: List[Dict[str, Any]]) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
        task_ids = [str(row.get("task_id") or "").strip() for row in project_rows]
        task_ids = [task_id for task_id in task_ids if task_id]
        if not task_ids:
            return {}, []
        response = requests.post(
            f"{self.runtime_api_base_url}/status/batch",
            json={"task_ids": task_ids},
            headers={"X-API-Token": self.runtime_api_token, "Accept": "application/json"},
            timeout=60,
        )
        response.raise_for_status()
        statuses = (response.json() or {}).get("statuses") or []
        by_id = {str(s.get("task_id") or ""): s for s in statuses if isinstance(s, dict)}
        terminal: Dict[str, str] = {}
        lost_rows: List[Dict[str, Any]] = []
        for row in project_rows:
            task_id = str(row.get("task_id") or "").strip()
            status = by_id.get(task_id) or {}
            state = str(status.get("state") or "").strip().upper()
            created = str(row.get("created_at") or "")
            try:
                created_ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if state in ("SUCCESS", "FAILURE", "REVOKED"):
                terminal[task_id] = state
            elif state in ("", "PENDING", "UNKNOWN"):
                created_ts = _parse_ts(created)
                age = time.time() - created_ts if created_ts else None
                if age is not None and age >= self.lost_after_seconds:
                    lost_rows.append(
                        {"row_id": str(row.get("id") or ""), "task_id": task_id, "backend": str(row.get("backend") or "")}
                    )
        return terminal, lost_rows


def _parse_ts(value: str) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
