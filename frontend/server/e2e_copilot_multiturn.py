"""Multi-turn, multi-task Copilot E2E tests against the real LLM + real databases.

Simulates one Copilot panel session faithfully: every turn carries the conversation transcript,
cross-turn copilot_memory (identity records projected from prior observations), simulated
confirmation receipts (applied / failed), and a context_payload that EVOLVES between turns the
way the real frontend's does (uploads become present, run blockers change). Scenarios interleave
several distinct user goals in one session, switch topics mid-stream, and return to the main
line afterwards — the way real users behave.
"""
from __future__ import annotations

import copy
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from e2e_copilot_matrix import build_assistant  # noqa: E402
from management_api.copilot import _compact_memory_records  # noqa: E402

RECENT_TURNS = 8


def build_conversation(history, injected_resolutions=None):
    """Mirror the frontend's buildCopilotConversationContext: recent visible messages + receipts."""
    visible = [m for m in history if m["role"] in ("user", "assistant")]
    recent = [
        {"role": m["role"], "at": m.get("at", ""), "content": m["content"][:1200]}
        for m in visible[-RECENT_TURNS:]
    ]
    older = visible[: max(0, len(visible) - RECENT_TURNS)]
    payload = {
        "compression": "summary_plus_recent" if older else "recent_only",
        "total_messages": len(visible),
        "summary": "; ".join(f"{m['role']}: {m['content'][:120]}" for m in older[-4:]),
        "recent_messages": recent,
    }
    resolutions = list(injected_resolutions or [])
    if resolutions:
        payload["recent_action_resolutions"] = resolutions
    return payload


class MultiturnSession:
    """Drives one Copilot panel session across turns with evolving context."""

    def __init__(self, assistant, context_type, base_payload):
        self.assistant = assistant
        self.context_type = context_type
        self.base_payload = base_payload
        self.history = []
        self.memory = []
        self.results = []

    def turn(self, user_text, *, payload_patch=None, receipts=None, expect=None, label=""):
        """Run one user turn. ``receipts`` simulates confirmation outcomes the user just clicked;
        ``payload_patch`` evolves the live context the way the real page state would."""
        display = label or user_text[:52]
        print(f"\n--- turn: {display}")
        payload = copy.deepcopy(self.base_payload)
        if payload_patch:
            self._deep_merge(payload, payload_patch)
        payload["copilot_conversation"] = build_conversation(self.history, receipts)
        if self.memory:
            payload["copilot_memory"] = self.memory
        started = time.time()
        try:
            result = self.assistant.plan_turn(
                context_type=self.context_type,
                context_payload=payload,
                user_id="e2e-user",
                username="e2e",
                content=user_text,
            )
        except Exception as exc:
            print(f"  !! EXCEPTION {type(exc).__name__}: {str(exc)[:140]}")
            result = {"state": "exception", "error": str(exc), "actions": [], "questions": [], "observations": []}
        self.history.append({"role": "user", "content": user_text, "at": "t"})
        self.history.append({"role": "assistant", "content": str(result.get("content") or ""), "at": "t"})

        actions = [a.get("id") for a in (result.get("actions") or [])]
        questions = [(q.get("kind"), q.get("text")) for q in (result.get("questions") or [])]
        sources = sorted({str(r.get("source") or "") for r in (result.get("observations") or [])})
        print(f"  state={result.get('state')} actions={actions} questions={questions} obs={sources}")
        print(f"  msg: {str(result.get('content') or '')[:170].replace(chr(10), ' ')}")

        # carry forward memory exactly like the frontend does (identity records only)
        obs_map = {}
        for index, record in enumerate(result.get("observations") or []):
            obs_map[f"prev:{index}"] = {
                "ok": True,
                "skill": record.get("source", ""),
                "values": [record],
            }
        if obs_map:
            self.memory = (_compact_memory_records(obs_map) or [])[:20] + self.memory
            self.memory = self.memory[:20]

        failures = []
        for name, check in (expect or []):
            try:
                ok = check(result)
            except Exception:
                ok = False
            if not ok:
                failures.append(name)
                print(f"  !! EXPECT FAILED: {name}")
        elapsed = time.time() - started
        print(f"  ({elapsed:.0f}s) {'PASS' if not failures else 'FAIL: ' + '; '.join(failures)}")
        self.results.append({"label": display, "ok": not failures, "failures": failures, "elapsed": elapsed})
        return result

    @staticmethod
    def _deep_merge(target, patch):
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                MultiturnSession._deep_merge(target[key], value)
            else:
                target[key] = value


