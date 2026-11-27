"""Store V-Bio task receipts in the current client workspace."""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3

TERMINAL_STATES = {"SUCCESS", "FAILURE", "REVOKED"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def summarize_status(payload, task_id, token):
    if not isinstance(payload, dict) or payload.get("task_id") != task_id:
        raise ValueError("The gateway returned a status for a different task.")
    state = payload.get("state")
    if not isinstance(state, str) or not state.strip():
        raise ValueError("The gateway status response has no state.")
    state = state.strip().upper()
    info = payload.get("info")
    info = info if isinstance(info, dict) else {}
    result = {"state": state}
    for key in ("message", "status", "exc_message"):
        message = info.get(key)
        if isinstance(message, list):
            message = " ".join(str(item) for item in message)
        if isinstance(message, str) and message.strip():
            result["status_message"] = " ".join(message.replace(token, "[redacted]").split())[:400]
            break
    progress = info.get("progress_percent")
    if type(progress) in (float, int) and 0 <= progress <= 100:
        result["progress_percent"] = progress
    if state in TERMINAL_STATES:
        completed = info.get("completed_at")
        if isinstance(completed, str):
            result["completed_at"] = completed
    else:
        remaining = info.get("estimated_remaining_seconds")
        result["estimated_remaining_seconds"] = (
            remaining if type(remaining) in (float, int) and 0 <= remaining < math.inf else None
        )
    return result


class TaskHistory:
    def __init__(self, root):
        self.path = Path(root) / ".vbio" / "tasks.sqlite3"

    @contextmanager
    def database(self):
        self.path.parent.mkdir(exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    api_base TEXT NOT NULL, project_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    receipt TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY (api_base, project_id, task_id)
                )
            """)
            yield db

    def save(self, base, project, task_id, **fields):
        key = (base, project, task_id)
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            stored = db.execute(
                "SELECT receipt FROM tasks WHERE api_base=? AND project_id=? AND task_id=?", key
            ).fetchone()
            receipt = json.loads(stored[0]) if stored else dict(zip(("api_base", "project_id", "task_id"), key))
            if "state" in fields:
                for name in ("status_message", "progress_percent", "estimated_remaining_seconds", "completed_at"):
                    receipt.pop(name, None)
            receipt.update(fields)
            receipt["updated_at"] = now()
            db.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?, ?, ?, ?)",
                       (*key, json.dumps(receipt, ensure_ascii=False), receipt["updated_at"]))
        return receipt

    def list(self):
        if not self.path.is_file():
            return []
        with self.database() as db:
            rows = db.execute("SELECT receipt FROM tasks ORDER BY updated_at DESC, project_id, task_id").fetchall()
        records = [json.loads(row[0]) for row in rows]
        for receipt in records:
            path = receipt.get("result_path")
            if path:
                receipt["result_file_exists"] = Path(path).is_file()
        return records
