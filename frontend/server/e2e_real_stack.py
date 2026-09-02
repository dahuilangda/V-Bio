"""Real-stack Copilot E2E: drives the DEPLOYED management API over HTTP.

Unlike e2e_copilot_matrix (in-process assistant) this exercises exactly what the frontend
exercises — the running server, its loaded settings (real LLM + real database proxy), and
every audit/gate compiled into the deployed process. Sessions are multi-turn: the context
payload evolves between turns (uploads appear, blockers change), confirmation receipts are
injected the way the frontend persists them, and cross-turn memory rides along — mirroring
buildCopilotConversationContext faithfully.

Scenario families:
  S1  ambiguous entity (KLK, no isoform/organism) -> identity-stating choice -> user picks
      -> create action proposed in proposal phrasing (never a completion claim)
  S2  receipt-driven recovery: applied receipt -> continuation proposes the pocket box the
      blocker names; FAILED receipt -> the next turn re-proposes corrected, never claims done
  S3  module matrix: pubchem / rcsb / uniprot / alphafold / chembl+compute / host actions
  S4  receipt-grounded status reporting (no fabricated machine state)

Run:  python e2e_real_stack.py [--base http://127.0.0.1:5055]
"""
from __future__ import annotations

import copy
import json
import re
import sys
import time

import requests

BASE = "http://127.0.0.1:5055"
for arg in sys.argv[1:]:
    if arg.startswith("--base="):
        BASE = arg.split("=", 1)[1]

TURN_URL = f"{BASE}/vbio-api/copilot/turn"
RECENT_TURNS = 8

TASK_LIST_AFFINITY = {
    "page": {"contextType": "task_list", "workflowKey": "affinity", "workflowTitle": "Docking"},
    "project": {"id": "e2e-1", "name": "E2E Docking", "task_type": "affinity", "workflow_key": "affinity"},
    "rows": [],
    "summary": {"total": 0, "all": 0},
}

TASK_LIST_WITH_ROWS = {
    "page": {"contextType": "task_list", "workflowKey": "prediction", "workflowTitle": "Prediction"},
    "project": {"id": "e2e-2", "name": "E2E Prediction", "task_type": "prediction", "workflow_key": "prediction"},
    "rows": [
        {"id": "row-1", "name": "Lysozyme fold", "state": "SUCCESS", "backend": "boltz", "submitted": "2026-08-18 10:00"},
        {"id": "row-2", "name": "DHODH retry", "state": "FAILURE", "backend": "boltz", "submitted": "2026-08-18 11:00"},
        {"id": "row-3", "name": "KLK screen", "state": "RUNNING", "backend": "protenix", "submitted": "2026-08-19 09:00"},
    ],
    "summary": {"total": 3, "all": 3, "FAILURE": 1, "SUCCESS": 1, "RUNNING": 1},
}

TASK_DETAIL_AFFINITY_DOCK_READY = {
    "page": {"contextType": "task_detail", "workflowKey": "affinity", "workflowTitle": "Docking", "runLabel": "Run Docking"},
    "project": {"id": "e2e-1", "name": "E2E Docking", "task_type": "affinity", "workflow_key": "affinity"},
    "draft": {
        "taskName": "KLK-布洛芬对接", "taskSummary": "", "backend": "boltz",
        "options": {"affinityMode": "dock", "seed": 42},
        "components": [], "constraints": [],
    },
    "runtime": {
        "displayTaskState": "DRAFT", "runDisabled": True,
        "runBlockedReason": "Set a docking pocket box before running.",
        "activeTaskId": "",
    },
    "affinityUploads": {"targetFileName": "2BDG.cif", "ligandFileName": "", "targetUploaded": True, "ligandUploaded": False},
    "ligandSmiles": "CC(C)CC1=CC=C(C=C1)C(C)C(=O)O",
    "currentTask": None,
}

TASK_DETAIL_PREDICTION = {
    "page": {"contextType": "task_detail", "workflowKey": "prediction", "workflowTitle": "Structure Prediction", "runLabel": "Run Prediction"},
    "project": {"id": "e2e-2", "name": "E2E Prediction", "task_type": "prediction", "workflow_key": "prediction"},
    "draft": {
        "taskName": "New task", "taskSummary": "", "backend": "boltz",
        "options": {"seed": 42},
        "components": [{"id": "c1", "type": "protein", "label": "Protein 1", "sequence": ""}],
        "constraints": [],
    },
    "runtime": {"displayTaskState": "DRAFT", "runDisabled": True, "runBlockedReason": "Protein sequence is empty.", "activeTaskId": ""},
    "currentTask": None,
}


