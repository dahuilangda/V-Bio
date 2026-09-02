"""Large-scale real-stack CHAOS suite: complex multi-turn sessions against the deployed API.

Each session interleaves SEVERAL goals with distractions, mind changes, ambiguous references,
and injected failure receipts — the way real users behave. The invariants asserted every turn:

  INV1 grounding — answers cite retrieved data; fabricated identifiers never ship (audits).
  INV2 identity — entity choices state their dimensions; unstated dimensions are asked.
  INV3 honesty — completion claims only with applied receipts; failures re-proposed corrected.
  INV4 recovery — after a failed receipt the next turn proposes a corrective path, never
       repeats the failed operation verbatim, never claims done.
  INV5 relevance — after a distraction the assistant returns to the user's main line.

Run:  python e2e_real_stack_chaos.py
"""
from __future__ import annotations

import re
import sys
import time

from e2e_real_stack import (
    RealStackSession,
    TASK_LIST_AFFINITY,
    TASK_LIST_WITH_ROWS,
    TASK_DETAIL_AFFINITY_DOCK_READY,
    TASK_DETAIL_PREDICTION,
    has_action_skill,
    has_choice_question,
    msg_contains,
    no_actions,
    obs_source,
    question_options_match,
)

RETRACT_CREATE = [{
    "plan_id": "chaos-1", "operation_id": "create-1", "skill": "tasks:create_docking",
    "label": "New docking task (with target structure)", "status": "applied",
    "detail": "New docking task (with target structure) applied.",
    "arguments": {"create": True, "targetPdbId": "2BDG", "ligandSmiles": "CC(C)CC1=CC=C(C=C1)C(C)C(=O)O"},
}]

POCKET_FAILED = [{
    "plan_id": "chaos-2", "operation_id": "pocket-1", "skill": "task_detail:set_docking_pocket_box",
    "label": "Set docking pocket box", "status": "failed",
    "error": "No atoms could be parsed from the target structure.",
    "arguments": {"mode": "auto"},
}]

POCKET_APPLIED = [{
    "plan_id": "chaos-3", "operation_id": "pocket-2", "skill": "task_detail:set_docking_pocket_box",
    "label": "Set docking pocket box", "status": "applied",
    "detail": "Pocket box set around the co-crystallized ligand.",
    "arguments": {"mode": "auto"},
}]

RENAME_FAILED = [{
    "plan_id": "chaos-4", "operation_id": "rename-1", "skill": "tasks:rename",
    "label": "Rename task", "status": "failed",
    "error": "Could not find the task referenced by Copilot.",
    "arguments": {"taskRowId": "row-9", "taskName": "KLK 对接 v2"},
}]


def session_interleaved_docking():
    """Goal A (dock KLK+ibuprofen) interrupted by goal B (aspirin lookup), a chit-chat
    distractor, an ambiguous reference, then back to A with a mind change."""
    s = RealStackSession("chaos-A", "task_list", TASK_LIST_AFFINITY)
    s.turn("我想对接klk和布洛芬", expect=[
        ("A1 asks identity determination", lambda r: has_choice_question(r) or obs_source("rcsb")(r)),
    ])
    s.turn("等等，先帮我查一下阿司匹林的分子量和SMILES", expect=[
        ("B retrieves the compound", lambda r: obs_source("pubchem")(r) or has_choice_question(r)),
    ])
    s.turn("好的。对了今天天气怎么样？", expect=[
        ("chit-chat handled without actions", lambda r: no_actions(r)),
    ])
    s.turn("继续刚才的事", expect=[
        ("returns to the docking line", lambda r: has_choice_question(r) or obs_source("rcsb")(r) or has_action_skill("tasks:create_docking")(r)),
    ])
    s.turn("用人的那个", expect=[
        ("resolves the ambiguous reference to a concrete proposal or grounded question",
         lambda r: has_action_skill("tasks:create_docking")(r) or has_choice_question(r)),
    ])
    s.turn("不要这个，换 2BDH", expect=[
        ("mind change honored", lambda r: has_action_skill("tasks:create_docking")(r) or has_choice_question(r)),
    ])
    return s


