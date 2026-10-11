#!/usr/bin/env python3
"""v15 batch health monitor: run any time to see all 18 design tasks."""
import json
import subprocess
import sys


def q(sql):
    out = subprocess.run(
        ["docker", "exec", "frontend2_supabase_db", "psql", "-U", "postgres",
         "-d", "postgres", "-t", "-A", "-c", sql],
        capture_output=True, text=True)
    return [line.split("|") for line in out.stdout.strip().split("\n") if line]


def progress(task_id):
    out = subprocess.run(
        ["docker", "exec", "vbio-cpu-worker", "sh", "-c",
         f"cat /data/boltz_central_results/_runtime_tmp/boltz_task_{task_id}*/peptide_progress.json 2>/dev/null"],
        capture_output=True, text=True)
    try:
        return json.loads(out.stdout)["peptide_design"]
    except Exception:
        return {}


rows = q("SELECT name, task_state, task_id FROM project_tasks "
         "WHERE project_id='3bf7199e-0e4e-483b-b896-c7dd07b157e4' "
         "AND name LIKE '降解剂v15-%' ORDER BY name")
print(f"{'task':38} {'state':10} {'gen':6} {'done':8} best     metric")
for name, state, tid in rows:
    p = progress(tid)
    gen = f"{p.get('current_generation', '-')}/{p.get('total_generations', '-')}"
    done = p.get("status_message", "").split(":")[-1].strip()[:14] or "-"
    best = p.get("best_score")
    best = f"{best:.4f}" if isinstance(best, (int, float)) else "-"
    seqs = p.get("current_best_sequences") or []
    metric = ""
    if seqs:
        b = seqs[0]
        metric = (f"{b.get('interface_metric_label')}:"
                  f"{(b.get('interface_metric') or 0):.3f}")
    print(f"{name:38} {state:10} {gen:6} {done:8} {best:8} {metric}")

print()
out = subprocess.run(["docker", "exec", "vbio-redis", "redis-cli", "llen", "cap.protenix.default"],
                     capture_output=True, text=True)
print(f"protenix queue backlog: {out.stdout.strip()}")
out = subprocess.run(["sh", "-c", "nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv,noheader"],
                     capture_output=True, text=True)
print("GPU:", " | ".join(out.stdout.strip().split("\n")))