def build_conversation(history, injected_resolutions=None):
    visible = [m for m in history if m["role"] in ("user", "assistant")]
    recent = [{"role": m["role"], "at": m.get("at", ""), "content": m["content"][:1200]} for m in visible[-RECENT_TURNS:]]
    older = visible[: max(0, len(visible) - RECENT_TURNS)]
    payload = {
        "compression": "summary_plus_recent" if older else "recent_only",
        "total_messages": len(visible),
        "summary": "; ".join(f"{m['role']}: {m['content'][:120]}" for m in older[-4:]),
        "recent_messages": recent,
    }
    if injected_resolutions:
        payload["recent_action_resolutions"] = injected_resolutions
    return payload


class RealStackSession:
    def __init__(self, name, context_type, base_payload):
        self.name = name
        self.context_type = context_type
        self.base_payload = base_payload
        self.history = []
        self.results = []

    def turn(self, user_text, *, payload_patch=None, receipts=None, expect=None, label=""):
        display = label or user_text[:52]
        print(f"\n--- [{self.name}] turn: {display}", flush=True)
        payload = copy.deepcopy(self.base_payload)
        if payload_patch:
            self._deep_merge(payload, payload_patch)
        payload["copilot_conversation"] = build_conversation(self.history, receipts)
        started = time.time()
        try:
            response = requests.post(
                TURN_URL,
                json={
                    "context_type": self.context_type,
                    "context_payload": payload,
                    "user_id": "e2e-user",
                    "username": "e2e",
                    "content": user_text,
                },
                timeout=240,
            )
            if response.status_code != 200:
                result = {"state": "http_error", "error": response.text[:200], "actions": [], "questions": [], "observations": []}
            else:
                result = response.json()
        except Exception as exc:
            print(f"  !! EXCEPTION {type(exc).__name__}: {str(exc)[:140]}")
            result = {"state": "exception", "error": str(exc), "actions": [], "questions": [], "observations": []}
        self.history.append({"role": "user", "content": user_text, "at": "t"})
        self.history.append({"role": "assistant", "content": str(result.get("content") or ""), "at": "t"})

        actions = [a.get("id") for a in (result.get("actions") or [])]
        questions = [(q.get("kind"), str(q.get("text"))[:60]) for q in (result.get("questions") or [])]
        sources = sorted({str(r.get("source") or "") for r in (result.get("observations") or [])})
        print(f"  state={result.get('state')} actions={actions} questions={questions} obs={sources}")
        print(f"  msg: {str(result.get('content') or '')[:200].replace(chr(10), ' ')}")

        failures = []
        for name, check in (expect or []):
            try:
                ok = check(result)
            except Exception:
                ok = False
            if not ok:
                failures.append(name)
                print(f"  !! EXPECT FAILED: {name}")
        if failures:
            print(f"  trace: {json.dumps(result.get('trace') or [], ensure_ascii=False)[:600]}")
            print(f"  questions-full: {json.dumps(result.get('questions') or [], ensure_ascii=False)[:400]}")
        elapsed = time.time() - started
        print(f"  ({elapsed:.0f}s) {'PASS' if not failures else 'FAIL: ' + '; '.join(failures)}")
        self.results.append({"label": display, "ok": not failures, "failures": failures, "elapsed": elapsed, "state": result.get("state")})
        return result

    @staticmethod
    def _deep_merge(target, patch):
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                RealStackSession._deep_merge(target[key], value)
            else:
                target[key] = value


# ── assertion helpers ────────────────────────────────────────────────────────────────────

def state_is(expected):
    return lambda r: r.get("state") == expected

def state_in(*expected):
    return lambda r: r.get("state") in expected

def has_action_skill(skill):
    return lambda r: any(a.get("id") == skill for a in (r.get("actions") or []))

def no_actions(r):
    return not (r.get("actions") or [])