def session_recovery_chain():
    """Continuation → pocket fails → recover → pocket applied → submit proposed; a failed
    rename on a stale row id mid-flow; a vague status question at the end."""
    s = RealStackSession("chaos-B", "task_detail", TASK_DETAIL_AFFINITY_DOCK_READY)
    s.turn("已应用刚才确认的操作（见回执），无需重复；请继续完成计划", receipts=RETRACT_CREATE, expect=[
        ("continuation proposes the blocker's resolution", has_action_skill("task_detail:set_docking_pocket_box")),
    ])
    s.turn("继续", receipts=POCKET_FAILED, expect=[
        ("INV4 recovers from the failed pocket", lambda r: bool(r.get("actions") or r.get("questions")) or r.get("state") == "complete"),
        ("INV3 no completion claim on failed receipt",
         lambda r: not re.search(r"(已设置|已应用|成功)[^。！？]{0,20}(口袋|pocket)", str(r.get("content") or ""), re.I)),
    ])
    s.turn(
        "好了",
        receipts=POCKET_APPLIED,
        # The context must evolve with the receipt (the real frontend clears the blocker
        # once the pocket is set) — otherwise the model correctly re-proposes the pocket.
        payload_patch={"runtime": {"displayTaskState": "DRAFT", "runDisabled": False, "runBlockedReason": "", "activeTaskId": ""}},
        expect=[
            ("applied pocket advances the chain", lambda r: has_action_skill("task_detail:submit_current")(r) or bool(r.get("questions")) or r.get("state") == "complete"),
        ],
    )
    s.turn("帮我把这个任务改名叫 KLK 对接 v2", expect=[
        ("rename proposed for the current task", lambda r: has_action_skill("task_detail:apply_metadata_patch")(r) or bool(r.get("questions"))),
    ])
    s.turn("刚才改名失败了，再试一次", receipts=RENAME_FAILED, expect=[
        ("INV4 rename failure retried or explained", lambda r: bool(r.get("actions") or r.get("questions"))),
    ])
    s.turn("现在怎么样了？", expect=[
        ("vague status answered from context", lambda r: bool(str(r.get("content") or "").strip())),
    ])
    return s


def session_prediction_with_distractions():
    """Prediction flow with sequence fill, a template request, list-management distractions,
    and a multi-thing turn (rename + lookup in one message)."""
    s = RealStackSession("chaos-C", "task_detail", TASK_DETAIL_PREDICTION)
    s.turn("帮我把目标蛋白设为人的溶菌酶，然后建个预测任务", expect=[
        ("sequence sourced from uniprot", lambda r: obs_source("uniprot")(r) or has_choice_question(r)),
    ])
    s.turn("再给它加一个模板结构，用实验结构就行", expect=[
        ("template path taken", lambda r: obs_source("rcsb")(r) or has_choice_question(r) or has_action_skill("task_detail:apply_structure_template")(r)),
    ])
    s.turn("顺便把种子改成 7，再把任务名改成 Lysozyme v2", expect=[
        ("multi-thing turn handled", lambda r: bool(r.get("actions")) or bool(r.get("questions"))),
    ])
    s.turn("算了，模板不要了，就要序列", expect=[
        ("mind change acknowledged", lambda r: r.get("state") in ("complete", "await_confirmation", "needs_input")),
    ])
    return s


def session_vague_questions():
    """Underspecified asks: anaphora, comparative without candidates, typo'd compound,
    mixed-language entity."""
    s = RealStackSession("chaos-D", "task_list", TASK_LIST_WITH_ROWS)
    s.turn("这个项目现在怎么样？", expect=[
        ("context-anchored stats answer", lambda r: bool(str(r.get("content") or "").strip())),
    ])
    s.turn("哪个更好？", expect=[
        ("underspecified comparative asks back", has_choice_question),
    ])
    s.turn("帮我查查 ibuprofin", expect=[
        ("typo tolerated via retrieval or clarification", lambda r: obs_source("pubchem")(r) or bool(r.get("questions"))),
    ])
    s.turn("失败的那个任务怎么回事？", expect=[
        ("targets the visible FAILURE row", lambda r: msg_contains(r"DHODH|失败|FAILURE|row-2")(r) or bool(r.get("questions"))),
    ])
    return s


def main():
    started = time.time()
    sessions = [
        session_interleaved_docking(),
        session_recovery_chain(),
        session_prediction_with_distractions(),
        session_vague_questions(),
    ]
    total = passed = 0
    print("\n" + "=" * 70)
    print(f"CHAOS REAL-STACK: results by session")
    for session in sessions:
        session_passed = sum(1 for r in session.results if r["ok"])
        total += len(session.results)
        passed += session_passed
        print(f"  [{session.name}] {session_passed}/{len(session.results)}")
        for r in session.results:
            if not r["ok"]:
                print(f"    FAIL {r['label']}: {'; '.join(r['failures'])}")
    print(f"TOTAL: {passed}/{total} turns passed ({time.time() - started:.0f}s)")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
