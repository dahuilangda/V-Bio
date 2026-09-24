"""Workflow-specific environment tests: virtual_screening, affinity, lead_optimization.

These tests drive the FULL planner loop with REAL frontend payload shapes for the less-covered
workflows, and pin two architectural contracts:

1. PLANNER ENVIRONMENT AWARENESS: the planner must plan according to the environment it is in —
   the workflow's real parameter keys, the page's actual available actions, and the task's
   real state. It must not propose operations that the environment does not support.
2. HARNESS CAPABILITY-BOUNDARY AUDIT: the harness must reject any operation that crosses the
   registered capability boundary — unknown skills, params not in the workflow schema, mixed
   read/write phases, dependency violations.

3. ATOMIC OPERATIONS: the planner must emit one atomic operation per unit of work, never a fused
   multi-step shortcut.
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from management_api.copilot import CopilotAssistant
from management_api.copilot_skill_harness import CopilotSkillDefinition, CopilotSkillHarness
from management_api.copilot_skills.online_databases import OnlineSkillDefinition
from tests.helpers import FakeResponse, NullLogger, is_phase2_request


class SequenceModelSession:
    def __init__(self, responses: List[str]) -> None:
        self.responses = list(responses)
        self.requests: List[Dict[str, Any]] = []

    def post(self, url: str, headers: Dict[str, str] | None = None, json: Any = None, timeout: float | None = None, **kwargs: Any) -> FakeResponse:
        self.requests.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if not self.responses:
            if is_phase2_request(json):
                # Phase-2 enrichment is best-effort: an unscripted free-text call returns empty
                # content so the assistant falls back to the audited planner message.
                return FakeResponse("")
            raise AssertionError("Unexpected model request.")
        return FakeResponse(self.responses.pop(0))


def make_assistant(responses: List[Any]) -> Tuple[CopilotAssistant, SequenceModelSession]:
    session = SequenceModelSession([r if isinstance(r, str) else json.dumps(r) for r in responses])
    assistant = CopilotAssistant(
        chat_api_url="http://model.invalid/v1/chat/completions",
        chat_api_key="test-key", chat_model="test-model",
        timeout_seconds=3, session=session, logger=NullLogger(), max_planner_rounds=8,
    )
    return assistant, session


def _turn(message: str, operations: List[Dict[str, Any]] | None = None,
          questions: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    return {"message": message, "questions": questions or [], "operations": operations or []}


def _read_op(op_id: str, skill: str, arguments: Dict[str, Any], depends: List[str] | None = None) -> Dict[str, Any]:
    return {"id": op_id, "skill": skill, "arguments": arguments, "depends_on": depends or []}


def _write_op(op_id: str, skill: str, arguments: Dict[str, Any], depends: List[str] | None = None) -> Dict[str, Any]:
    return _read_op(op_id, skill, arguments, depends)


def conv_context(*pairs: Tuple[str, str]) -> Dict[str, Any]:
    recent = [{"role": role, "content": content} for role, content in pairs]
    return {"copilot_conversation": {"recent_messages": recent, "compression": "recent_only", "total_messages": len(recent)}}


def patch_host_skills(*skills: CopilotSkillDefinition):
    return patch("management_api.copilot.build_cross_context_skill_definitions", return_value=list(skills))


def inject_read_skills(assistant: CopilotAssistant, skill_map: Dict[str, Tuple[OnlineSkillDefinition, Any]]) -> None:
    """Register stub read skills through the PUBLIC registry API only.

    The harness derives its read-skill catalog live from the registry (no construction-time
    snapshot), so one register() call makes the skill both executable and auditable — the
    registry is the single source of truth.
    """
    for name, (definition, handler) in skill_map.items():
        assistant.skill_harness.skills.register(definition, handler)
def vs_task_detail_payload(
    *,
    task_state: str = "SUCCESS",
    run_blocked_reason: str = "",
) -> Dict[str, Any]:
    """Virtual screening task_detail payload (Nesso-1 fixed backend)."""
    return {
        "page": {"contextType": "task_detail", "workflowKey": "virtual_screening", "workflowTitle": "Virtual Screening", "runLabel": "Run Screening",
                 "availableActions": ["analyze_current_context", "plan_confirmed_parameter_patch", "plan_confirmed_submit", "plan_confirmed_cancel_current_task"]},
        "project": {"id": "vs-proj", "name": "VS Project", "task_type": "virtual_screening", "workflow_key": "virtual_screening"},
        "draft": {"taskName": "VS run", "taskSummary": "", "backend": "nesso", "options": {"seed": 7},
                  "components": [{"index": 0, "type": "protein", "sequenceLength": 350}], "constraints": []},
        "runtime": {"displayTaskState": task_state, "runDisabled": bool(run_blocked_reason),
                    "runBlockedReason": run_blocked_reason, "authoritativeTaskState": task_state},
        "currentTask": {"id": "vs-row", "name": "VS run", "task_state": task_state, "backend": "nesso",
                        "confidenceSummary": {"avgPlddt": 0.71, "iptm": 0.6}, "affinitySummary": {},
                        "components": [{"index": 0, "type": "protein", "sequenceLength": 350}], "error_text": ""},
    }


def affinity_task_detail_payload(
    *,
    task_state: str = "SUCCESS",
    run_blocked_reason: str = "",
    has_ligand: bool = True,
) -> Dict[str, Any]:
    """Affinity task_detail payload (affinityMode + seed params, ligand/target skills)."""
    return {
        "page": {"contextType": "task_detail", "workflowKey": "affinity", "workflowTitle": "Affinity Scoring", "runLabel": "Run",
                 "availableActions": ["analyze_current_context", "plan_confirmed_parameter_patch", "plan_confirmed_submit", "plan_confirmed_cancel_current_task", "plan_confirmed_delete_current_task"]},
        "project": {"id": "aff-proj", "name": "Affinity Project", "task_type": "affinity", "workflow_key": "affinity"},
        "draft": {"taskName": "Affinity run", "taskSummary": "", "backend": "boltz", "options": {"affinityMode": "score", "seed": 1},
                  "components": [{"index": 0, "type": "protein", "sequenceLength": 300}, {"index": 1, "type": "ligand"}], "constraints": []},
        "runtime": {"displayTaskState": task_state, "runDisabled": bool(run_blocked_reason),
                    "runBlockedReason": run_blocked_reason, "authoritativeTaskState": task_state},
        "currentTask": {"id": "aff-row", "name": "Affinity run", "task_state": task_state, "backend": "boltz",
                        "confidenceSummary": {}, "affinitySummary": {"interfaceMetric": 0.62},
                        "components": [{"index": 0, "type": "protein", "sequenceLength": 300}, {"index": 1, "type": "ligand"}], "error_text": ""},
    }


def lead_opt_task_detail_payload(*, task_state: str = "SUCCESS") -> Dict[str, Any]:
    """Lead optimization task_detail payload (no parameter patch skill — boundary test)."""
    return {
        "page": {"contextType": "task_detail", "workflowKey": "lead_optimization", "workflowTitle": "Lead Optimization", "runLabel": "Run",
                 "availableActions": ["analyze_current_context", "plan_confirmed_submit", "plan_confirmed_cancel_current_task"]},
        "project": {"id": "lo-proj", "name": "LO Project", "task_type": "lead_optimization", "workflow_key": "lead_optimization"},
        "draft": {"taskName": "LO run", "taskSummary": "", "backend": "boltz", "options": {},
                  "components": [{"index": 0, "type": "protein", "sequenceLength": 400}], "constraints": []},
        "runtime": {"displayTaskState": task_state, "runDisabled": False, "runBlockedReason": "", "authoritativeTaskState": task_state},
        "currentTask": {"id": "lo-row", "name": "LO run", "task_state": task_state, "backend": "boltz",
                        "confidenceSummary": {}, "affinitySummary": {},
                        "components": [{"index": 0, "type": "protein", "sequenceLength": 400}], "error_text": ""},
    }


# --------------------------------------------------------------------------- #
# 1. Virtual Screening environment
# --------------------------------------------------------------------------- #


class VirtualScreeningScenarioTests(unittest.TestCase):
    def test_vs_analysis_cites_backend_and_result(self):
        payload = vs_task_detail_payload()
        responses = [
            _turn("VS 任务完成，Nesso-1 后端，平均 pLDDT 0.71。"),
            "**Virtual Screening** 任务已完成，使用 **Nesso-1** 后端。平均 pLDDT **0.71**，ipTM 0.6。",
        ]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                     user_id="u", username="alice", content="分析这个筛选任务")
        self.assertEqual(result["state"], "complete")
        self.assertTrue("nesso" in result["content"].lower() or "Nesso" in result["content"])

    def test_vs_seed_change_uses_workflow_param(self):
        from management_api.copilot_capabilities import build_task_detail_skill_definitions
        d = next(s for s in build_task_detail_skill_definitions("virtual_screening") if s.name == "task_detail:apply_parameter_patch")
        payload = vs_task_detail_payload()
        responses = [_turn("更新 seed。", operations=[
            _write_op("p", "task_detail:apply_parameter_patch", {"parameterPatch": {"seed": 42}}),
        ])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="seed 改成 42")
        self.assertEqual(result["actions"][0]["arguments"]["parameterPatch"], {"seed": 42})

    def test_vs_backend_must_be_nesso_only(self):
        # The environment fixes backend to nesso — a planner proposing another backend must be
        # rejected by the harness schema (enum constraint).
        from management_api.copilot_capabilities import build_task_detail_skill_definitions
        d = next(s for s in build_task_detail_skill_definitions("virtual_screening") if s.name == "task_detail:apply_parameter_patch")
        payload = vs_task_detail_payload()
        responses = [
            _turn("改后端。", operations=[
                _write_op("p", "task_detail:apply_parameter_patch", {"parameterPatch": {"backend": "boltz"}}),
            ]),
            _turn("虚拟筛选仅支持 Nesso-1 后端。"),
            "虚拟筛选仅支持 Nesso-1 后端。",
        ]
        assistant, session = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="换 boltz 后端")
        # The invalid backend is rejected by the schema audit; the model recovers honestly.
        self.assertEqual(result["state"], "complete")
        self.assertTrue(_has_feedback(session, 1, "must be one of"))


def _has_feedback(session: SequenceModelSession, round_index: int, marker: str) -> bool:
    for msg in session.requests[round_index]["json"]["messages"]:
        if marker in str(msg.get("content") or ""):
            return True
    return False


# --------------------------------------------------------------------------- #
# 2. Affinity environment
# --------------------------------------------------------------------------- #


class AffinityScenarioTests(unittest.TestCase):
    def test_affinity_mode_change(self):
        from management_api.copilot_capabilities import build_task_detail_skill_definitions
        d = next(s for s in build_task_detail_skill_definitions("affinity") if s.name == "task_detail:apply_parameter_patch")
        payload = affinity_task_detail_payload()
        responses = [_turn("切换模式。", operations=[
            _write_op("p", "task_detail:apply_parameter_patch", {"parameterPatch": {"affinityMode": "pose"}}),
        ])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="换成 pose 模式")
        self.assertEqual(result["actions"][0]["arguments"]["parameterPatch"]["affinityMode"], "pose")

    def test_affinity_ligand_smiles_skill_is_atomic(self):
        # The ligand SMILES skill is a distinct atomic operation — planner must not fuse it
        # into the parameter patch.
        from management_api.copilot_capabilities import build_task_detail_skill_definitions
        d = next(s for s in build_task_detail_skill_definitions("affinity") if s.name == "task_detail:apply_docking_ligand_smiles")
        payload = affinity_task_detail_payload(has_ligand=False)
        responses = [_turn("设置配体。", operations=[
            _write_op("l", "task_detail:apply_docking_ligand_smiles", {"smiles": "CCO"}),
        ])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="配体用 CCO")
        self.assertEqual(len(result["actions"]), 1)
        self.assertEqual(result["actions"][0]["id"], "task_detail:apply_docking_ligand_smiles")
        self.assertEqual(result["actions"][0]["arguments"]["smiles"], "CCO")

    def test_affinity_run_blocked_without_ligand(self):
        payload = affinity_task_detail_payload(has_ligand=False, run_blocked_reason="Add a ligand SMILES to run")
        responses = [
            _turn("当前无法运行：需要添加配体 SMILES。"),
            "当前任务**无法运行**：Add a ligand SMILES to run。需要先设置配体 SMILES 才能提交。",
        ]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                     user_id="u", username="alice", content="为什么不能运行")
        self.assertEqual(result["state"], "complete")
        self.assertIn("ligand", result["content"].lower())


# --------------------------------------------------------------------------- #
# 3. Lead Optimization boundary
# --------------------------------------------------------------------------- #


class LeadOptimizationScenarioTests(unittest.TestCase):
    def test_lo_has_no_parameter_patch_skill(self):
        # The environment exposes NO apply_parameter_patch skill — the planner cannot propose
        # parameter changes it has no tool for. This pins the capability boundary.
        from management_api.copilot_capabilities import build_task_detail_skill_definitions
        defs = build_task_detail_skill_definitions("lead_optimization")
        names = [d.name for d in defs]
        self.assertNotIn("task_detail:apply_parameter_patch", names)
        self.assertIn("task_detail:submit_current", names)

    def test_lo_analysis_from_context(self):
        payload = lead_opt_task_detail_payload()
        responses = [
            _turn("先导优化任务已完成，boltz 后端。"),
            "**Lead Optimization** 任务已完成，使用 boltz 后端。",
        ]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                     user_id="u", username="alice", content="分析这个先导优化任务")
        self.assertEqual(result["state"], "complete")
        self.assertTrue("lead" in result["content"].lower() or "先导" in result["content"])


# --------------------------------------------------------------------------- #
# 4. Harness capability-boundary audit
# --------------------------------------------------------------------------- #


class HarnessBoundaryAuditTests(unittest.TestCase):
    def test_unknown_skill_rejected(self):
        from management_api.copilot_capabilities import build_task_detail_skill_definitions
        d = next(s for s in build_task_detail_skill_definitions("peptide_design") if s.name == "task_detail:submit_current")
        payload = {
            "page": {"contextType": "task_detail", "workflowKey": "peptide_design"},
            "project": {"id": "p", "task_type": "peptide_design", "workflow_key": "peptide_design"},
            "currentTask": {"id": "r", "name": "T", "task_state": "SUCCESS", "backend": "boltz", "confidenceSummary": {}, "affinitySummary": {}, "components": [], "error_text": ""},
        }
        responses = [
            _turn("执行。", operations=[_write_op("x", "nonexistent.skill", {})]),
            _turn("没有这个操作，无法执行。"),
            "没有这个操作，无法执行。",
        ]
        assistant, session = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="做点什么")
        self.assertTrue(_has_feedback(session, 1, "is not registered"))

    def test_param_not_in_workflow_schema_rejected(self):
        # peptide_design schema has no 'affinityMode' — proposing it must be rejected by the
        # harness schema audit (additionalProperties: false).
        from management_api.copilot_capabilities import build_task_detail_skill_definitions
        d = next(s for s in build_task_detail_skill_definitions("peptide_design") if s.name == "task_detail:apply_parameter_patch")
        payload = {
            "page": {"contextType": "task_detail", "workflowKey": "peptide_design"},
            "project": {"id": "p", "task_type": "peptide_design", "workflow_key": "peptide_design"},
            "currentTask": {"id": "r", "name": "T", "task_state": "SUCCESS", "backend": "boltz", "confidenceSummary": {}, "affinitySummary": {}, "components": [], "error_text": ""},
        }
        responses = [
            _turn("改参数。", operations=[
                _write_op("bad", "task_detail:apply_parameter_patch", {"parameterPatch": {"affinityMode": "pose"}}),
            ]),
            _turn("affinityMode 不适用于多肽设计任务。"),
            "affinityMode 不适用于多肽设计任务。",
        ]
        assistant, session = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="改成 pose 模式")
        self.assertTrue(_has_feedback(session, 1, "is not declared"))

    def test_mixed_read_write_phase_rejected(self):
        # Read and write operations in the same round must be rejected (phase separation).
        rd = OnlineSkillDefinition(name="eval.read", description="Read.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False})
        d = CopilotSkillDefinition(name="eval.write", label="Write", description="Write.",
            input_schema={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False},
            effect="update")
        payload = {"page": {"contextType": "task_detail", "workflowKey": "prediction"}, "project": {"id": "p", "task_type": "prediction"}}
        responses = [
            _turn("混发。", operations=[_read_op("r", "eval.read", {}), _write_op("w", "eval.write", {"value": "x"})]),
            _turn("分开操作。", operations=[_write_op("w", "eval.write", {"value": "x"})]),
        ]
        assistant, session = make_assistant(responses)
        inject_read_skills(assistant, {"eval.read": (rd, lambda a: {"value": "x"})})
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="查一下然后改")
        self.assertTrue(_has_feedback(session, 1, "HELD"))

    def test_dependency_on_failed_observation_rejected(self):
        # A write that depends on a FAILED read must be rejected — cannot consume failed data.
        rd = OnlineSkillDefinition(name="eval.fail", description="Fails.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False})
        d = CopilotSkillDefinition(name="eval.write", label="Write", description="Write.",
            input_schema={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False},
            effect="update")
        payload = {"page": {"contextType": "task_detail", "workflowKey": "prediction"}, "project": {"id": "p", "task_type": "prediction"}}
        responses = [
            _turn("先读。", operations=[_read_op("r", "eval.fail", {})]),
            # Write consumes the FAILED read's data via $fromObservation — must be rejected by
            # the harness audit (materialization refuses to resolve failed observations).
            _turn("写。", operations=[_write_op("w", "eval.write", {"value": {"$fromObservation": "r", "field": "value"}}, depends=["r"])]),
            # Model recovers honestly after the rejection.
            _turn("读取失败，无法基于该数据写入。"),
        ]
        assistant, session = make_assistant(responses)
        def failing(args): raise RuntimeError("source down")
        inject_read_skills(assistant, {"eval.fail": (rd, failing)})
        write_calls: List[Dict[str, Any]] = []
        with patch_host_skills(d):
            assistant.skill_harness.skills.register(
                CopilotSkillDefinition(name="eval.write", label="Write", description="Write.",
                    input_schema={"type": "object", "properties": {"value": {"type": "string"}},
                                  "required": ["value"], "additionalProperties": False},
                    effect="update"),
                lambda args: write_calls.append(args) or {"ok": True},
            )
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="读然后写")
        # Rejection feedback must reach the planner: consuming a failed observation is refused.
        self.assertTrue(_has_feedback(session, 2, "failed skill calls"))
        # The write must never have executed — the audit stopped it before any handler ran.
        self.assertEqual(write_calls, [])
        # The failure is reported honestly; the turn still completes with a real answer.
        self.assertEqual(result["state"], "complete")
        self.assertIn("读取失败", result["content"])

    def test_dependency_on_failed_observation_rejected_via_depends_on(self):
        # A write that depends_on a FAILED read without consuming its data must also be rejected —
        # the dependency audit fires even when $fromObservation is not used.
        rd = OnlineSkillDefinition(name="eval.fail", description="Fails.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False})
        d = CopilotSkillDefinition(name="eval.write", label="Write", description="Write.",
            input_schema={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False},
            effect="update")
        payload = {"page": {"contextType": "task_detail", "workflowKey": "prediction"}, "project": {"id": "p", "task_type": "prediction"}}
        responses = [
            _turn("先读。", operations=[_read_op("r", "eval.fail", {})]),
            # Literal arguments, but the ordering dependency points at the FAILED read.
            _turn("写。", operations=[_write_op("w", "eval.write", {"value": "x"}, depends=["r"])]),
            _turn("读取失败，无法继续。"),
        ]
        assistant, session = make_assistant(responses)
        def failing(args): raise RuntimeError("source down")
        inject_read_skills(assistant, {"eval.fail": (rd, failing)})
        write_calls: List[Dict[str, Any]] = []
        with patch_host_skills(d):
            assistant.skill_harness.skills.register(
                CopilotSkillDefinition(name="eval.write", label="Write", description="Write.",
                    input_schema={"type": "object", "properties": {"value": {"type": "string"}},
                                  "required": ["value"], "additionalProperties": False},
                    effect="update"),
                lambda args: write_calls.append(args) or {"ok": True},
            )
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="读然后写")
        # The depends_on audit names the failed observation explicitly.
        self.assertTrue(_has_feedback(session, 2, "references a failed observation: r"))
        self.assertEqual(write_calls, [])
        self.assertEqual(result["state"], "complete")

    def test_failed_lookup_report_warns_against_id_reuse(self):
        # The USER-reported failure mode: UniProt is down, the planner retries and REUSES the
        # failed operation id, the audit rejects it, and the model repeats the mistake until the
        # consecutive-rejection check breaks the turn ("I could not complete this request").
        # The harness must PREVENT this by telling the planner on the FIRST failure that a retry
        # needs a NEW id — the failed id cannot be reused.
        rd = OnlineSkillDefinition(name="eval.fail", description="Fails.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False})
        payload = {"page": {"contextType": "task_detail", "workflowKey": "prediction"}, "project": {"id": "p", "task_type": "prediction"}}
        responses = [
            # Round 1: read fails (source down). The harness report must carry the id-reuse warning.
            _turn("查一下。", operations=[_read_op("r", "eval.fail", {})]),
            # Round 2: the (weak) planner retries by REUSING the same id — but now with the warning
            # in the report it should instead learn to use a NEW id.
            _turn("重试。", operations=[_read_op("r", "eval.fail", {})]),
            # Round 3: corrected retry with a new id.
            _turn("换 id 重试。", operations=[_read_op("r2", "eval.fail", {})]),
            # Round 4: source still down — harness must now say "do not retry; tell the user".
            _turn("如实告知。"),
            "UniProt 源暂时不可用，请稍后重试或提供 accession 直接解析。",
        ]
        assistant, session = make_assistant(responses)
        def failing(args): raise RuntimeError("source down")
        inject_read_skills(assistant, {"eval.fail": (rd, failing)})
        with patch_host_skills():
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="查一下这个蛋白")
        self.assertEqual(result["state"], "complete")
        # The first failure report carried the id-reuse warning and the second carried the
        # "do not retry" escalation (all echoed inside system messages of later requests).
        escalation_seen = any(
            "failed 2 times in a row" in str(msg.get("content") or "")
            for req in session.requests for msg in req["json"]["messages"]
        )
        self.assertTrue(escalation_seen, "harness must escalate after consecutive failures")
        # And the id-reuse warning was present in the first failure report.
        reuse_warning_seen = any(
            "NEW operation id" in str(msg.get("content") or "") and "cannot be reused" in str(msg.get("content") or "")
            for req in session.requests for msg in req["json"]["messages"]
        )
        self.assertTrue(reuse_warning_seen, "harness must warn that the failed id cannot be reused")

    def test_repeated_failure_ends_in_honest_source_unavailable_report(self):
        # If the planner keeps retrying a dead source with NEW ids, the harness must escalate:
        # after N consecutive failures the report says "do not retry again — tell the user".
        rd = OnlineSkillDefinition(name="eval.fail", description="Fails.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False})
        payload = {"page": {"contextType": "task_detail", "workflowKey": "prediction"}, "project": {"id": "p", "task_type": "prediction"}}
        responses = [
            _turn("查。", operations=[_read_op("r1", "eval.fail", {})]),
            _turn("再查。", operations=[_read_op("r2", "eval.fail", {})]),
            _turn("源不可用，已告知用户。"),
            "数据源暂时不可用，请稍后再试。",
        ]
        assistant, session = make_assistant(responses)
        def failing(args): raise RuntimeError("source down")
        inject_read_skills(assistant, {"eval.fail": (rd, failing)})
        with patch_host_skills():
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="查蛋白")
        self.assertEqual(result["state"], "complete")
        self.assertIn("不可用", result["content"])
        # The second failure round carried the escalation.
        second_round_text = "".join(
            str(msg.get("content") or "")
            for msg in session.requests[2]["json"]["messages"]
        )
        self.assertIn("failed 2 times in a row", second_round_text)
        self.assertIn("Do NOT retry it again", second_round_text)


# --------------------------------------------------------------------------- #
# 5. Environment changes across multi-turn conversations
# --------------------------------------------------------------------------- #


class EnvironmentChangeAcrossTurnsTests(unittest.TestCase):
    """The planner must notice when the environment changes between turns (task state, options)."""

    def test_task_state_changes_from_running_to_success(self):
        # Turn 1: task RUNNING. Turn 2: task SUCCESS. The planner must answer per the CURRENT state.
        ctx = conv_context(
            ("user", "任务什么状态"), ("assistant", "当前运行中。"),
        )
        # Turn 2 payload shows SUCCESS (the task finished between turns).
        payload = {
            "page": {"contextType": "task_detail", "workflowKey": "peptide_design"},
            "project": {"id": "p", "task_type": "peptide_design", "workflow_key": "peptide_design"},
            "currentTask": {"id": "r", "name": "CD73", "task_state": "SUCCESS", "backend": "boltz",
                            "confidenceSummary": {"avgPlddt": 0.59, "iptm": 0.73}, "affinitySummary": {},
                            "components": [{"index": 0, "type": "protein", "sequenceLength": 524}], "error_text": ""},
        }
        responses = [
            _turn("任务已成功完成，ipTM 0.73。"),
            "任务**已成功完成**，ipTM **0.73**。",
        ]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(context_type="task_detail", context_payload={**ctx, **payload},
                                     user_id="u", username="alice", content="现在呢")
        self.assertEqual(result["state"], "complete")
        self.assertTrue("成功" in result["content"] or "SUCCESS" in result["content"].upper())
        self.assertNotIn("运行中", result["content"])

    def test_option_changed_in_draft_between_turns(self):
        # Turn 1 said binder=17. Turn 2's draft shows binder=25 (user changed it manually).
        # The planner must reflect the CURRENT draft, not the previous turn's claim.
        ctx = conv_context(
            ("user", "肽长多少"), ("assistant", "当前肽长 17。"),
        )
        payload = {
            "page": {"contextType": "task_detail", "workflowKey": "peptide_design"},
            "project": {"id": "p", "task_type": "peptide_design", "workflow_key": "peptide_design"},
            "draft": {"taskName": "CD73", "backend": "boltz", "options": {"peptideBinderLength": 25}, "components": [], "constraints": []},
            "currentTask": {"id": "r", "name": "CD73", "task_state": "DRAFT", "backend": "boltz",
                            "confidenceSummary": {}, "affinitySummary": {}, "components": [], "error_text": ""},
        }
        responses = [
            _turn("当前草稿的肽长是 25。"),
            "当前草稿的肽长是 **25**。",
        ]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(context_type="task_detail", context_payload={**ctx, **payload},
                                     user_id="u", username="alice", content="那现在肽长是多少")
        self.assertEqual(result["state"], "complete")
        self.assertIn("25", result["content"])


# --------------------------------------------------------------------------- #
# 6. Atomic operations: no fused shortcuts
# --------------------------------------------------------------------------- #


class AtomicOperationTests(unittest.TestCase):
    def test_catalogs_contain_no_fused_skills(self):
        # The fused 'patch-and-submit' skill was removed in the atomicity refactor. Pin the
        # catalogs: every action skill is a single atomic unit — nothing named or described as
        # combining two effects (patch+submit, copy+run, etc.) may return.
        from management_api.copilot_capabilities import (
            build_task_detail_skill_definitions,
            build_cross_context_skill_definitions,
        )
        catalogs = {
            "peptide_design": build_task_detail_skill_definitions("peptide_design"),
            "affinity": build_task_detail_skill_definitions("affinity"),
            "virtual_screening": build_task_detail_skill_definitions("virtual_screening"),
            "lead_optimization": build_task_detail_skill_definitions("lead_optimization"),
            "project_list": build_cross_context_skill_definitions(
                current_context="project_list", context_payload={"page": {"contextType": "project_list"}}, workflow_key=""),
            "task_list": build_cross_context_skill_definitions(
                current_context="task_list", context_payload={"page": {"contextType": "task_list"}}, workflow_key=""),
        }
        fused_markers = ("patch_and_submit", "patch+submit", "and submit", "and execute")
        for catalog_name, definitions in catalogs.items():
            for definition in definitions:
                name = definition.name
                description = str(definition.description or "").lower()
                self.assertNotIn("patch_and_submit", name, f"{catalog_name}: fused skill {name}")
                for marker in fused_markers:
                    self.assertNotIn(marker, description, f"{catalog_name}: fused description on {name}")

    def test_submit_requires_existing_draft_not_fused(self):
        # The planner must not emit a fused 'patch-and-submit'. It emits apply_parameter_patch
        # then submit_current as SEPARATE operations when the user wants both.
        from management_api.copilot_capabilities import build_task_detail_skill_definitions
        defs = build_task_detail_skill_definitions("peptide_design")
        patch_d = next(s for s in defs if s.name == "task_detail:apply_parameter_patch")
        submit_d = next(s for s in defs if s.name == "task_detail:submit_current")
        payload = {
            "page": {"contextType": "task_detail", "workflowKey": "peptide_design"},
            "project": {"id": "p", "task_type": "peptide_design", "workflow_key": "peptide_design"},
            "draft": {"taskName": "T", "backend": "boltz", "options": {"peptideBinderLength": 20}, "components": [], "constraints": []},
            "currentTask": {"id": "r", "name": "T", "task_state": "DRAFT", "backend": "boltz", "confidenceSummary": {}, "affinitySummary": {}, "components": [], "error_text": ""},
        }
        # The planner correctly emits TWO atomic operations (patch, then submit with depends_on).
        responses = [_turn("改肽长并提交。", operations=[
            _write_op("patch", "task_detail:apply_parameter_patch", {"parameterPatch": {"peptideBinderLength": 25}}),
            _write_op("submit", "task_detail:submit_current", {}, depends=["patch"]),
        ])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(patch_d, submit_d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="肽长改成 25 然后提交")
        self.assertEqual(result["state"], "await_confirmation")
        self.assertEqual(len(result["actions"]), 2)
        ids = [a["id"] for a in result["actions"]]
        self.assertEqual(ids, ["task_detail:apply_parameter_patch", "task_detail:submit_current"])
        self.assertIn("patch", result["actions"][1]["payload"]["dependsOn"])

    def test_outline_cross_step_dependency_rejected(self):
        # Outline steps are themselves ordered: a later step must NOT depends_on an earlier
        # step's operation id (that id is not an observation and not a same-round operation,
        # so the dependency audit rejects it). The planner corrects by dropping the dependency.
        from management_api.copilot_capabilities import build_task_detail_skill_definitions
        defs = build_task_detail_skill_definitions("peptide_design")
        patch_d = next(s for s in defs if s.name == "task_detail:apply_parameter_patch")
        submit_d = next(s for s in defs if s.name == "task_detail:submit_current")
        payload = {"page": {"contextType": "task_detail", "workflowKey": "peptide_design"}, "project": {"id": "p"}}
        responses = [
            {"message": "两步计划。", "questions": [], "operations": [],
             "goal_steps": [{"description": "改参数"}, {"description": "提交"}]},
            _turn("第一步。", operations=[_write_op("patch", "task_detail:apply_parameter_patch", {"parameterPatch": {"peptideBinderLength": 25}})]),
            _turn("第二步。", operations=[_write_op("submit", "task_detail:submit_current", {}, depends=["patch"])]),
            _turn("第二步（修正）。", operations=[_write_op("submit", "task_detail:submit_current", {})]),
        ]
        assistant, session = make_assistant(responses)
        with patch_host_skills(patch_d, submit_d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="改参数并提交")
        self.assertEqual(result["state"], "await_confirmation")
        ids = [a["operation_id"] for a in result["actions"]]
        self.assertEqual(ids, ["patch", "submit"])
        # The cross-step dependency was rejected with the unknown-prior-operation message
        # (the rejection feedback is carried on the request AFTER the offending round).
        self.assertTrue(_has_feedback(session, 3, "unknown operation"))

    def test_same_mistake_in_later_outline_step_is_recoverable(self):
        # The planner fixed a structural mistake in step 1, then made the SAME mistake again in
        # step 2. That is a fixable error in a NEW context — the loop must let the model fix it,
        # NOT treat it as repeated-rejection non-convergence (which would fail the whole plan).
        from management_api.copilot_capabilities import build_task_detail_skill_definitions
        defs = build_task_detail_skill_definitions("peptide_design")
        patch_d = next(s for s in defs if s.name == "task_detail:apply_parameter_patch")
        submit_d = next(s for s in defs if s.name == "task_detail:submit_current")
        payload = {"page": {"contextType": "task_detail", "workflowKey": "peptide_design"}, "project": {"id": "p"}}

        def op_missing(oid, skill, args):
            # Deliberately no depends_on key — the audit rejects this.
            return {"id": oid, "skill": skill, "arguments": args}

        def op_fixed(oid, skill, args):
            return {"id": oid, "skill": skill, "arguments": args, "depends_on": []}

        responses = [
            {"message": "两步计划。", "questions": [], "operations": [],
             "goal_steps": [{"description": "改参数"}, {"description": "提交"}]},
            _turn("第一步。", operations=[op_missing("patch", "task_detail:apply_parameter_patch", {"parameterPatch": {"peptideBinderLength": 25}})]),
            _turn("第一步修正。", operations=[op_fixed("patch", "task_detail:apply_parameter_patch", {"parameterPatch": {"peptideBinderLength": 25}})]),
            _turn("第二步。", operations=[op_missing("submit", "task_detail:submit_current", {})]),
            _turn("第二步修正。", operations=[op_fixed("submit", "task_detail:submit_current", {})]),
        ]
        assistant, session = make_assistant(responses)
        with patch_host_skills(patch_d, submit_d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="改参数并提交")
        self.assertEqual(result["state"], "await_confirmation")
        self.assertEqual([a["operation_id"] for a in result["actions"]], ["patch", "submit"])
        # Both steps' rejections were fed back; the turn did NOT fail early.
        self.assertEqual(len(session.requests), 5)

    def test_consecutive_identical_rejections_still_fail_honestly(self):
        # The model repeats the SAME rejected output on CONSECUTIVE rounds without an accepted
        # round in between — genuine non-convergence, must end in state=failed (never a
        # fabricated success).
        from management_api.copilot_capabilities import build_task_detail_skill_definitions
        defs = build_task_detail_skill_definitions("peptide_design")
        patch_d = next(s for s in defs if s.name == "task_detail:apply_parameter_patch")
        payload = {"page": {"contextType": "task_detail", "workflowKey": "peptide_design"}, "project": {"id": "p"}}
        responses = [
            _turn("改。", operations=[{"id": "x", "skill": "task_detail:apply_parameter_patch",
                                       "arguments": {"parameterPatch": {"peptideBinderLength": 25}}}]),
            _turn("再犯。", operations=[{"id": "x", "skill": "task_detail:apply_parameter_patch",
                                         "arguments": {"parameterPatch": {"peptideBinderLength": 25}}}]),
            # The convergence-aid round grants one more attempt; the model repeats the identical
            # rejection a third time — only then does the loop break to honest failure.
            _turn("还犯。", operations=[{"id": "x", "skill": "task_detail:apply_parameter_patch",
                                         "arguments": {"parameterPatch": {"peptideBinderLength": 25}}}]),
        ]
        assistant, session = make_assistant(responses)
        with patch_host_skills(patch_d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="改参数")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["actions"], [])
        # Honest failure in the USER'S language (Chinese turn → Chinese message): names the
        # outcome (nothing applied) instead of demanding a rephrase.
        self.assertIn("没能", result["content"])
        self.assertIn("没有对你做任何修改", result["content"])

    def test_from_observation_invalid_index_rejected(self):
        # A $fromObservation index that is not a number must be rejected — never silently
        # treated as record 0 (a hard filter would mask the planner's contract error).
        rd = OnlineSkillDefinition(name="eval.read", description="Read.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False})
        d = CopilotSkillDefinition(name="eval.write", label="Write", description="Write.",
            input_schema={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False},
            effect="update")
        payload = {"page": {"contextType": "task_detail", "workflowKey": "prediction"}, "project": {"id": "p", "task_type": "prediction"}}
        responses = [
            _turn("先读。", operations=[_read_op("r", "eval.read", {})]),
            _turn("写。", operations=[_write_op("w", "eval.write", {"value": {"$fromObservation": "r", "field": "value", "index": "first"}}, depends=["r"])]),
            _turn("修正。", operations=[_write_op("w", "eval.write", {"value": {"$fromObservation": "r", "field": "value", "index": 0}}, depends=["r"])]),
        ]
        assistant, session = make_assistant(responses)
        inject_read_skills(assistant, {"eval.read": (rd, lambda args: {"value": "x"})})
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="读然后写")
        self.assertEqual(result["state"], "await_confirmation")
        # The rejection feedback reaches the planner on the round after the bad write (the
        # read round happens first, so the feedback is on request index 2, not 1).
        self.assertTrue(_has_feedback(session, 2, "invalid record index"))
        self.assertEqual(result["actions"][0]["arguments"]["value"], "x")


# --------------------------------------------------------------------------- #
# 6b. Observation classification: SUCCESS / NO_MATCH / FAILED — no fake success
# --------------------------------------------------------------------------- #


class ObservationClassificationTests(unittest.TestCase):
    """The three-state outcome audit must never report an empty result as SUCCESS."""

    @staticmethod
    def _observation(values: List[Dict[str, Any]], *, ok: bool = True) -> Dict[str, Any]:
        return {"skill": "s", "values": values, "ok": ok, "count": len(values), "successCount": len(values)}

    def test_resolve_shaped_record_is_success(self):
        obs = self._observation([{"accession": "P00533", "organism": "Homo sapiens"}])
        self.assertEqual(CopilotSkillHarness.classify_observation(obs), "SUCCESS")

    def test_search_shaped_nonempty_results_is_success(self):
        obs = self._observation([{"results": [{"accession": "P00533"}]}])
        self.assertEqual(CopilotSkillHarness.classify_observation(obs), "SUCCESS")

    def test_search_shaped_empty_results_is_no_match(self):
        # An authoritative empty list must NOT be reported as SUCCESS — the planner would
        # otherwise believe data was retrieved when the source answered "nothing found".
        obs = self._observation([{"results": []}])
        self.assertEqual(CopilotSkillHarness.classify_observation(obs), "NO_MATCH")

    def test_search_shaped_null_results_is_no_match(self):
        # {"results": null} is a search-shaped payload without records — not a retrieved
        # record, and NOT a success. (Previously misclassified as SUCCESS.)
        obs = self._observation([{"results": None}])
        self.assertEqual(CopilotSkillHarness.classify_observation(obs), "NO_MATCH")

    def test_empty_record_is_no_match(self):
        # An empty {} value carries no data — it must not count as retrieved.
        obs = self._observation([{}])
        self.assertEqual(CopilotSkillHarness.classify_observation(obs), "NO_MATCH")

    def test_no_values_is_no_match(self):
        obs = self._observation([])
        self.assertEqual(CopilotSkillHarness.classify_observation(obs), "NO_MATCH")

    def test_failed_execution_is_failed(self):
        obs = self._observation([{"results": [{"accession": "P00533"}]}], ok=False)
        self.assertEqual(CopilotSkillHarness.classify_observation(obs), "FAILED")


class OutlinePhaseTwoTests(unittest.TestCase):
    """A pure-analysis outline (zero operations, zero lookups) ends with a phase-2 enriched answer."""

    def test_pure_analysis_outline_enriches_final_message(self):
        # The last step's short JSON-constrained message must be enriched by phase-2 generation,
        # just like the non-outline pure-answer path — no truncated one-liner for outlined plans.
        d = CopilotSkillDefinition(name="task_detail:submit_current", label="Start run",
            description="Submit the current task for execution.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect="execute", context_type="task_detail")
        payload = {"page": {"contextType": "task_detail", "workflowKey": "prediction"}, "project": {"id": "p"}}
        responses = [
            {"message": "分析计划。", "questions": [], "operations": [],
             "goal_steps": [{"description": "看状态"}, {"description": "总结"}]},
            _turn("任务运行中。"),
            _turn("任务运行中，预计稍后完成。"),
            "任务运行中，预计稍后完成。",
        ]
        assistant, session = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="分析任务")
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["actions"], [])
        # Phase-2 fired: the final content is the free-text answer, not the short step message.
        self.assertEqual(result["content"], "任务运行中，预计稍后完成。")
        self.assertEqual(len(session.requests), 4)


class AgentLoopBoundaryTests(unittest.TestCase):
    """Planner-loop boundary behaviors: mid-outline questions, abort, declared limits."""

    def test_outline_step_question_ends_with_needs_input(self):
        # A question inside an outline step ends the turn with needs_input and KEEPS the
        # questions (never silently advancing the step or dropping the question). The outline
        # does not persist across turns — the next turn re-plans with the user's answer.
        d = CopilotSkillDefinition(name="task_detail:submit_current", label="Start run",
            description="Submit the current task for execution.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect="execute", context_type="task_detail")
        payload = {"page": {"contextType": "task_detail", "workflowKey": "peptide_design"}, "project": {"id": "p"}}
        responses = [
            {"message": "计划。", "questions": [], "operations": [],
             "goal_steps": [{"description": "改参数"}, {"description": "提交"}]},
            {"message": "问一下。", "questions": [
                {"text": "用哪个后端?", "kind": "choice",
                 "options": [{"label": "boltz", "value": "boltz"}, {"label": "AF3", "value": "alphafold3"}]},
            ], "operations": []},
        ]
        assistant, session = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="改参数并提交")
        self.assertEqual(result["state"], "needs_input")
        self.assertEqual(len(result["questions"]), 1)
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["questions"][0]["options"][0]["value"], "boltz")

    def test_abort_stops_before_next_model_call(self):
        # An SSE consumer disconnect sets abort during the loop; the next round must stop
        # BEFORE making another (costly) model call.
        d = CopilotSkillDefinition(name="task_detail:submit_current", label="Start run",
            description="Submit the current task for execution.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect="execute", context_type="task_detail")
        payload = {"page": {"contextType": "task_detail", "workflowKey": "peptide_design"}, "project": {"id": "p"}}
        responses = [
            {"message": "计划。", "questions": [], "operations": [],
             "goal_steps": [{"description": "改参数"}, {"description": "提交"}]},
            _turn("改。", operations=[_write_op("p", "task_detail:apply_parameter_patch", {"parameterPatch": {"peptideBinderLength": 25}})]),
        ]
        assistant, session = make_assistant(responses)
        abort = threading.Event()
        def on_step(step):
            if step.get("event") == "outline":
                abort.set()
        with patch_host_skills(d):
            with self.assertRaises(RuntimeError):
                assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                    user_id="u", username="alice", content="分析",
                                    on_event=on_step, abort=abort)
        # Only the first round's model call was made; the aborted round never fired.
        self.assertEqual(len(session.requests), 1)

    def test_too_many_questions_rejected(self):
        d = CopilotSkillDefinition(name="task_detail:submit_current", label="Start run",
            description="Submit the current task for execution.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect="execute", context_type="task_detail")
        payload = {"page": {"contextType": "task_detail", "workflowKey": "prediction"}, "project": {"id": "p"}}
        questions = [{"text": f"问题{i}?", "kind": "confirm"} for i in range(4)]
        responses = [
            {"message": "问。", "questions": questions, "operations": []},
            {"message": "修正。", "questions": questions[:2], "operations": []},
        ]
        assistant, session = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="问问题")
        self.assertEqual(result["state"], "needs_input")
        self.assertTrue(_has_feedback(session, 1, "at most 3"))

    def test_too_many_goal_steps_rejected(self):
        d = CopilotSkillDefinition(name="task_detail:submit_current", label="Start run",
            description="Submit the current task for execution.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect="execute", context_type="task_detail")
        payload = {"page": {"contextType": "task_detail", "workflowKey": "prediction"}, "project": {"id": "p"}}
        steps = [{"description": f"步骤{i}"} for i in range(21)]
        responses = [
            {"message": "计划。", "questions": [], "operations": [], "goal_steps": steps},
            {"message": "修正。", "questions": [], "operations": [], "goal_steps": steps[:2]},
            _turn("第一步完成。"),
            _turn("第二步完成。"),
            "计划完成。",
        ]
        assistant, session = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="做计划")
        self.assertTrue(_has_feedback(session, 1, "more items than the declared maximum")
                        or _has_feedback(session, 1, "at most 20"))


# --------------------------------------------------------------------------- #
# 7. Progressive disclosure: the exposed catalog IS the capability boundary
# --------------------------------------------------------------------------- #


class ProgressiveDisclosureTests(unittest.TestCase):
    """The planner sees ONLY the current page's action skills (plus universal reads).

    A page's action skills must never leak into another page's prompt, and any operation outside
    the exposed catalog is rejected by the harness ("is not registered"). The catalog defines
    what the planner can legitimately do at each step of a multi-page plan.
    """

    def _prompt_text(self, session: SequenceModelSession) -> str:
        return "\n".join(
            str(msg.get("content") or "")
            for msg in session.requests[0]["json"]["messages"]
        )

    def test_task_detail_prompt_exposes_only_task_detail_actions(self):
        payload = vs_task_detail_payload()
        responses = [
            _turn("虚拟筛选已就绪。"),
            "虚拟筛选已就绪，可随时开始。",
        ]
        assistant, session = make_assistant(responses)
        result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                     user_id="u", username="alice", content="开始筛选")
        self.assertEqual(result["state"], "complete")
        prompt = self._prompt_text(session)
        # Its own page's action skills ARE exposed.
        self.assertIn("task_detail:submit_current", prompt)
        self.assertIn("task_detail:apply_parameter_patch", prompt)
        # Other pages' action skills are NOT exposed.
        for other_page_skill in ("projects:create", "projects:failed", "tasks:create"):
            self.assertNotIn(other_page_skill, prompt, f"{other_page_skill} must not leak into task_detail")

    def test_project_list_prompt_exposes_only_project_list_actions(self):
        payload = {
            "page": {"contextType": "project_list"},
            "project": {"id": "p", "name": "P"},
            "summary": {"allTypeCounts": {"prediction": 1}, "allBackendCounts": {"boltz": 1},
                        "allTaskStateCounts": {"SUCCESS": 1}, "activeProjects": 0, "failedProjects": 0},
        }
        responses = [
            _turn("项目列表已就绪。"),
            "项目列表已就绪。",
        ]
        assistant, session = make_assistant(responses)
        result = assistant.plan_turn(context_type="project_list", context_payload=payload,
                                     user_id="u", username="alice", content="看项目")
        self.assertEqual(result["state"], "complete")
        prompt = self._prompt_text(session)
        self.assertIn("projects:create", prompt)
        self.assertIn("projects:failed", prompt)
        for other_page_skill in ("tasks:create", "task_detail:submit_current", "task_detail:apply_parameter_patch"):
            self.assertNotIn(other_page_skill, prompt, f"{other_page_skill} must not leak into project_list")

    def test_language_mandate_in_system_prompt(self):
        # The planner must answer in the user's language — the mandate is part of the system prompt.
        payload = {"page": {"contextType": "task_detail", "workflowKey": "prediction"}, "project": {"id": "p"}}
        responses = [_turn("好的。"), "好的。"]
        assistant, session = make_assistant(responses)
        with patch_host_skills():
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="你好")
        self.assertEqual(result["state"], "complete")
        self.assertIn("SAME language", self._prompt_text(session))

    def test_copilot_memory_usage_instruction_present(self):
        # copilot_memory (prior-turn retrieved records) is injected via context_payload, so the
        # system prompt must instruct the planner how to use it — and warn that memory carries
        # truncated identity only, never full data to be completed from.
        payload = {"page": {"contextType": "task_detail", "workflowKey": "prediction"}, "project": {"id": "p"}}
        responses = [_turn("好的。"), "好的。"]
        assistant, session = make_assistant(responses)
        with patch_host_skills():
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="继续")
        self.assertEqual(result["state"], "complete")
        prompt = self._prompt_text(session)
        self.assertIn("copilot_memory", prompt)
        self.assertIn("TRUNCATED", prompt)
        self.assertIn("Never invent or complete field values from memory", prompt)

    def test_system_prompt_load_bearing_sections_present(self):
        # A/B evidence (real model, 3 reps × 3 scenarios): the deployed mid-tier planner model
        # measurably depends on the explicit sourcing/discipline sections. A slimmed prompt lost
        # target-by-name (1/3) and sequence-by-name (0/3) while the full prompt scores 9/9 with
        # the same harness. These sections are therefore load-bearing: removing one silently is a
        # behavior regression, not a cleanup. (Revisit only together with a model upgrade + a
        # rerun of the A/B harness in e2e_diag_copilot.py / ab-style scenarios.)
        payload = {"page": {"contextType": "task_detail", "workflowKey": "prediction"}, "project": {"id": "p"}}
        responses = [_turn("好的。"), "好的。"]
        assistant, session = make_assistant(responses)
        with patch_host_skills():
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="继续")
        self.assertEqual(result["state"], "complete")
        system_portion = self._prompt_text(session).split("CONTRACT — three roles, one loop", 1)[0]
        for section in ("LANGUAGE:", "PLAN CORRECTNESS", "PLAN RECOVERY", "CONFIRMATION HONESTY",
                        "SKILL EXPOSURE", "INPUT SOURCING", "EXECUTION PRINCIPLES", "DATA ANSWERS"):
            self.assertIn(section, system_portion, f"{section!r} is a load-bearing prompt section")

    def test_system_prompt_does_not_duplicate_protocol_guidelines(self):
        # pi/Anthropic principle: say it once, in the right place. Loop MECHANICS (id rules,
        # repeats, reference syntax, goal_steps lock/hold) live in the harness-rendered
        # protocol GUIDELINES; the system prompt carries only judgment and boundaries. These
        # exact lines were deduplicated (A/B-verified 9/9) — a regression here means the
        # prompt started growing duplicates again.
        payload = {"page": {"contextType": "task_detail", "workflowKey": "prediction"}, "project": {"id": "p"}}
        responses = [_turn("好的。"), "好的。"]
        assistant, session = make_assistant(responses)
        with patch_host_skills():
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="继续")
        self.assertEqual(result["state"], "complete")
        prompt = self._prompt_text(session)
        system_portion = prompt.split("CONTRACT — three roles, one loop", 1)[0]
        for deduped in (
            "Never repeat an exact call that already succeeded",
            "Reference retrieved values via $fromObservation",
            "Only the current page's action skills are listed",
            "The harness drives each step one at a time and reports",
        ):
            self.assertNotIn(deduped, system_portion,
                             f"{deduped!r} is protocol GUIDELINES content and must not also live in the system prompt")
        # The protocol still carries them — the split, not the content, is what matters.
        for kept in ("Never repeat an exact call that already SUCCEEDED",
                     "Reference retrieved values, never paste long values"):
            self.assertIn(kept, prompt)

    def test_choice_question_result_carries_allow_other(self):
        # The UI offers a free-text "Other ___" answer next to choice chips unless the planner
        # disables it; the flag must round-trip the audit into the turn result untouched.
        payload = {"page": {"contextType": "task_detail", "workflowKey": "prediction"}, "project": {"id": "p"}}
        question = {
            "text": "用哪个模板结构?",
            "kind": "choice",
            "allowOther": True,
            "options": [
                {"label": "实验结构", "value": "experimental"},
                {"label": "预测模型", "value": "predicted"},
            ],
        }
        responses = [_turn("请选择模板来源。", questions=[question])]
        assistant, session = make_assistant(responses)
        with patch_host_skills():
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="帮我搭任务")
        self.assertEqual(result["state"], "needs_input")
        self.assertEqual(result["questions"][0]["allowOther"], True)

    def test_unexposed_page_skill_is_rejected(self):
        # On a task_detail turn the planner emits a project_list action — outside the exposed
        # catalog, so the harness must reject it and the planner must recover honestly.
        payload = vs_task_detail_payload()
        responses = [
            _turn("建项目。", operations=[_write_op("c", "projects:create", {"name": "X"})]),
            _turn("当前页面无法创建项目。"),
            "当前页面无法创建项目。",
        ]
        assistant, session = make_assistant(responses)
        result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                     user_id="u", username="alice", content="帮我建个项目")
        self.assertEqual(result["state"], "complete")
        self.assertTrue(_has_feedback(session, 1, "is not registered"))
        self.assertEqual(result["actions"], [])
        self.assertIn("无法", result["content"])

    def test_unexposed_task_detail_skill_rejected_on_project_list(self):
        # Symmetric case: submit_current is a task_detail action; on project_list it is not in
        # the catalog, so the harness rejects it with the same contract message.
        payload = {"page": {"contextType": "project_list"}, "project": {"id": "p", "name": "P"},
                   "summary": {"allTypeCounts": {}, "allBackendCounts": {}, "allTaskStateCounts": {},
                               "activeProjects": 0, "failedProjects": 0}}
        responses = [
            _turn("提交。", operations=[_write_op("s", "task_detail:submit_current", {})]),
            _turn("当前页面无法提交任务。"),
            "当前页面无法提交任务。",
        ]
        assistant, session = make_assistant(responses)
        result = assistant.plan_turn(context_type="project_list", context_payload=payload,
                                     user_id="u", username="alice", content="帮我提交任务")
        self.assertEqual(result["state"], "complete")
        self.assertTrue(_has_feedback(session, 1, "is not registered"))
        self.assertEqual(result["actions"], [])


if __name__ == "__main__":
    unittest.main()


class HeldWritesCompletionGuardTests(unittest.TestCase):
    """A turn that held confirmation operations (emitted alongside reads) may not end 'complete'
    without resolving them: retrieving data is not applying it. The guard forces ONE
    reconsideration; the second completion is accepted (explicit withdrawal is legitimate).
    """

    def _write_def(self):
        return CopilotSkillDefinition(name="eval.write", label="Write", description="Write.",
            input_schema={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False},
            effect="update")

    def _read_def(self):
        return OnlineSkillDefinition(name="eval.read", description="Read.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False})

    def test_completion_with_unresolved_held_writes_is_reconsidered_once(self):
        d = self._write_def(); rd = self._read_def()
        payload = {"page": {"contextType": "task_detail", "workflowKey": "prediction"}, "project": {"id": "p", "task_type": "prediction"}}
        responses = [
            # round 0: read + write mixed -> reads run, write HELD
            _turn("混发。", operations=[_read_op("r", "eval.read", {}), _write_op("w", "eval.write", {"value": "x"})]),
            # round 1: model tries to end 'complete' -> guard forces reconsideration
            _turn("没做。", operations=[]),
            # round 2: model re-emits the write -> surfaced for confirmation
            _turn("补上。", operations=[_write_op("w2", "eval.write", {"value": "x"})]),
        ]
        assistant, session = make_assistant(responses)
        inject_read_skills(assistant, {"eval.read": (rd, lambda a: {"value": "x"})})
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="查了但没改")
        self.assertEqual(result["state"], "await_confirmation")
        self.assertTrue(any(a["id"] == "eval.write" for a in result["actions"]))
        # the reconsideration note reached the model
        self.assertTrue(_has_feedback(session, 2, "HELD and never applied"))

    def test_second_completion_after_withdrawal_is_accepted(self):
        d = self._write_def(); rd = self._read_def()
        payload = {"page": {"contextType": "task_detail", "workflowKey": "prediction"}, "project": {"id": "p", "task_type": "prediction"}}
        responses = [
            _turn("混发。", operations=[_read_op("r", "eval.read", {}), _write_op("w", "eval.write", {"value": "x"})]),
            _turn("不做了，明确告知用户。", operations=[]),   # guard fires
            _turn("这些步骤未应用，因为…", operations=[]),    # explicit withdrawal -> accepted
        ]
        assistant, session = make_assistant(responses)
        inject_read_skills(assistant, {"eval.read": (rd, lambda a: {"value": "x"})})
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="查了但没改")
        self.assertEqual(result["state"], "complete")