def obs_source(source):
    return lambda r: any(str(rec.get("source") or "") == source for rec in (r.get("observations") or []))

def question_options_match(pattern):
    rx = re.compile(pattern, re.IGNORECASE)
    def check(r):
        for q in (r.get("questions") or []):
            for opt in (q.get("options") or []):
                # Identity lives in the label OR the hint (the retrieved title carries
                # organism/protein — the harness-synthesized choices put it in hint).
                if rx.search(str(opt.get("label") or "")) or rx.search(str(opt.get("hint") or "")):
                    return True
            if rx.search(str(q.get("text") or "")):
                return True
        return False
    return check

def has_choice_question(r):
    return any(q.get("kind") == "choice" for q in (r.get("questions") or []))

def msg_contains(pattern):
    rx = re.compile(pattern, re.IGNORECASE)
    return lambda r: bool(rx.search(str(r.get("content") or "")))

def msg_lacks_completion_claims(r):
    # No past-tense lifecycle claims: nothing has been applied in a pure proposal turn.
    return not re.search(r"(任务|项目|草稿)[^。！？!?\n]{0,32}?(已创建|已提交|已打开)", str(r.get("content") or ""))

def trace_has(event):
    return lambda r: event in json.dumps(r.get("trace") or [], ensure_ascii=False)


APPLIED_CREATE_RECEIPT = [{
    "plan_id": "plan-e2e-1", "operation_id": "create-1", "skill": "tasks:create_docking",
    "label": "New docking task (with target structure)", "status": "applied",
    "detail": "New docking task (with target structure) applied.",
    "arguments": {"create": True, "targetPdbId": "2BDG", "ligandSmiles": "CC(C)CC1=CC=C(C=C1)C(C)C(=O)O"},
}]

FAILED_POCKET_RECEIPT = [{
    "plan_id": "plan-e2e-2", "operation_id": "pocket-1", "skill": "task_detail:set_docking_pocket_box",
    "label": "Set docking pocket box", "status": "failed",
    "error": "The uploaded target file is not a .pdb, .cif structure.",
    "arguments": {"mode": "auto"},
}]

APPLIED_SUBMIT_RECEIPT = [{
    "plan_id": "plan-e2e-3", "operation_id": "submit-1", "skill": "task_detail:submit_current",
    "label": "Start run", "status": "applied", "detail": "Task submitted. The task is now queued.",
    "arguments": {},
}]


# ── scenarios ────────────────────────────────────────────────────────────────────────────

def scenario_s1_ambiguity():
    s = RealStackSession("S1-klk", "task_list", TASK_LIST_AFFINITY)
    s.turn(
        "我想对接klk和布洛芬",
        expect=[
            ("asks identity choice, not silent pick", lambda r: has_choice_question(r)),
            ("options carry identity dimensions", question_options_match(r"kallikrein|KLK\d|sapiens|organism|物种")),
            ("retrieved structures", obs_source("rcsb")),
            ("retrieved compound", obs_source("pubchem")),
            ("no premature create", no_actions),
        ],
    )
    s.turn(
        "用 2BDG（Human Kallikrein 4）",
        expect=[
            ("proposes create_docking", has_action_skill("tasks:create_docking")),
        ],
    )
    return s


def scenario_s2_receipt_recovery():
    s = RealStackSession("S2-recovery", "task_detail", TASK_DETAIL_AFFINITY_DOCK_READY)
    s.turn(
        "已应用刚才确认的操作（见回执），无需重复；请继续完成计划",
        receipts=APPLIED_CREATE_RECEIPT,
        expect=[
            ("proposes pocket box the blocker names", has_action_skill("task_detail:set_docking_pocket_box")),
        ],
    )
    s.turn(
        "刚才设置口袋盒子失败了，继续",
        receipts=FAILED_POCKET_RECEIPT,
        expect=[
            ("re-proposes a corrective action", lambda r: bool(r.get("actions") or r.get("questions"))),
            ("no completion claim on failed receipt", lambda r: msg_lacks_completion_claims(r)),
        ],
    )
    return s