def has_action(result, skill):
    return any(a.get("id") == skill for a in (result.get("actions") or []))


def has_question_kind(result, kind):
    return any(q.get("kind") == kind for q in (result.get("questions") or []))


def obs_sources(result):
    return {str(r.get("source") or "") for r in (result.get("observations") or [])}


def msg_has(result, token):
    return token.lower() in str(result.get("content") or "").lower()


def action_arg(result, skill, key):
    for action in result.get("actions") or []:
        if action.get("id") == skill:
            value = action.get("arguments") or {}
            for part in key.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            return value
    return None


# --------------------------------------------------------------------------- #
# Scenario A — one docking task, six interleaved turns, evolving environment
# --------------------------------------------------------------------------- #

def scenario_a(assistant):
    from e2e_copilot_matrix import TASK_DETAIL_AFFINITY

    session = MultiturnSession(assistant, "task_detail", TASK_DETAIL_AFFINITY)

    session.turn(
        "Fill in the human DHODH into the target",
        expect=[
            ("rcsb searched", lambda r: "rcsb" in obs_sources(r)),
            ("asks user to choose entry", lambda r: has_question_kind(r, "choice")),
        ],
        label="A1 target-by-name -> rcsb + choice",
    )

    # user answers the choice question with a concrete pick (frontend sends it as a user message)
    session.turn(
        "Use 9V34",
        payload_patch={"affinityUploads": {"targetFileName": "9V34.pdb", "targetUploaded": True}},
        expect=[
            ("applies the chosen structure (or resolves it first)",
             lambda r: has_action(r, "task_detail:apply_docking_target_structure") or "rcsb" in obs_sources(r)),
        ],
        label="A2 user picked 9V34 -> apply",
    )

    session.turn(
        "Good. By the way, what does DHODH do biologically? Just a quick answer.",
        expect=[
            ("answers the side topic (no docking actions)", lambda r: not has_action(r, "task_detail:apply_docking_target_structure")),
            ("mentions DHODH in the answer", lambda r: msg_has(r, "dhodh") or msg_has(r, "嘧啶") or msg_has(r, "pyrimidine")),
        ],
        label="A3 topic switch: biology side question",
    )

    session.turn(
        "OK back to the task. The ligand is brequinar.",
        payload_patch={"affinityUploads": {"ligandUploaded": True}},
        expect=[
            ("pubchem lookup or direct apply", lambda r: "pubchem" in obs_sources(r) or has_action(r, "task_detail:apply_docking_ligand_smiles")),
        ],
        label="A4 back to main line: ligand by name",
    )

    session.turn(
        "Set the seed to 7.",
        expect=[
            ("patches seed via parameter patch", lambda r: action_arg(r, "task_detail:apply_parameter_patch", "parameterPatch.seed") == 7),
        ],
        label="A5 parameter change: seed=7",
    )

    session.turn(
        "Can we run it now?",
        payload_patch={
            "runtime": {
                "displayTaskState": "DRAFT",
                "runDisabled": True,
                "runBlockedReason": "Dock mode requires a pocket box.",
            }
        },
        expect=[
            ("does not propose submit while blocked", lambda r: not has_action(r, "task_detail:submit_current")),
            ("names the actual blocker", lambda r: msg_has(r, "pocket") or msg_has(r, "box") or msg_has(r, "盒子")),
        ],
        label="A6 environment check: run blocked by pocket",
    )
    return session


# --------------------------------------------------------------------------- #
# Scenario B — a confirmed action FAILS; the next turn must recover
# --------------------------------------------------------------------------- #

