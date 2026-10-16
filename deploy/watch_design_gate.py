#!/usr/bin/env python3
"""Block until the design batch gen1 milestone: the first L route task finishes gen1
finalization — best_score becomes a number in peptide_progress.json — or the
task fails, or 8 hours elapse. Exit 0 on milestone, 2 on timeout."""
import json
import subprocess
import sys
import time

import os
TASK = os.environ.get("WATCH_TASK_ID", "")
if not TASK:
    print("Usage: WATCH_TASK_ID=<task_id> python3 deploy/watch_design_gate.py")
    sys.exit(1)
DEADLINE = time.time() + 8 * 3600


def sh(cmd: str) -> str:
    return subprocess.run(["docker", "exec", "vbio-cpu-worker", "sh", "-c", cmd],
                          capture_output=True, text=True).stdout


def progress() -> dict:
    out = sh(f"cat /data/boltz_central_results/_runtime_tmp/boltz_task_{TASK}*/peptide_progress.json 2>/dev/null")
    try:
        return json.loads(out)["peptide_design"]
    except Exception:
        return {}


def failures() -> int:
    out = subprocess.run(
        ["docker", "exec", "frontend2_supabase_db", "psql", "-U", "postgres",
         "-d", "postgres", "-t", "-A", "-c",
         "SELECT count(*) FROM project_tasks WHERE project_id='3bf7199e-0e4e-483b-b896-c7dd07b157e4' "
         "AND name LIKE '降解剂%' AND task_state IN ('FAILURE','REVOKED');"],
        capture_output=True, text=True).stdout
    try:
        return int(out.strip() or 0)
    except ValueError:
        return -1


def state() -> str:
    out = subprocess.run(
        ["docker", "exec", "frontend2_supabase_db", "psql", "-U", "postgres",
         "-d", "postgres", "-t", "-A", "-c",
         f"SELECT task_state FROM project_tasks WHERE task_id='{TASK}';"],
        capture_output=True, text=True).stdout
    return out.strip()


last_report = 0.0
while time.time() < DEADLINE:
    p = progress()
    best = p.get("best_score")
    row_state = state()
    fails = failures()
    now = time.time()
    if best is not None:
        print(json.dumps({"milestone": "best_score", "value": best,
                          "gen": p.get("current_generation"),
                          "msg": p.get("status_message"),
                          "failures": fails}, ensure_ascii=False))
        sys.exit(0)
    if row_state in ("FAILURE", "REVOKED"):
        print(json.dumps({"milestone": "task_failed", "state": row_state,
                          "error_hint": "see cpu worker logs"}, ensure_ascii=False))
        sys.exit(1)
    if fails > 0 and now - last_report > 1800:
        print(f"[watcher] failures detected: {fails}", flush=True)
        last_report = now
    time.sleep(120)
print(json.dumps({"milestone": "timeout"}))
sys.exit(2)