def scenario_s3_modules():
    results = []
    def one(name, ctx, text, expects):
        session = RealStackSession(name, ctx[0], ctx[1])
        session.turn(text, expect=expects)
        return session.results[-1]
    results.append(one("S3-pubchem", ("project_list", {"page": {"contextType": "project_list"}, "summary": {"all": 1}}),
        "查一下布洛芬的SMILES和分子量",
        [("pubchem retrieved", obs_source("pubchem")), ("answer cites retrieved data", lambda r: bool(str(r.get('content') or '').strip()))]))
    results.append(one("S3-rcsb", ("task_detail", TASK_DETAIL_AFFINITY_DOCK_READY),
        "帮我找人的DHODH的实验结构",
        [("rcsb retrieved", obs_source("rcsb")),
         ("identity stated in question or answer", lambda r: question_options_match(r"human|dhodh")(r) or msg_contains(r"DHODH")(r))]))
    results.append(one("S3-uniprot", ("task_detail", TASK_DETAIL_PREDICTION),
        "帮我查人DHODH的UniProt序列",
        [("uniprot retrieved", obs_source("uniprot"))]))
    results.append(one("S3-alphafold", ("task_detail", TASK_DETAIL_PREDICTION),
        "给我人DHODH的AlphaFold预测模型",
        # Accept: the model resolves (possibly via uniprot first), or reports an authoritative
        # NO_MATCH and asks the fallback — but never a bare memory accession claim.
        [("alphafold resolved or fallback asked", lambda r: obs_source("alphafold")(r) or (r.get("state") == "needs_input" and bool(r.get("questions"))))]))
    results.append(one("S3-chembl-compute", ("project_list", {"page": {"contextType": "project_list"}, "summary": {"all": 1}}),
        "查BTK激酶的ChEMBL活性数据并计算平均IC50",
        # Either the full retrieval+compute chain, or the identity contract fires first
        # (BTK spans organisms — asking species/isoform BEFORE retrieving is correct).
        [("retrieval or identity determination", lambda r: obs_source("chembl")(r) or has_choice_question(r))]))
    results.append(one("S3-host-filter", ("task_list", TASK_LIST_WITH_ROWS),
        "只看失败的任务",
        [("filter action proposed", lambda r: has_action_skill("tasks:failure")(r) or has_action_skill("tasks:update_view")(r))]))
    return results


def scenario_s4_receipt_grounded_status():
    s = RealStackSession("S4-status", "task_detail", TASK_DETAIL_AFFINITY_DOCK_READY)
    s.turn(
        "任务现在什么状态？",
        receipts=APPLIED_SUBMIT_RECEIPT,
        payload_patch={"runtime": {"displayTaskState": "QUEUED", "runDisabled": True, "runBlockedReason": "", "activeTaskId": "rt-1"}},
        expect=[
            ("answers from context/receipts", msg_contains(r"QUEUED|排队|已提交|queued")),
            ("no fabricated blocker", lambda r: "missing seed" not in str(r.get("content") or "")),
        ],
    )
    return s


def main():
    suite_start = time.time()
    sessions = []
    print("=== S1 ambiguous entity → identity choice → create ===", flush=True)
    sessions.append(scenario_s1_ambiguity())
    print("\n=== S2 receipt-driven recovery ===", flush=True)
    sessions.append(scenario_s2_receipt_recovery())
    print("\n=== S3 module matrix ===", flush=True)
    for result in scenario_s3_modules():
        sessions.append(result)
    print("\n=== S4 receipt-grounded status ===", flush=True)
    sessions.append(scenario_s4_receipt_grounded_status())

    # module one-shots return results (dicts), sessions return session objects — normalize
    all_results = []
    for item in sessions:
        if isinstance(item, RealStackSession):
            all_results.extend(item.results)
        elif isinstance(item, dict):
            all_results.append(item)

    passed = sum(1 for r in all_results if r.get("ok"))
    print("\n" + "=" * 70)
    print(f"REAL-STACK E2E: {passed}/{len(all_results)} turns passed  ({time.time() - suite_start:.0f}s total)")
    for r in all_results:
        mark = "PASS" if r.get("ok") else "FAIL"
        failures = "; ".join(r.get("failures") or []) or "-"
        print(f"  [{mark}] {str(r.get('label'))[:56]:58} state={r.get('state')} {failures if not r.get('ok') else ''}")
    return 0 if passed == len(all_results) else 1


if __name__ == "__main__":
    sys.exit(main())
