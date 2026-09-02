"""Diagnostic: rerun the failing E2E scenarios with full per-round trace output.

Not a test — an investigation tool. Prints every trace round (model call, audit rejection,
observations, terminal state) so a routing or capability failure can be attributed to its
actual cause instead of guessed at.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from e2e_copilot_matrix import (
    TASK_DETAIL_AFFINITY,
    TASK_DETAIL_PREDICTION,
    build_assistant,
)


def dump_trace(turn, name):
    print(f"\n########## {name} ##########")
    print("state:", turn.get("state"))
    for step in turn.get("trace") or []:
        detail = step.get("detail") or {}
        print(f"  [r{step.get('round')}] {step.get('event')} {json.dumps(detail, ensure_ascii=False)[:400]}")
    for q in turn.get("questions") or []:
        print("  QUESTION:", q.get("kind"), str(q.get("text"))[:120], [o.get("value") for o in (q.get("options") or [])])
    for a in turn.get("actions") or []:
        print("  ACTION:", a.get("id"), json.dumps(a.get("arguments"), ensure_ascii=False)[:200])
    for rec in turn.get("observations") or []:
        print("  OBS:", rec.get("source"), {k: rec.get(k) for k in ("pdbId", "cid", "accession", "title") if rec.get(k)})
    print("  MESSAGE:", str(turn.get("content"))[:400].replace("\n", " "))


def main():
    assistant = build_assistant()
    cases = [
        ("docking/target-by-name", "task_detail", TASK_DETAIL_AFFINITY,
         "Fill in the human DHODH into the target"),
        ("docking/ligand-by-common-name", "task_detail", TASK_DETAIL_AFFINITY,
         "The ligand uses brequinar"),
        ("prediction/sequence-by-name", "task_detail", TASK_DETAIL_PREDICTION,
         "Fill in the sequence of human DHODH into the protein component"),
    ]
    for name, ctx, payload, content in cases:
        try:
            turn = assistant.plan_turn(
                context_type=ctx, context_payload=payload,
                user_id="diag", username="diag", content=content,
            )
            dump_trace(turn, name)
        except Exception as exc:
            print(f"\n########## {name} ##########")
            print(f"!! EXCEPTION {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
