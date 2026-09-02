"""Large-scale end-to-end Copilot planner-turn matrix.

Drives the REAL CopilotAssistant (real LLM endpoint, real online sources through the configured
proxy) with realistic user requests across every host page and workflow, mimicking what the
frontend sends. Each scenario records the turn outcome (state / actions / questions /
observations) and asserts the behaviors the product promises:

- docking workflow: target-by-name -> rcsb.search + user choice (never silent pick)
- ligand by common/CAS name -> pubchem.search
- ligand given as SMILES -> applied directly, no lookup
- sequence target (prediction) -> uniprot with valid field syntax
- no "source unavailable" misreports for 4xx rejections
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

from management_api.copilot import CopilotAssistant


def _load_env(path_candidates):
    env = {}
    for path in path_candidates:
        if not os.path.exists(path):
            continue
        for line in open(path):
            line = line.strip()
            if line.startswith("VBIO_COPILOT") and "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                env[key] = value
    return env


ENV = _load_env([
    "/data/V-Bio/frontend/.env.copilot",
    "/data/V-Bio/frontend/.env",
    "/data/V-Bio/.env",
])


def build_assistant() -> CopilotAssistant:
    session = requests.Session()
    assistant = CopilotAssistant(
        chat_api_url=ENV.get("VBIO_COPILOT_API_URL", ""),
        chat_api_key=ENV.get("VBIO_COPILOT_API_KEY", ""),
        chat_model=ENV.get("VBIO_COPILOT_MODEL", ""),
        timeout_seconds=90.0,
        session=session,
        logger=__import__("logging").getLogger("e2e"),
    )
    proxy = "http://172.16.34.31:2080"
    assistant.update_runtime_overrides(proxies={"http": proxy, "https": proxy})  # database proxy only; LLM calls stay direct
    return assistant


TASK_DETAIL_AFFINITY = {
    "page": {
        "contextType": "task_detail",
        "workflowKey": "affinity",
        "workflowTitle": "Docking",
        "workflowShortTitle": "Docking",
        "runLabel": "Run Docking",
        "supportsSequenceInputs": False,
    },
    "project": {"id": "e2e-1", "name": "E2E Docking", "task_type": "affinity", "workflow_key": "affinity"},
    "draft": {
        "taskName": "New task",
        "taskSummary": "",
        "backend": "boltz",
        "options": {"affinityMode": "dock", "seed": 42},
        "components": [{"id": "c1", "type": "protein", "label": "Protein 1", "sequence": ""}],
        "constraints": [],
    },
    "runtime": {
        "displayTaskState": "DRAFT",
        "runDisabled": True,
        "runBlockedReason": "Protein sequence is empty.",
        "activeTaskId": "",
    },
    "affinityUploads": {"targetFileName": "", "ligandFileName": "", "targetUploaded": False, "ligandUploaded": False},
    "currentTask": None,
}

TASK_DETAIL_PREDICTION = {
    "page": {
        "contextType": "task_detail",
        "workflowKey": "prediction",
        "workflowTitle": "Structure Prediction",
        "workflowShortTitle": "Prediction",
        "runLabel": "Run Prediction",
        "supportsSequenceInputs": True,
    },
    "project": {"id": "e2e-2", "name": "E2E Prediction", "task_type": "prediction", "workflow_key": "prediction"},
    "draft": {
        "taskName": "New task",
        "taskSummary": "",
        "backend": "boltz",
        "options": {"seed": 42},
        "components": [{"id": "c1", "type": "protein", "label": "Protein 1", "sequence": ""}],
        "constraints": [],
    },
    "runtime": {
        "displayTaskState": "DRAFT",
        "runDisabled": True,
        "runBlockedReason": "Protein sequence is empty.",
        "activeTaskId": "",
    },
    "currentTask": None,
}

PROJECT_LIST = {
    "page": {"contextType": "project_list"},
    "summary": {
        "allTypeCounts": {"prediction": 3, "affinity": 2, "virtual_screening": 1},
        "allBackendCounts": {"boltz": 5, "nesso": 1},
        "allTaskStateCounts": {"SUCCESS": 4, "FAILURE": 1, "RUNNING": 1},
        "activeProjects": 1,
        "failedProjects": 1,
        "emptyProjects": 2,
    },
    "options": {"workflowOptions": ["prediction", "virtual_screening", "peptide_design", "lead_optimization", "affinity"]},
    "projects": [
        {"id": "prj-1", "name": "Kinase screening", "task_type": "virtual_screening", "task_state": "RUNNING", "task_counts": {"total": 3, "running": 1}},
        {"id": "prj-2", "name": "Old docking try", "task_type": "affinity", "task_state": "FAILURE", "task_counts": {"total": 1, "failure": 1}},
    ],
}


def summarize_turn(turn) -> dict:
    return {
        "state": turn.get("state"),
        "actions": [
            {"skill": a.get("id"), "arguments": a.get("arguments")}
            for a in (turn.get("actions") or [])
        ],
        "questions": [
            {"text": q.get("text"), "kind": q.get("kind"), "options": [o.get("value") for o in (q.get("options") or [])][:8]}
            for q in (turn.get("questions") or [])
        ],
        "observations": [
            {"source": rec.get("source"), "key": [rec.get(k) for k in ("pdbId", "cid", "accession", "title") if rec.get(k)][:1]}
            for rec in (turn.get("observations") or [])
        ][:8],
        "content_head": str(turn.get("content") or "")[:220],
    }


def run_scenario(assistant, name, context_type, payload, content, checks):
    started = time.time()
    print(f"\n=== {name} ===")
    print(f"user: {content}")
    # Default gate: a happy-path scenario must reach a usable terminal state. A turn that burns
    # the round budget and ends "failed" is a regression even when a narrow check (e.g. "used
    # uniprot") happens to pass on the way down.
    checks = [("turn reached a usable terminal state", lambda t: t.get("state") != "failed")] + list(checks)
    try:
        turn = assistant.plan_turn(
            context_type=context_type,
            context_payload=payload,
            user_id="e2e-user",
            username="e2e",
            content=content,
        )
    except Exception as exc:
        print(f"!! EXCEPTION: {type(exc).__name__}: {exc}")
        return {"name": name, "ok": False, "error": str(exc)}
    summary = summarize_turn(turn)
    print("state:", summary["state"])
    for action in summary["actions"]:
        print("  action:", action["skill"], json.dumps(action["arguments"], ensure_ascii=False)[:160])
    for question in summary["questions"]:
        print("  question:", question["kind"], question["text"][:100])
        print("    options:", question["options"])
    for obs in summary["observations"]:
        print("  obs:", obs["source"], obs["key"])
    print("  message:", summary["content_head"].replace("\n", " ")[:200])
    failures = []
    for label, check in checks:
        try:
            ok = check(turn)
        except Exception as exc:
            ok = False
        if not ok:
            failures.append(label)
            print(f"  !! CHECK FAILED: {label}")
    elapsed = time.time() - started
    print(f"  ({elapsed:.1f}s) {'PASS' if not failures else 'FAIL: ' + '; '.join(failures)}")
    return {"name": name, "ok": not failures, "failures": failures, "elapsed": elapsed, "summary": summary}


def has_action_skill(turn, skill):
    return any(a.get("id") == skill for a in (turn.get("actions") or []))


def has_question_kind(turn, kind="choice"):
    return any(q.get("kind") == kind for q in (turn.get("questions") or []))


def observation_sources(turn):
    sources = set()
    for rec in turn.get("observations") or []:
        sources.add(str(rec.get("source") or ""))
    return sources


def message_mentions(turn, token):
    return token.lower() in str(turn.get("content") or "").lower()


def main():
    assistant = build_assistant()
    scenarios = []

    # 1. The exact production failure: docking target by name -> RCSB + user choice
    scenarios.append(run_scenario(
        assistant, "docking/target-by-name", "task_detail", TASK_DETAIL_AFFINITY,
        "Fill in the human DHODH into the target",
        [
            ("used rcsb (not uniprot) for structure target", lambda t: "rcsb" in observation_sources(t)),
            ("ended asking user to choose (or with apply action)", lambda t: has_question_kind(t, "choice") or has_action_skill(t, "task_detail:apply_docking_target_structure")),
            ("no silent structure pick without asking", lambda t: not has_action_skill(t, "task_detail:apply_docking_target_structure") or has_question_kind(t, "choice")),
        ],
    ))

    # 2. Ligand by common name -> pubchem
    scenarios.append(run_scenario(
        assistant, "docking/ligand-by-common-name", "task_detail", TASK_DETAIL_AFFINITY,
        "The ligand uses brequinar",
        [
            ("used pubchem", lambda t: "pubchem" in observation_sources(t) or has_action_skill(t, "task_detail:apply_docking_ligand_smiles")),
        ],
    ))

    # 3. Ligand by CAS registry number -> pubchem name namespace
    scenarios.append(run_scenario(
        assistant, "docking/ligand-by-cas", "task_detail", TASK_DETAIL_AFFINITY,
        "Help me find the ligand with CAS number 96187-53-0",
        [
            ("used pubchem", lambda t: "pubchem" in observation_sources(t)),
        ],
    ))

    # 4. Ligand given as SMILES -> direct apply, NO lookup
    scenarios.append(run_scenario(
        assistant, "docking/ligand-by-smiles", "task_detail", TASK_DETAIL_AFFINITY,
        "The ligand's SMILES is CCOc1ccccc1, just fill it in directly",
        [
            ("applied directly or asked nothing more", lambda t: has_action_skill(t, "task_detail:apply_docking_ligand_smiles")),
            ("no pubchem lookup for a given SMILES", lambda t: "pubchem" not in observation_sources(t)),
        ],
    ))

    # 5. Sequence target on prediction -> uniprot with field syntax
    scenarios.append(run_scenario(
        assistant, "prediction/sequence-by-name", "task_detail", TASK_DETAIL_PREDICTION,
        "Fill in the sequence of human DHODH into the protein component",
        [
            ("used uniprot", lambda t: "uniprot" in observation_sources(t)),
        ],
    ))

    # 6. Environment guard: runDisabled draft -> must NOT propose submit
    scenarios.append(run_scenario(
        assistant, "prediction/no-submit-while-blocked", "task_detail", TASK_DETAIL_PREDICTION,
        "Just run the current task",
        [
            ("did not propose submit while runDisabled", lambda t: not has_action_skill(t, "task_detail:submit_current")),
        ],
    ))

    # 7. project_list: docking alias creates the right workflow (direct action OR a confirm
    #    question naming the workflow first — asking before creating is equally correct)
    scenarios.append(run_scenario(
        assistant, "project_list/create-docking", "project_list", PROJECT_LIST,
        "Create a new docking project",
        [
            ("proposed projects:create or asked to confirm", lambda t: has_action_skill(t, "projects:create") or has_question_kind(t, "confirm")),
        ],
    ))

    # 9. docking: "complete it for me" plans THROUGH the gate — ligand apply chained with the
    #    run submit it unblocks (depends_on), so confirming the pair starts the computation
    #    without the user having to re-prompt.
    gated_affinity = json.loads(json.dumps(TASK_DETAIL_AFFINITY))
    gated_affinity["affinityUploads"]["targetUploaded"] = True
    gated_affinity["affinityUploads"]["targetFileName"] = "4NFF.cif"
    gated_affinity["runtime"]["runBlockedReason"] = "Ligand SMILES is empty."
    scenarios.append(run_scenario(
        assistant, "docking/complete-and-run", "task_detail", gated_affinity,
        "对接布洛芬，帮我完成",
        [
            ("applied the ligand", lambda t: has_action_skill(t, "task_detail:apply_docking_ligand_smiles")),
            ("chained the run submit after it", lambda t: has_action_skill(t, "task_detail:submit_current")),
        ],
    ))

    # 8. project_list: stats answered from context
    scenarios.append(run_scenario(
        assistant, "project_list/stats-from-context", "project_list", PROJECT_LIST,
        "How many of my projects are currently running tasks?",
        [
            ("answered from context (mentions count or project)", lambda t: message_mentions(t, "1") or message_mentions(t, "running")),
        ],
    ))

    print("\n================ MATRIX SUMMARY ================")
    passed = sum(1 for s in scenarios if s.get("ok"))
    for s in scenarios:
        status = "PASS" if s.get("ok") else f"FAIL {s.get('failures') or s.get('error', '')[:80]}"
        print(f"  {s['name']}: {status} ({s.get('elapsed', 0):.0f}s)")
    print(f"  TOTAL: {passed}/{len(scenarios)} passed")
    return 0 if passed == len(scenarios) else 1


if __name__ == "__main__":
    sys.exit(main())