def scenario_b(assistant):
    from e2e_copilot_matrix import TASK_DETAIL_AFFINITY

    session = MultiturnSession(assistant, "task_detail", TASK_DETAIL_AFFINITY)

    first = session.turn(
        "The ligand is brequinar, fill it in",
        expect=[("produces the ligand apply action", lambda r: has_action(r, "task_detail:apply_docking_ligand_smiles"))],
        label="B1 ligand apply proposed",
    )

    failed_receipt = [{
        "plan_id": "plan-b", "operation_id": "op-b",
        "skill": "task_detail:apply_docking_ligand_smiles", "label": "Set docking ligand (SMILES)",
        "status": "failed", "error": "Affinity binder SMILES are only supported for affinity tasks.",
    }]

    session.turn(
        "It failed, please try again",
        receipts=failed_receipt,
        expect=[
            ("does not silently declare done", lambda r: r.get("state") != "complete" or has_action(r, "task_detail:apply_docking_ligand_smiles")),
            ("re-proposes or diagnoses the failure",
             lambda r: has_action(r, "task_detail:apply_docking_ligand_smiles") or "pubchem" in obs_sources(r) or msg_has(r, "fail") or msg_has(r, "失败")),
        ],
        label="B2 failure receipt -> recovery attempt",
    )

    applied_receipt = [{
        "plan_id": "plan-b2", "operation_id": "op-b2",
        "skill": "task_detail:apply_docking_ligand_smiles", "label": "Set docking ligand (SMILES)",
        "status": "applied", "detail": "Ligand SMILES set.",
    }]

    session.turn(
        "Now what else is missing before I can run?",
        receipts=applied_receipt,
        payload_patch={"affinityUploads": {"ligandUploaded": True}},
        expect=[
            ("does not re-propose the applied operation", lambda r: not has_action(r, "task_detail:apply_docking_ligand_smiles")),
            ("points at the remaining gap (message or question)",
             lambda r: (msg_has(r, "target") or msg_has(r, "pocket") or msg_has(r, "靶"))
                       or any(("target" in str(q.get("text") or "").lower() or "pocket" in str(q.get("text") or "").lower())
                              for q in (r.get("questions") or []))),
        ],
        label="B3 applied receipt -> advance, no re-propose",
    )
    return session


# --------------------------------------------------------------------------- #
# Scenario C — prediction task + literature side quest + return to main line
# --------------------------------------------------------------------------- #

def scenario_c(assistant):
    from e2e_copilot_matrix import TASK_DETAIL_PREDICTION

    session = MultiturnSession(assistant, "task_detail", TASK_DETAIL_PREDICTION)

    session.turn(
        "Fill the sequence of human DHODH into the protein component",
        payload_patch={
            "draft": {"components": [{"id": "c1", "type": "protein", "label": "Protein 1", "sequence": "MAWRHLKKRAQDAVIILGGGGLLFASYLMATGDERFYAEHLMPTLQGAEF"}]},
            "runtime": {"runDisabled": False, "runBlockedReason": ""},
        },
        expect=[
            ("uniprot sourced or already-filled answer", lambda r: "uniprot" in obs_sources(r) or has_action(r, "task_detail:apply_parameter_patch")),
        ],
        label="C1 sequence filled",
    )

    session.turn(
        "Find me recent papers on DHODH inhibitors",
        expect=[
            ("pubmed used OR honest source-unavailable handling (never fabricated results)",
             lambda r: "pubmed" in obs_sources(r)
                       or msg_has(r, "unreachable") or msg_has(r, "timed out") or msg_has(r, "timeout")
                       or msg_has(r, "不可用") or has_question_kind(r, "confirm")),
            ("no fabricated citations", lambda r: r.get("state") != "complete" or "pubmed" in obs_sources(r)),
        ],
        label="C2 side quest: literature",
    )

    session.turn(
        "Back to the task: also compute the affinity between the protein and a ligand while predicting",
        expect=[
            ("patches affinity binding OR asks how to pair (no ligand component exists)",
             lambda r: action_arg(r, "task_detail:apply_parameter_patch", "parameterPatch.affinityBinding") is not None
                       or has_question_kind(r, "choice")),
        ],
        label="C3 return: enable affinity scoring",
    )
    return session


def report(name, session):
    ok = sum(1 for r in session.results if r["ok"])
    for r in session.results:
        print(f"  {r['label']}: {'PASS' if r['ok'] else 'FAIL ' + '; '.join(r['failures'])} ({r['elapsed']:.0f}s)")
    print(f"  {name}: {ok}/{len(session.results)}")
    return ok, len(session.results)


def main():
    assistant = build_assistant()
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    totals = [0, 0]
    if which in ("all", "a"):
        print("\n================ SCENARIO A: docking, six interleaved turns ================")
        s = scenario_a(assistant)
        totals[0], totals[1] = (a + b for a, b in zip(totals, report("A", s)))
    if which in ("all", "b"):
        print("\n================ SCENARIO B: failed confirmation -> recovery ================")
        s = scenario_b(assistant)
        totals[0], totals[1] = (a + b for a, b in zip(totals, report("B", s)))
    if which in ("all", "c"):
        print("\n================ SCENARIO C: prediction + side quest + return ================")
        s = scenario_c(assistant)
        totals[0], totals[1] = (a + b for a, b in zip(totals, report("C", s)))
    print(f"\nMULTITURN TOTAL: {totals[0]}/{totals[1]}")
    return 0 if totals[0] == totals[1] else 1


if __name__ == "__main__":
    sys.exit(main())
