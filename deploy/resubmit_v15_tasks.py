#!/usr/bin/env python3
"""Resubmit v15 peptide-design tasks, reusing the existing project_tasks rows.

DANGER: submit ONLY the tasks that actually need it. Resubmitting healthy
tasks double-dispatches them (the old celery task keeps running as an orphan
burning GPU — the 2026-10-12 incident: 14 orphans starved the D route's
target predictions into their 1-hour timeout).

Filter examples:
  python3 deploy/resubmit_v15_tasks.py --chirality d        # all D tasks
  python3 deploy/resubmit_v15_tasks.py --name-substr CtBP1  # by name
  python3 deploy/resubmit_v15_tasks.py --list               # dry run
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, "/tmp")
from v11_L_tasks import (  # noqa: E402
    REST, PROJECT_ID, TARGETS,
    http, submit_predict, patch_task_row,
)


def find_row_id(name: str) -> str | None:
    url = (f"{REST}/project_tasks?project_id=eq.{PROJECT_ID}"
           f"&name=eq.{urllib.parse.quote(name)}&select=id,task_id,task_state")
    status, text = http("GET", url)
    rows = json.loads(text)
    return rows[0]["id"] if rows else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chirality", choices=("l", "d"), default=None)
    parser.add_argument("--name-substr", default=None)
    parser.add_argument("--list", action="store_true", help="dry run")
    args = parser.parse_args()

    results = []
    for target, pdb, sequence, idx in TARGETS:
        for chirality, base in (("l", 442000), ("d", 443000)):
            if args.chirality and chirality != args.chirality:
                continue
            name = f"降解剂v15-{chirality.upper()}-{target}({pdb})"
            if args.name_substr and args.name_substr not in name:
                continue
            row_id = find_row_id(name)
            if not row_id:
                print(f"SKIP {name}: no row")
                continue
            print(f"WILL SUBMIT {name} (row {row_id[:8]})")
            results.append((name, sequence, base + idx, chirality, row_id))

    if args.list:
        print(f"\n{len(results)} task(s) selected (dry run)")
        return

    ok = 0
    for name, sequence, seed, chirality, row_id in results:
        options = {
            "peptideDesignMode": "bicyclic",
            "peptideStructureMode": "auto",
            "peptideBinderLength": 15,
            "peptideIterations": 6,
            "peptidePopulationSize": 12,
            "peptideEliteSize": 4,
            "peptideBicyclicLinkerCcd": "SEZ",
            "peptideChirality": chirality,
        }
        try:
            task_id = submit_predict(sequence, seed, options)
            patch_task_row(row_id, task_id)
            print(f"OK  {name}  task_id={task_id} seed={seed}", flush=True)
            ok += 1
        except Exception as exc:
            print(f"ERR {name}  {exc}", flush=True)
        time.sleep(1)
    print(f"\nresubmitted {ok}/{len(results)}")


if __name__ == "__main__":
    main()
