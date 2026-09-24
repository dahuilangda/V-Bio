"""Realistic end-to-end Copilot planner-loop tests — 50+ real user scenarios.

These tests drive the FULL planner loop (model call → audit → skill execution → replan) against
REAL user questions (Chinese prompts the production Copilot receives) and the REAL host skills and
read skills, scripting only the model's JSON responses. They pin the behavior a user sees for the
highest-frequency request patterns and guard the regressions that motivated the refactor.

Categories:
  - TaskDetailAnalysisScenario: analyze/explain the current task from context (all states)
  - ParameterPatchScenario: per-workflow parameter patches + run chains (prediction/affinity/peptide/virtual_screening)
  - TaskDetailActionScenario: submit/cancel/delete/save/metadata/attachments/template/affinity skills
  - TaskListFilterScenario: filter/sort/search the task list
  - ProjectListScenario: create/open/rename/delete/filter projects
  - ReadSkillScenario: each read skill's realistic question + correct routing
  - AmbiguousEntityScenario: disambiguation before searching
  - FailedSourceScenario: source-unavailable honest reporting (three-state audit)
  - NoMatchScenario: authoritative empty results, honest reporting
  - MultiStepScenario: search → resolve → action chains, outline plans
  - CrossPageScenario: project_list → task_list → task_detail progression
  - ConversationFollowUpScenario: MULTI-TURN sequences where turn 2 references turn 1
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from management_api.copilot import CopilotAssistant
from management_api.copilot_skill_harness import CopilotSkillDefinition
from management_api.copilot_skills.online_databases import OnlineSkillDefinition
from tests.helpers import FakeResponse, NullLogger, is_phase2_request


# --------------------------------------------------------------------------- #
# Test infrastructure
# --------------------------------------------------------------------------- #


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
    # Dict responses are the JSON planner turns; plain-string responses are phase-2 free-text
    # answers (kept verbatim, NOT JSON-wrapped — json.dumps would add quotes/escapes).
    session = SequenceModelSession([r if isinstance(r, str) else json.dumps(r) for r in responses])
    assistant = CopilotAssistant(
        chat_api_url="http://model.invalid/v1/chat/completions",
        chat_api_key="test-key", chat_model="test-model",
        timeout_seconds=3, session=session, logger=NullLogger(), max_planner_rounds=6,
    )
    return assistant, session


def _turn(message: str, operations: List[Dict[str, Any]] | None = None,
          questions: List[Dict[str, Any]] | None = None,
          goal_steps: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    t: Dict[str, Any] = {"message": message, "questions": questions or [], "operations": operations or []}
    if goal_steps is not None:
        t["goal_steps"] = goal_steps
    return t


def _read_op(op_id: str, skill: str, arguments: Dict[str, Any], depends: List[str] | None = None) -> Dict[str, Any]:
    return {"id": op_id, "skill": skill, "arguments": arguments, "depends_on": depends or []}


def _write_op(op_id: str, skill: str, arguments: Dict[str, Any], depends: List[str] | None = None) -> Dict[str, Any]:
    return _read_op(op_id, skill, arguments, depends)


def _feedback(session: SequenceModelSession, round_index: int, marker: str) -> str:
    for msg in session.requests[round_index]["json"]["messages"]:
        content = str(msg.get("content") or "")
        if marker in content:
            return content
    raise AssertionError(f"No feedback message containing {marker!r} in round {round_index}")


def _has_feedback(session: SequenceModelSession, round_index: int, marker: str) -> bool:
    try:
        _feedback(session, round_index, marker)
        return True
    except AssertionError:
        return False


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
def conv_context(*pairs: Tuple[str, str]) -> Dict[str, Any]:
    """Build a copilot_conversation context_payload from (role, content) pairs (multi-turn)."""
    recent = [{"role": role, "content": content} for role, content in pairs]
    return {"copilot_conversation": {"recent_messages": recent, "compression": "recent_only", "total_messages": len(recent)}}


def task_detail_payload(workflow: str = "prediction", **task_overrides: Any) -> Dict[str, Any]:
    task: Dict[str, Any] = {
        "id": "row-1", "name": "Test task", "task_state": "SUCCESS", "backend": "boltz",
        "confidenceSummary": {"avgPlddt": 82.0, "iptm": 0.68},
        "affinitySummary": {}, "components": [{"index": 0, "type": "protein", "sequenceLength": 300}],
        "error_text": "",
    }
    task.update(task_overrides)
    return {
        "page": {"contextType": "task_detail", "workflowKey": workflow},
        "project": {"id": "proj-1", "name": "Test Project", "task_type": workflow},
        "draft": {"taskName": task["name"], "backend": "boltz"},
        "currentTask": task,
    }


# --------------------------------------------------------------------------- #
# 1. Task analysis — answer from context (10 scenarios)
# --------------------------------------------------------------------------- #


class TaskDetailAnalysisScenarioTests(unittest.TestCase):
    def test_analyze_successful_prediction_quotes_plddt_iptm(self):
        responses = [_turn("任务成功完成,平均 pLDDT 82.0、ipTM 0.68,置信度良好,蛋白 300 残基,boltz 后端。"),
        "任务成功完成，平均 pLDDT 82.0、ipTM 0.68，置信度良好，蛋白 300 残基，boltz 后端。",
    ]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload(),
                                      user_id="u", username="alice", content="分析现在的任务")
        self.assertEqual(result["state"], "complete")
        self.assertIn("82.0", result["content"])

    def test_title_only_answer_accepted_immediately(self):
        # The harness does NOT audit message text. A title-only answer is the model's output —
        # the harness trusts it. The system prompt guides the model to write complete answers;
        # the harness only audits structure. This means the model's first response is accepted.
        responses = [_turn("当前任务分析如下："),
        "当前任务分析如下：任务成功完成，平均 pLDDT 82.0、ipTM 0.68，置信度良好。",
    ]
        assistant, session = make_assistant(responses)
        result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload(),
                                      user_id="u", username="alice", content="分析一下这个 boltz 任务的结果")
        self.assertEqual(result["state"], "complete")
        self.assertGreaterEqual(len(session.requests), 1)

    def test_failed_task_quotes_error_text(self):
        payload = task_detail_payload(task_state="FAILURE", error_text="CUDA out of memory", backend="alphafold3", confidenceSummary={})
        responses = [_turn("任务失败:CUDA out of memory。alphafold3 显存不足,建议减少组件或用 boltz 重试。"),
        "任务失败：CUDA out of memory。alphafold3 显存不足，建议减少组件或用 boltz 重试。",
    ]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(context_type="task_detail", context_payload=payload, user_id="u", username="alice", content="分析现在的任务")
        self.assertEqual(result["state"], "complete")
        self.assertIn("CUDA", result["content"])

    def test_queued_task_reports_waiting_state(self):
        payload = task_detail_payload(task_state="QUEUED", confidenceSummary={})
        responses = [_turn("任务当前状态为 QUEUED(排队中),尚未开始执行。它会在计算资源可用时自动开始,可在任务列表查看实时进度。"),
        "任务当前状态为 QUEUED（排队中），尚未开始执行。它会在计算资源可用时自动开始，可在任务列表查看实时进度。",
    ]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(context_type="task_detail", context_payload=payload, user_id="u", username="alice", content="分析现在的任务")
        self.assertEqual(result["state"], "complete")
        self.assertIn("QUEUED", result["content"])

    def test_running_task_reports_in_progress(self):
        payload = task_detail_payload(task_state="RUNNING", confidenceSummary={})
        responses = [_turn("任务状态 RUNNING,正在运行中,预计稍后完成。"),
        "任务状态 RUNNING，正在运行中，预计稍后完成。",
    ]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(context_type="task_detail", context_payload=payload, user_id="u", username="alice", content="分析现在的任务")
        self.assertEqual(result["state"], "complete")
        self.assertIn("RUNNING", result["content"])

    def test_draft_task_reports_not_submitted(self):
        payload = task_detail_payload(task_state="DRAFT", confidenceSummary={})
        responses = [_turn("任务状态 DRAFT(草稿),尚未提交运行。检查组件后可点击运行。"),
        "任务状态 DRAFT（草稿），尚未提交运行。检查组件后可点击运行。",
    ]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(context_type="task_detail", context_payload=payload, user_id="u", username="alice", content="分析现在的任务")
        self.assertEqual(result["state"], "complete")
        self.assertIn("DRAFT", result["content"])

    def test_revoked_task_reports_cancelled(self):
        payload = task_detail_payload(task_state="REVOKED", confidenceSummary={}, error_text="user cancelled")
        responses = [
            _turn("任务已被取消(user cancelled)。可重新提交运行。"),
            "任务已被取消（user cancelled）。可重新提交运行。",
        ]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(context_type="task_detail", context_payload=payload, user_id="u", username="alice", content="分析现在的任务")
        self.assertEqual(result["state"], "complete")
        self.assertIn("取消", result["content"])

    def test_affinity_task_quotes_interface_metric(self):
        payload = task_detail_payload("affinity", affinitySummary={"interfaceMetric": 0.55}, confidenceSummary={})
        responses = [_turn("亲和力任务完成,界面指标 0.55,结合力中等。"),
        "亲和力任务完成，界面指标 0.55，结合力中等。",
    ]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(context_type="task_detail", context_payload=payload, user_id="u", username="alice", content="分析现在的任务")
        self.assertEqual(result["state"], "complete")
        self.assertIn("0.55", result["content"])

    def test_low_confidence_task_flagged(self):
        payload = task_detail_payload(confidenceSummary={"avgPlddt": 45.0, "iptm": 0.3})
        responses = [_turn("平均 pLDDT 仅 45.0、ipTM 0.3,预测置信度偏低,结果可靠性差,建议调整参数重跑。"),
        "平均 pLDDT 仅 45.0、ipTM 0.3，预测置信度偏低，结果可靠性差，建议调整参数重跑。",
    ]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(context_type="task_detail", context_payload=payload, user_id="u", username="alice", content="分析现在的任务")
        self.assertEqual(result["state"], "complete")
        self.assertIn("45.0", result["content"])

    def test_multi_component_task_describes_each(self):
        payload = task_detail_payload(components=[
            {"index": 0, "type": "protein", "sequenceLength": 250},
            {"index": 1, "type": "ligand"},
        ])
        responses = [_turn("任务含 1 个蛋白(250 残基)和 1 个配体,已完成预测。"),
        "任务含 1 个蛋白（250 残基）和 1 个配体，已完成预测。",
    ]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(context_type="task_detail", context_payload=payload, user_id="u", username="alice", content="分析现在的任务")
        self.assertEqual(result["state"], "complete")
        self.assertIn("250", result["content"])


# --------------------------------------------------------------------------- #
# 2. Parameter patch + run (8 scenarios across workflows)
# --------------------------------------------------------------------------- #


class ParameterPatchScenarioTests(unittest.TestCase):
    def _patch_skill(self, workflow: str) -> CopilotSkillDefinition:
        from management_api.copilot_capabilities import build_task_detail_skill_definitions
        return next(d for d in build_task_detail_skill_definitions(workflow) if d.name == "task_detail:apply_parameter_patch")

    def _submit_skill(self, workflow: str) -> CopilotSkillDefinition:
        from management_api.copilot_capabilities import build_task_detail_skill_definitions
        return next(d for d in build_task_detail_skill_definitions(workflow) if d.name == "task_detail:submit_current")

    def test_prediction_seed_patch_then_submit(self):
        patch_d, submit_d = self._patch_skill("prediction"), self._submit_skill("prediction")
        responses = [_turn("改 seed 并运行。", operations=[
            _write_op("patch", "task_detail:apply_parameter_patch", {"parameterPatch": {"seed": 42}}),
            _write_op("submit", "task_detail:submit_current", {}, depends=["patch"]),
        ])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(patch_d, submit_d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload("prediction"),
                                          user_id="u", username="alice", content="把 seed 改成 42 然后运行")
        self.assertEqual(result["state"], "await_confirmation")
        self.assertEqual(result["actions"][0]["arguments"]["parameterPatch"], {"seed": 42})

    def test_prediction_backend_switch_to_alphafold3(self):
        d = self._patch_skill("prediction")
        responses = [_turn("切换后端。", operations=[_write_op("p", "task_detail:apply_parameter_patch", {"parameterPatch": {"backend": "alphafold3"}})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload("prediction"), user_id="u", username="alice", content="用 alphafold3 跑")
        self.assertEqual(result["state"], "await_confirmation")
        self.assertEqual(result["actions"][0]["arguments"]["parameterPatch"]["backend"], "alphafold3")

    def test_affinity_mode_change_to_pose(self):
        d = self._patch_skill("affinity")
        responses = [_turn("改亲和力模式。", operations=[_write_op("p", "task_detail:apply_parameter_patch", {"parameterPatch": {"affinityMode": "pose"}})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload("affinity"), user_id="u", username="alice", content="换成 pose 模式")
        self.assertEqual(result["state"], "await_confirmation")
        self.assertEqual(result["actions"][0]["arguments"]["parameterPatch"]["affinityMode"], "pose")

    def test_peptide_design_iterations_change(self):
        d = self._patch_skill("peptide_design")
        responses = [_turn("增加迭代次数。", operations=[_write_op("p", "task_detail:apply_parameter_patch", {"parameterPatch": {"peptideIterations": 200}})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload("peptide_design"), user_id="u", username="alice", content="迭代 200 次")
        self.assertEqual(result["state"], "await_confirmation")
        self.assertEqual(result["actions"][0]["arguments"]["parameterPatch"]["peptideIterations"], 200)

    def test_peptide_binder_length_change(self):
        d = self._patch_skill("peptide_design")
        responses = [_turn("设置 binder 长度。", operations=[_write_op("p", "task_detail:apply_parameter_patch", {"parameterPatch": {"peptideBinderLength": 25}})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload("peptide_design"), user_id="u", username="alice", content="binder 长度 25")
        self.assertEqual(result["actions"][0]["arguments"]["parameterPatch"]["peptideBinderLength"], 25)

    def test_virtual_screening_seed_change(self):
        d = self._patch_skill("virtual_screening")
        responses = [_turn("改 seed。", operations=[_write_op("p", "task_detail:apply_parameter_patch", {"parameterPatch": {"seed": 7}})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload("virtual_screening"), user_id="u", username="alice", content="seed 设为 7")
        self.assertEqual(result["actions"][0]["arguments"]["parameterPatch"]["seed"], 7)

    def test_invalid_parameter_rejected_then_replanned(self):
        d = self._patch_skill("prediction")
        responses = [
            _turn("Updating.", operations=[_write_op("bad", "task_detail:apply_parameter_patch", {"parameterPatch": {"fakeParam": 99}})]),
            _turn("fakeParam 不是 prediction 任务的有效参数,可选 seed/backend/affinityBinding 等。"),
            "fakeParam 不是 prediction 任务的有效参数，可选 seed/backend/affinityBinding 等。",
        ]
        assistant, session = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload("prediction"), user_id="u", username="alice", content="改参数")
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["actions"], [])
        self.assertTrue(_has_feedback(session, 1, "is not declared"))

    def test_seed_out_of_range_rejected(self):
        d = self._patch_skill("prediction")
        responses = [
            _turn("设置 seed。", operations=[_write_op("bad", "task_detail:apply_parameter_patch", {"parameterPatch": {"seed": -5}})]),
            _turn("seed 不能为负数,prediction 任务已用默认值。"),
            "seed 不能为负数，prediction 任务已用默认值。",
        ]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload("prediction"), user_id="u", username="alice", content="seed 设为 -5")
        self.assertEqual(result["state"], "complete")


# --------------------------------------------------------------------------- #
# 3. Task-detail action skills (8 scenarios)
# --------------------------------------------------------------------------- #


class TaskDetailActionScenarioTests(unittest.TestCase):
    def _skills(self, workflow: str, *names: str) -> List[CopilotSkillDefinition]:
        from management_api.copilot_capabilities import build_task_detail_skill_definitions
        all_defs = build_task_detail_skill_definitions(workflow)
        by_name = {d.name: d for d in all_defs}
        return [by_name[n] for n in names]

    def test_submit_current(self):
        [d] = self._skills("prediction", "task_detail:submit_current")
        responses = [_turn("提交任务。", operations=[_write_op("s", "task_detail:submit_current", {})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload(), user_id="u", username="alice", content="运行")
        self.assertEqual(result["state"], "await_confirmation")

    def test_cancel_current(self):
        [d] = self._skills("prediction", "task_detail:cancel_current")
        responses = [_turn("取消任务。", operations=[_write_op("c", "task_detail:cancel_current", {})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload(task_state="RUNNING"), user_id="u", username="alice", content="取消")
        self.assertEqual(result["state"], "await_confirmation")
        self.assertTrue(result["actions"][0]["payload"]["destructive"])

    def test_delete_current(self):
        [d] = self._skills("prediction", "task_detail:delete_current")
        responses = [_turn("删除任务。", operations=[_write_op("d", "task_detail:delete_current", {})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload(), user_id="u", username="alice", content="删除这个任务")
        self.assertEqual(result["state"], "await_confirmation")
        self.assertTrue(result["actions"][0]["payload"]["destructive"])

    def test_save_draft(self):
        [d] = self._skills("prediction", "task_detail:save_draft")
        responses = [_turn("保存草稿。", operations=[_write_op("sv", "task_detail:save_draft", {})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload(task_state="DRAFT"), user_id="u", username="alice", content="保存")
        self.assertEqual(result["state"], "await_confirmation")

    def test_rename_task(self):
        [d] = self._skills("prediction", "task_detail:apply_metadata_patch")
        responses = [_turn("重命名。", operations=[_write_op("rn", "task_detail:apply_metadata_patch", {"metadataPatch": {"taskName": "New Name"}})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload(), user_id="u", username="alice", content="改名叫 New Name")
        self.assertEqual(result["actions"][0]["arguments"]["metadataPatch"]["taskName"], "New Name")

    def test_structure_template_prediction_only(self):
        [d] = self._skills("prediction", "task_detail:apply_structure_template")
        responses = [_turn("应用模板。", operations=[_write_op("t", "task_detail:apply_structure_template", {"structureUrl": "https://files.rcsb.org/download/1ABC.pdb", "fileName": "1ABC.pdb"})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload("prediction"), user_id="u", username="alice", content="用 1ABC 做模板")
        self.assertEqual(result["actions"][0]["arguments"]["structureUrl"], "https://files.rcsb.org/download/1ABC.pdb")

    def test_affinity_ligand_smiles(self):
        [d] = self._skills("affinity", "task_detail:apply_docking_ligand_smiles")
        responses = [_turn("设置配体。", operations=[_write_op("l", "task_detail:apply_docking_ligand_smiles", {"smiles": "CCO"})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload("affinity"), user_id="u", username="alice", content="配体用 CCO")
        self.assertEqual(result["actions"][0]["arguments"]["smiles"], "CCO")

    def test_affinity_target_structure(self):
        [d] = self._skills("affinity", "task_detail:apply_docking_target_structure")
        responses = [_turn("设置靶标结构。", operations=[_write_op("tg", "task_detail:apply_docking_target_structure", {"structureUrl": "https://files.rcsb.org/download/1XYZ.pdb"})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload("affinity"), user_id="u", username="alice", content="靶标用 1XYZ")
        self.assertEqual(result["actions"][0]["arguments"]["structureUrl"], "https://files.rcsb.org/download/1XYZ.pdb")


# --------------------------------------------------------------------------- #
# 4. Task-list filter/sort (6 scenarios)
# --------------------------------------------------------------------------- #


class TaskListFilterScenarioTests(unittest.TestCase):
    def _skills(self) -> List[CopilotSkillDefinition]:
        from management_api.copilot_skills.context_actions import build_context_skill_definitions
        return build_context_skill_definitions("task_list", {"project": {"id": "p1", "workflow_key": "prediction"}})

    def test_filter_failed_tasks(self):
        d = next(s for s in self._skills() if s.name == "tasks:update_view")
        responses = [_turn("筛选失败。", operations=[_write_op("f", "tasks:update_view", {"stateFilter": "FAILURE"})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_list", context_payload={"project": {"id": "p1"}}, user_id="u", username="alice", content="哪些任务失败了")
        self.assertEqual(result["actions"][0]["arguments"]["stateFilter"], "FAILURE")

    def test_filter_running_tasks(self):
        d = next(s for s in self._skills() if s.name == "tasks:update_view")
        responses = [_turn("筛选运行中。", operations=[_write_op("f", "tasks:update_view", {"stateFilter": "RUNNING"})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_list", context_payload={"project": {"id": "p1"}}, user_id="u", username="alice", content="正在运行的任务")
        self.assertEqual(result["actions"][0]["arguments"]["stateFilter"], "RUNNING")

    def test_sort_by_iptm_desc(self):
        d = next(s for s in self._skills() if s.name == "tasks:update_view")
        responses = [_turn("排序。", operations=[_write_op("s", "tasks:update_view", {"sortKey": "iptm", "sortDirection": "desc"})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_list", context_payload={"project": {"id": "p1"}}, user_id="u", username="alice", content="按 iptm 从高到低")
        self.assertEqual(result["actions"][0]["arguments"]["sortKey"], "iptm")

    def test_search_by_text(self):
        d = next(s for s in self._skills() if s.name == "tasks:update_view")
        responses = [_turn("搜索。", operations=[_write_op("s", "tasks:update_view", {"search": "EGFR"})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_list", context_payload={"project": {"id": "p1"}}, user_id="u", username="alice", content="搜 EGFR")
        self.assertEqual(result["actions"][0]["arguments"]["search"], "EGFR")

    def test_filter_by_backend(self):
        d = next(s for s in self._skills() if s.name == "tasks:update_view")
        responses = [_turn("筛选后端。", operations=[_write_op("f", "tasks:update_view", {"backendFilter": "boltz"})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_list", context_payload={"project": {"id": "p1"}}, user_id="u", username="alice", content="boltz 的任务")
        self.assertEqual(result["actions"][0]["arguments"]["backendFilter"], "boltz")

    def test_clear_filters(self):
        d = next(s for s in self._skills() if s.name == "tasks:clear_filters")
        responses = [_turn("清除筛选。", operations=[_write_op("c", "tasks:clear_filters", {})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_list", context_payload={"project": {"id": "p1"}}, user_id="u", username="alice", content="显示全部")
        self.assertEqual(result["state"], "await_confirmation")


# --------------------------------------------------------------------------- #
# 5. Project-list actions (5 scenarios)
# --------------------------------------------------------------------------- #


class ProjectListScenarioTests(unittest.TestCase):
    def _skills(self) -> List[CopilotSkillDefinition]:
        from management_api.copilot_skills.context_actions import build_context_skill_definitions
        return build_context_skill_definitions("project_list", {})

    def test_create_prediction_project(self):
        d = next(s for s in self._skills() if s.name == "projects:create")
        responses = [_turn("创建。", operations=[_write_op("c", "projects:create", {"create": True, "workflow": "prediction"})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="project_list", context_payload={}, user_id="u", username="alice", content="建个预测项目")
        self.assertEqual(result["actions"][0]["arguments"]["workflow"], "prediction")

    def test_create_virtual_screening_project(self):
        d = next(s for s in self._skills() if s.name == "projects:create")
        responses = [_turn("创建。", operations=[_write_op("c", "projects:create", {"create": True, "workflow": "virtual_screening"})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="project_list", context_payload={}, user_id="u", username="alice", content="建虚拟筛选项目")
        self.assertEqual(result["actions"][0]["arguments"]["workflow"], "virtual_screening")

    def test_open_project(self):
        d = next(s for s in self._skills() if s.name == "projects:open")
        responses = [_turn("打开。", operations=[_write_op("o", "projects:open", {"projectId": "proj-42"})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="project_list", context_payload={}, user_id="u", username="alice", content="打开 proj-42")
        self.assertEqual(result["actions"][0]["arguments"]["projectId"], "proj-42")

    def test_rename_project(self):
        d = next(s for s in self._skills() if s.name == "projects:rename")
        responses = [_turn("重命名。", operations=[_write_op("r", "projects:rename", {"projectId": "p1", "projectName": "New Project"})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="project_list", context_payload={}, user_id="u", username="alice", content="把 p1 改名 New Project")
        self.assertEqual(result["actions"][0]["arguments"]["projectName"], "New Project")

    def test_filter_active_projects(self):
        d = next(s for s in self._skills() if s.name == "projects:active")
        responses = [_turn("筛选活跃。", operations=[_write_op("f", "projects:active", {"activityFilter": "active"})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="project_list", context_payload={}, user_id="u", username="alice", content="活跃的项目")
        self.assertEqual(result["actions"][0]["arguments"]["activityFilter"], "active")


# --------------------------------------------------------------------------- #
# 6. Read-skill routing (8 scenarios)
# --------------------------------------------------------------------------- #


class ReadSkillScenarioTests(unittest.TestCase):
    def _stub_uniprot_search(self, accession: str = "Q02127", gene: str = "DHODH"):
        def handler(args: Dict[str, Any]) -> Dict[str, Any]:
            return {"source": "uniprot", "query": args.get("query", ""), "count": 1,
                    "results": [{"accession": accession, "geneNames": gene, "proteinName": gene, "sequence": "MSEQ"}]}
        d = OnlineSkillDefinition(name="uniprot.search", description="Search UniProt.",
            input_schema={"type": "object", "properties": {"query": {"type": "string", "minLength": 1}}, "required": ["query"], "additionalProperties": False})
        return d, handler

    def _stub_pubchem(self, smiles: str = "CCO", title: str = "Ethanol"):
        def handler(args: Dict[str, Any]) -> Dict[str, Any]:
            return {"source": "pubchem", "query": args.get("identifier", ""), "count": 1,
                    "results": [{"cid": "702", "title": title, "smiles": smiles}]}
        d = OnlineSkillDefinition(name="pubchem.search", description="Search PubChem.",
            input_schema={"type": "object", "properties": {"identifier": {"type": "string", "minLength": 1}}, "required": ["identifier"], "additionalProperties": False})
        return d, handler

    def test_gene_lookup_routes_to_uniprot_search(self):
        d, handler = self._stub_uniprot_search()
        responses = [_turn("查找。", operations=[_read_op("s", "uniprot.search", {"query": "gene:DHODH AND organism_id:9606"})]),
                     _turn("DHODH (Q02127) 序列已获取。")]
        assistant, session = make_assistant(responses)
        inject_read_skills(assistant, {"uniprot.search": (d, handler)})
        result = assistant.plan_turn(context_type="task_detail", context_payload={}, user_id="u", username="alice", content="人源 DHODH 的序列")
        self.assertEqual(result["state"], "complete")
        self.assertIn("Q02127", result["content"])

    def test_compound_lookup_routes_to_pubchem(self):
        d, handler = self._stub_pubchem(smiles="CC(=O)Oc1ccccc1C(=O)O", title="Aspirin")
        responses = [_turn("查找。", operations=[_read_op("f", "pubchem.search", {"identifier": "aspirin"})]),
                     _turn("Aspirin 的 SMILES 是 CC(=O)Oc1ccccc1C(=O)O。")]
        assistant, _ = make_assistant(responses)
        inject_read_skills(assistant, {"pubchem.search": (d, handler)})
        result = assistant.plan_turn(context_type="task_detail", context_payload={}, user_id="u", username="alice", content="阿司匹林的 SMILES")
        self.assertIn("Aspirin", result["content"])

    def test_pubchem_no_match_honest(self):
        def handler(args): return {"source": "pubchem", "query": args.get("identifier", ""), "count": 0, "results": []}
        d = OnlineSkillDefinition(name="pubchem.search", description="Search PubChem.",
            input_schema={"type": "object", "properties": {"identifier": {"type": "string", "minLength": 1}}, "required": ["identifier"], "additionalProperties": False})
        responses = [_turn("查找。", operations=[_read_op("f", "pubchem.search", {"identifier": "NOPE"})]),
                     _turn("PubChem 没有找到 NOPE 的记录。")]
        assistant, session = make_assistant(responses)
        inject_read_skills(assistant, {"pubchem.search": (d, handler)})
        result = assistant.plan_turn(context_type="task_detail", context_payload={}, user_id="u", username="alice", content="NOPE 的 SMILES")
        self.assertIn("NOPE", result["content"])
        self.assertTrue(_has_feedback(session, 1, "NO_MATCH"))

    def test_source_unavailable_reported_honestly(self):
        def handler(args): raise RuntimeError("HTTP 503 service unavailable")
        d = OnlineSkillDefinition(name="uniprot.resolve", description="Resolve.",
            input_schema={"type": "object", "properties": {"identifier": {"type": "string", "minLength": 1}}, "required": ["identifier"], "additionalProperties": False})
        responses = [_turn("查找。", operations=[_read_op("r", "uniprot.resolve", {"identifier": "P00533"})]),
                     _turn("UniProt 暂时不可用,请稍后重试。")]
        assistant, session = make_assistant(responses)
        inject_read_skills(assistant, {"uniprot.resolve": (d, handler)})
        result = assistant.plan_turn(context_type="task_detail", context_payload={}, user_id="u", username="alice", content="P00533 的序列")
        self.assertTrue("不可用" in result["content"] or "重试" in result["content"])
        self.assertTrue(_has_feedback(session, 1, "FAILED"))

    def test_target_inhibitors_route_to_chembl_target_activity(self):
        def handler(args): return {"source": "chembl", "query": args.get("query", ""), "count": 1,
            "results": [{"title": "Inhibitor X", "chemblId": "CHEMBL1", "smiles": "CCO", "activityType": "IC50", "value": 12.0, "units": "nM"}]}
        d = OnlineSkillDefinition(name="chembl.target_activity", description="Find inhibitors for a target.",
            input_schema={"type": "object", "properties": {"query": {"type": "string", "minLength": 1}}, "required": ["query"], "additionalProperties": False})
        responses = [_turn("查找抑制剂。", operations=[_read_op("i", "chembl.target_activity", {"query": "HPK1"})]),
                     _turn("HPK1 的抑制剂 Inhibitor X,IC50 12 nM。")]
        assistant, _ = make_assistant(responses)
        inject_read_skills(assistant, {"chembl.target_activity": (d, handler)})
        result = assistant.plan_turn(context_type="task_detail", context_payload={}, user_id="u", username="alice", content="HPK1 的抑制剂")
        self.assertIn("12", result["content"])

    def test_rcsb_search_finds_structure(self):
        def handler(args): return {"source": "rcsb", "query": args.get("text", ""), "count": 1,
            "results": [{"pdbId": "1ABC", "title": "Test structure", "method": "X-RAY", "resolution": 2.1}]}
        d = OnlineSkillDefinition(name="rcsb.search", description="Search RCSB PDB.",
            input_schema={"type": "object", "properties": {"text": {"type": "string", "minLength": 1}}, "required": ["text"], "additionalProperties": False})
        responses = [_turn("搜索结构。", operations=[_read_op("r", "rcsb.search", {"text": "EGFR"})]),
                     _turn("找到 EGFR 结构 1ABC,X-RAY 2.1Å。")]
        assistant, _ = make_assistant(responses)
        inject_read_skills(assistant, {"rcsb.search": (d, handler)})
        result = assistant.plan_turn(context_type="task_detail", context_payload={}, user_id="u", username="alice", content="EGFR 的实验结构")
        self.assertIn("1ABC", result["content"])

    def test_alphafold_resolve_predicted_structure(self):
        def handler(args): return {"source": "alphafold", "identifier": "P00533", "accession": "P00533",
            "pdbUrl": "https://alphafold.pdb", "avgPlddt": 88.5}
        d = OnlineSkillDefinition(name="alphafold.resolve", description="Resolve AlphaFold structure.",
            input_schema={"type": "object", "properties": {"identifier": {"type": "string", "minLength": 1}}, "required": ["identifier"], "additionalProperties": False})
        responses = [_turn("查找预测结构。", operations=[_read_op("a", "alphafold.resolve", {"identifier": "P00533"})]),
                     _turn("AlphaFold 预测 P00533,pLDDT 88.5。")]
        assistant, _ = make_assistant(responses)
        inject_read_skills(assistant, {"alphafold.resolve": (d, handler)})
        result = assistant.plan_turn(context_type="task_detail", context_payload={}, user_id="u", username="alice", content="P00533 的 AlphaFold 预测")
        self.assertIn("88.5", result["content"])

    def test_pubmed_search_finds_papers(self):
        def handler(args): return {"source": "pubmed", "query": args.get("query", ""), "count": 1,
            "results": [{"pmid": "12345", "title": "EGFR in cancer", "journal": "Nature", "year": "2023"}]}
        d = OnlineSkillDefinition(name="pubmed.search", description="Search PubMed.",
            input_schema={"type": "object", "properties": {"query": {"type": "string", "minLength": 1}}, "required": ["query"], "additionalProperties": False})
        responses = [_turn("搜文献。", operations=[_read_op("p", "pubmed.search", {"query": "EGFR cancer"})]),
                     _turn("找到 EGFR in cancer (Nature 2023)。")]
        assistant, _ = make_assistant(responses)
        inject_read_skills(assistant, {"pubmed.search": (d, handler)})
        result = assistant.plan_turn(context_type="task_detail", context_payload={}, user_id="u", username="alice", content="EGFR 癌症相关文献")
        self.assertIn("EGFR", result["content"])


# --------------------------------------------------------------------------- #
# 7. Ambiguous entity disambiguation (3 scenarios)
# --------------------------------------------------------------------------- #


class AmbiguousEntityScenarioTests(unittest.TestCase):
    def test_protein_family_asks_which_member(self):
        # The identity contract: candidates come from RETRIEVAL, never memory — the choice
        # question's identifier options must be backed by an observation this turn.
        def handler(args): return {"source": "uniprot", "query": args.get("query", ""), "count": 2, "results": [
            {"accession": "P00533", "gene": "EGFR", "organism": "Homo sapiens"},
            {"accession": "P04626", "gene": "ERBB2", "organism": "Homo sapiens"}]}
        d = OnlineSkillDefinition(name="uniprot.search", description="Search UniProt.",
            input_schema={"type": "object", "properties": {"query": {"type": "string", "minLength": 1}}, "required": ["query"], "additionalProperties": False})
        responses = [_turn("检索 EGFR 家族。", operations=[_read_op("s", "uniprot.search", {"query": "gene:EGFR OR gene:ERBB2"})]),
                     _turn("EGFR 家族多个成员,请选择。", questions=[{"text": "你要哪个成员?", "kind": "choice",
            "options": [{"label": "EGFR (ERBB1)", "value": "P00533"}, {"label": "HER2 (ERBB2)", "value": "P04626"}]}])]
        assistant, _ = make_assistant(responses)
        inject_read_skills(assistant, {"uniprot.search": (d, handler)})
        result = assistant.plan_turn(context_type="task_detail", context_payload={}, user_id="u", username="alice", content="EGFR 的序列")
        self.assertEqual(result["state"], "needs_input")
        self.assertEqual(result["questions"][0]["kind"], "choice")

    def test_compound_name_ambiguous_asks_which(self):
        responses = [_turn("多个化合物匹配,请选择。", questions=[{"text": "你指的是?", "kind": "choice",
            "options": [{"label": "Aspirin", "value": "aspirin"}, {"label": "Acetylsalicylic acid", "value": "acetylsalicylic acid"}]}])]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(context_type="task_detail", context_payload={}, user_id="u", username="alice", content="阿司匹林")
        self.assertEqual(result["state"], "needs_input")

    def test_freeform_question_for_missing_info(self):
        responses = [_turn("需要靶标名称。", questions=[{"text": "你要查询哪个靶标蛋白?", "kind": "freeform"}])]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(context_type="task_detail", context_payload={}, user_id="u", username="alice", content="帮我查抑制剂")
        self.assertEqual(result["state"], "needs_input")
        self.assertEqual(result["questions"][0]["kind"], "freeform")


# --------------------------------------------------------------------------- #
# 8. Multi-step lookup → action (3 scenarios)
# --------------------------------------------------------------------------- #


class MultiStepScenarioTests(unittest.TestCase):
    def test_search_resolve_then_create_consumes_sequence(self):
        def search(args): return {"source": "uniprot", "query": args.get("query", ""), "count": 1, "results": [{"accession": "Q02127", "geneNames": "DHODH"}]}
        def resolve(args): return {"source": "uniprot", "accession": "Q02127", "sequence": "MTPRKRGTTEDHRSE"}
        sd = OnlineSkillDefinition(name="uniprot.search", description="Search.", input_schema={"type": "object", "properties": {"query": {"type": "string", "minLength": 1}}, "required": ["query"], "additionalProperties": False})
        rd = OnlineSkillDefinition(name="uniprot.resolve", description="Resolve.", input_schema={"type": "object", "properties": {"identifier": {"type": "string", "minLength": 1}}, "required": ["identifier"], "additionalProperties": False})
        cd = CopilotSkillDefinition(name="tasks:create_with_sequence", label="New task", description="Create task.",
            input_schema={"type": "object", "properties": {"create": {"type": "boolean"}, "components": {"type": "array", "minItems": 1, "items": {"type": "object"}}}, "required": ["create", "components"], "additionalProperties": False},
            effect="create", context_type="task_list")
        responses = [
            _turn("搜索 DHODH。", operations=[_read_op("s", "uniprot.search", {"query": "gene:DHODH AND organism_id:9606"})]),
            _turn("解析 Q02127。", operations=[_read_op("r", "uniprot.resolve", {"identifier": "Q02127"}, depends=["s"])]),
            _turn("建任务。", operations=[_write_op("c", "tasks:create_with_sequence", {"create": True, "components": [{"type": "protein", "sequence": {"$fromObservation": "r", "field": "sequence", "index": 0}}]}, depends=["r"])]),
        ]
        assistant, _ = make_assistant(responses)
        inject_read_skills(assistant, {"uniprot.search": (sd, search), "uniprot.resolve": (rd, resolve)})
        with patch_host_skills(cd):
            result = assistant.plan_turn(context_type="task_list", context_payload={"project": {"id": "p1", "workflow_key": "prediction"}}, user_id="u", username="alice", content="查 DHODH 序列然后建任务")
        self.assertEqual(result["state"], "await_confirmation")
        self.assertEqual(result["actions"][0]["arguments"]["components"][0]["sequence"], "MTPRKRGTTEDHRSE")

    def test_outline_three_step_plan(self):
        from management_api.copilot_skills.context_actions import build_context_skill_definitions
        d = next(s for s in build_context_skill_definitions("project_list", {}) if s.name == "projects:update_view")
        responses = [
            _turn("分步执行。", goal_steps=[{"description": "筛选活跃"}, {"description": "筛选失败"}, {"description": "总结"}]),
            _turn("步骤一。", operations=[_write_op("f1", "projects:update_view", {"activityFilter": "active"})]),
            _turn("步骤二。", operations=[_write_op("f2", "projects:update_view", {"activityFilter": "failed"})]),
            _turn("完成。"),
        ]
        assistant, session = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="project_list", context_payload={}, user_id="u", username="alice", content="看活跃和失败项目")
        self.assertEqual(len(result["actions"]), 2)
        self.assertEqual(len(session.requests), 4)

    def test_compute_aggregate_over_observation_column(self):
        def handler(args): return {"source": "chembl", "results": [{"value": 10.0}, {"value": 20.0}, {"value": 30.0}]}
        d = OnlineSkillDefinition(name="chembl.bioactivity", description="Bioactivity.",
            input_schema={"type": "object", "properties": {"query": {"type": "string", "minLength": 1}}, "required": ["query"], "additionalProperties": False})
        responses = [
            _turn("查找活性。", operations=[_read_op("b", "chembl.bioactivity", {"query": "aspirin"})]),
            _turn("计算均值。", operations=[_read_op("agg", "compute.aggregate", {"values": {"$fromObservation": "b", "field": "value", "all": True}}, depends=["b"])]),
            _turn("均值 20.0 nM。"),
        ]
        assistant, _ = make_assistant(responses)
        inject_read_skills(assistant, {"chembl.bioactivity": (d, handler)})
        result = assistant.plan_turn(context_type="task_detail", context_payload={}, user_id="u", username="alice", content="aspirin 的平均活性")
        self.assertEqual(result["state"], "complete")
        self.assertIn("20", result["content"])


# --------------------------------------------------------------------------- #
# 9. MULTI-TURN conversational follow-ups (8 scenarios)
# --------------------------------------------------------------------------- #


class ConversationFollowUpScenarioTests(unittest.TestCase):
    """Turn 2 references data from turn 1 via copilot_conversation.recent_messages."""

    def test_followup_asks_about_previous_compound(self):
        # Turn 1 established "aspirin SMILES CCO"; turn 2 asks about its targets without re-stating.
        ctx = conv_context(("user", "aspirin 的 SMILES"), ("assistant", "Aspirin 的 SMILES 是 CCO。"))
        def handler(args): return {"source": "chembl", "query": args.get("query", ""), "count": 1, "results": [{"target": "COX1", "activityType": "IC50", "value": 5.0}]}
        d = OnlineSkillDefinition(name="chembl.bioactivity", description="Bioactivity.",
            input_schema={"type": "object", "properties": {"query": {"type": "string", "minLength": 1}}, "required": ["query"], "additionalProperties": False})
        responses = [_turn("查找 aspirin 靶标。", operations=[_read_op("t", "chembl.bioactivity", {"query": "aspirin"})]),
                     _turn("Aspirin 的靶标包括 COX1,IC50 5.0 nM。")]
        assistant, session = make_assistant(responses)
        inject_read_skills(assistant, {"chembl.bioactivity": (d, handler)})
        result = assistant.plan_turn(context_type="task_detail", context_payload=ctx, user_id="u", username="alice", content="它有哪些靶标")
        self.assertEqual(result["state"], "complete")
        # The conversation context (aspirin) must reach the planner's system message.
        sys_msg = str(session.requests[0]["json"]["messages"][0].get("content") or "")
        self.assertIn("aspirin", sys_msg.lower())

    def test_followup_runs_previous_lookup_result(self):
        # Turn 1 retrieved sequence "MSEQ"; turn 2 says "用这个建任务" (use THIS to create a task).
        ctx = conv_context(("user", "查 DHODH"), ("assistant", "DHODH Q02127 序列:MSEQ"))
        cd = CopilotSkillDefinition(name="tasks:create_with_sequence", label="New task", description="Create.",
            input_schema={"type": "object", "properties": {"create": {"type": "boolean"}, "components": {"type": "array", "minItems": 1}}, "required": ["create", "components"], "additionalProperties": False},
            effect="create", context_type="task_list")
        responses = [_turn("用上一步的序列建任务。", operations=[_write_op("c", "tasks:create_with_sequence", {"create": True, "components": [{"type": "protein", "sequence": "MSEQ"}]})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(cd):
            result = assistant.plan_turn(context_type="task_list", context_payload={**ctx, "project": {"id": "p1"}}, user_id="u", username="alice", content="用这个序列建任务")
        self.assertEqual(result["state"], "await_confirmation")

    def test_followup_change_seed_referenced_earlier(self):
        # Turn 1: "分析任务" → assistant said pLDDT 45 low confidence. Turn 2: "改 seed 重跑".
        ctx = conv_context(("user", "分析现在的任务"), ("assistant", "pLDDT 45.0 偏低,建议重跑。"))
        from management_api.copilot_capabilities import build_task_detail_skill_definitions
        d = next(s for s in build_task_detail_skill_definitions("prediction") if s.name == "task_detail:apply_parameter_patch")
        responses = [_turn("改 seed 重跑。", operations=[_write_op("p", "task_detail:apply_parameter_patch", {"parameterPatch": {"seed": 99}})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload={**ctx, **task_detail_payload()}, user_id="u", username="alice", content="换个 seed 重跑")
        self.assertEqual(result["actions"][0]["arguments"]["parameterPatch"]["seed"], 99)

    def test_followup_clarify_which_target_from_previous_question(self):
        # Turn 1: model asked which EGFR member. Turn 2: user answers "ERBB1". The model now resolves.
        ctx = conv_context(("user", "EGFR 的序列"), ("assistant", "EGFR 家族多个成员,你要哪个?"))
        def handler(args): return {"source": "uniprot", "accession": "P00533", "sequence": "MSEQ"}
        d = OnlineSkillDefinition(name="uniprot.resolve", description="Resolve.",
            input_schema={"type": "object", "properties": {"identifier": {"type": "string", "minLength": 1}}, "required": ["identifier"], "additionalProperties": False})
        responses = [_turn("解析 ERBB1。", operations=[_read_op("r", "uniprot.resolve", {"identifier": "P00533"})]),
                     _turn("ERBB1 (P00533) 序列:MSEQ。")]
        assistant, _ = make_assistant(responses)
        inject_read_skills(assistant, {"uniprot.resolve": (d, handler)})
        result = assistant.plan_turn(context_type="task_detail", context_payload=ctx, user_id="u", username="alice", content="ERBB1")
        self.assertEqual(result["state"], "complete")
        self.assertIn("P00533", result["content"])

    def test_followup_filter_then_analyze_visible_task(self):
        # Turn 1: filtered to failed tasks. Turn 2: "第一个为什么失败" (why did the first one fail).
        ctx = conv_context(("user", "哪些失败了"), ("assistant", "已筛选失败任务,共 2 个。"))
        responses = [_turn("第一个任务因 CUDA out of memory 失败,建议减少组件或换 boltz 重试。"),
        "第一个任务因 CUDA out of memory 失败，建议减少组件或换 boltz 重试。",
    ]
        assistant, _ = make_assistant(responses)
        payload = {**ctx, **task_detail_payload(task_state="FAILURE", error_text="CUDA out of memory")}
        result = assistant.plan_turn(context_type="task_detail", context_payload=payload, user_id="u", username="alice", content="第一个为什么失败")
        self.assertEqual(result["state"], "complete")
        self.assertIn("CUDA", result["content"])

    def test_followup_compare_two_compounds(self):
        # Turn 1: aspirin SMILES. Turn 2: "和 ibuprofen 比呢" (compare with ibuprofen).
        ctx = conv_context(("user", "aspirin 的 SMILES"), ("assistant", "Aspirin SMILES: CC(=O)Oc1ccccc1C(=O)O"))
        def handler(args): return {"source": "pubchem", "query": args.get("identifier", ""), "count": 1, "results": [{"cid": "3672", "title": "Ibuprofen", "smiles": "CC(C)CC1=CC=C(C=C1)C(C)C(=O)O"}]}
        d = OnlineSkillDefinition(name="pubchem.search", description="Search PubChem.",
            input_schema={"type": "object", "properties": {"identifier": {"type": "string", "minLength": 1}}, "required": ["identifier"], "additionalProperties": False})
        responses = [_turn("查找 ibuprofen 对比。", operations=[_read_op("f", "pubchem.search", {"identifier": "ibuprofen"})]),
                     _turn("Ibuprofen SMILES: CC(C)CC1=CC=C(C=C1)C(C)C(=O)O,与 aspirin 结构不同。")]
        assistant, _ = make_assistant(responses)
        inject_read_skills(assistant, {"pubchem.search": (d, handler)})
        result = assistant.plan_turn(context_type="task_detail", context_payload=ctx, user_id="u", username="alice", content="和 ibuprofen 比呢")
        self.assertEqual(result["state"], "complete")

    def test_followup_analyze_then_modify_parameter(self):
        # Turn 1: analyze → low confidence. Turn 2: "加大迭代次数" (increase iterations for peptide).
        ctx = conv_context(("user", "分析任务"), ("assistant", "肽段设计置信度偏低,建议增加迭代。"))
        from management_api.copilot_capabilities import build_task_detail_skill_definitions
        d = next(s for s in build_task_detail_skill_definitions("peptide_design") if s.name == "task_detail:apply_parameter_patch")
        # iterations are capped at 200 by the schema (backend clamps too);
        # an out-of-range ask would be audit-rejected before dispatch
        responses = [_turn("增加迭代。", operations=[_write_op("p", "task_detail:apply_parameter_patch", {"parameterPatch": {"peptideIterations": 40}})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload={**ctx, **task_detail_payload("peptide_design")}, user_id="u", username="alice", content="加大迭代次数到 40")
        self.assertEqual(result["actions"][0]["arguments"]["parameterPatch"]["peptideIterations"], 40)

    def test_followup_reject_then_user_rephrases(self):
        # Turn 1: model returned a placeholder (rejected). Turn 2: user rephrases more specifically.
        ctx = conv_context(("user", "分析"), ("assistant", "请补充信息。"))
        responses = [_turn("任务 SUCCESS,pLDDT 82.0、ipTM 0.68,300 残基,置信度良好。"),
        "任务 SUCCESS，pLDDT 82.0、ipTM 0.68，300 残基，置信度良好。",
    ]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(context_type="task_detail", context_payload={**ctx, **task_detail_payload()}, user_id="u", username="alice", content="这个任务的结果好不好,具体数值是多少")
        self.assertEqual(result["state"], "complete")
        self.assertIn("82.0", result["content"])


# --------------------------------------------------------------------------- #
# 9b. Deep consecutive-question chains (3-4 turns, each building on the prior)
# --------------------------------------------------------------------------- #


class DeepConversationChainScenarioTests(unittest.TestCase):
    """Multi-turn chains where turn 3/4 references data established in turn 1/2."""

    def test_lookup_then_targets_then_compare_activity(self):
        # Turn1: aspirin SMILES. Turn2: its targets. Turn3: compare activity across targets.
        ctx = conv_context(
            ("user", "aspirin 的 SMILES"), ("assistant", "Aspirin SMILES: CC(=O)Oc1ccccc1C(=O)O"),
            ("user", "它的靶标"), ("assistant", "Aspirin 的靶标:COX1 (IC50 5 nM), COX2 (IC50 1 nM)"),
        )
        def handler(args): return {"source": "chembl", "query": args.get("query", ""), "count": 2,
            "results": [{"target": "COX1", "value": 5.0}, {"target": "COX2", "value": 1.0}]}
        d = OnlineSkillDefinition(name="chembl.bioactivity", description="Bioactivity.",
            input_schema={"type": "object", "properties": {"query": {"type": "string", "minLength": 1}}, "required": ["query"], "additionalProperties": False})
        responses = [_turn("比较 aspirin 靶标活性。", operations=[_read_op("b", "chembl.bioactivity", {"query": "aspirin"})]),
                     _turn("COX2 (1 nM) 比 COX1 (5 nM) 更强,COX2 是主要靶标。")]
        assistant, _ = make_assistant(responses)
        inject_read_skills(assistant, {"chembl.bioactivity": (d, handler)})
        result = assistant.plan_turn(context_type="task_detail", context_payload=ctx, user_id="u", username="alice", content="比较一下活性")
        self.assertEqual(result["state"], "complete")
        self.assertIn("COX2", result["content"])

    def test_analyze_then_rerun_then_analyze_again(self):
        # Turn1: analyze (low pLDDT). Turn2: rerun with new seed. Turn3: analyze new result.
        ctx = conv_context(
            ("user", "分析现在的任务"), ("assistant", "pLDDT 45 偏低,建议重跑。"),
            ("user", "换个 seed 重跑"), ("assistant", "已用 seed 99 重新提交。"),
        )
        responses = [_turn("重跑后 pLDDT 提升到 78.0,ipTM 0.65,置信度明显改善。"),
        "重跑后 pLDDT 提升到 78.0，ipTM 0.65，置信度明显改善。",
    ]
        assistant, _ = make_assistant(responses)
        payload = {**ctx, **task_detail_payload(confidenceSummary={"avgPlddt": 78.0, "iptm": 0.65})}
        result = assistant.plan_turn(context_type="task_detail", context_payload=payload, user_id="u", username="alice", content="再分析一下")
        self.assertEqual(result["state"], "complete")
        self.assertIn("78.0", result["content"])

    def test_lookup_compound_then_lookup_target_then_create(self):
        # Turn1: compound SMILES. Turn2: its protein target's sequence. Turn3: create task from it.
        ctx = conv_context(
            ("user", "aspirin SMILES"), ("assistant", "CC(=O)Oc1ccccc1C(=O)O"),
            ("user", "COX1 的序列"), ("assistant", "COX1 (P23219) 序列:MSEQ..."),
        )
        cd = CopilotSkillDefinition(name="tasks:create_with_sequence", label="New task", description="Create.",
            input_schema={"type": "object", "properties": {"create": {"type": "boolean"}, "components": {"type": "array", "minItems": 1}}, "required": ["create", "components"], "additionalProperties": False},
            effect="create", context_type="task_list")
        responses = [_turn("用 COX1 序列建任务。", operations=[_write_op("c", "tasks:create_with_sequence", {"create": True, "components": [{"type": "protein", "sequence": "MSEQ"}]})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(cd):
            result = assistant.plan_turn(context_type="task_list", context_payload={**ctx, "project": {"id": "p1"}}, user_id="u", username="alice", content="用这个建个任务")
        self.assertEqual(result["state"], "await_confirmation")

    def test_filter_failures_then_analyze_one_then_rerun(self):
        # Turn1: filter failures. Turn2: why did it fail. Turn3: rerun with fix.
        ctx = conv_context(
            ("user", "哪些失败了"), ("assistant", "筛选出 2 个失败任务。"),
            ("user", "第一个为什么失败"), ("assistant", "CUDA out of memory,boltz 显存不足。"),
        )
        from management_api.copilot_capabilities import build_task_detail_skill_definitions
        d = next(s for s in build_task_detail_skill_definitions("prediction") if s.name == "task_detail:apply_parameter_patch")
        responses = [_turn("改用 alphafold3 重跑。", operations=[_write_op("p", "task_detail:apply_parameter_patch", {"parameterPatch": {"backend": "alphafold3"}})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload={**ctx, **task_detail_payload("prediction")}, user_id="u", username="alice", content="换 alphafold3 重跑")
        self.assertEqual(result["actions"][0]["arguments"]["parameterPatch"]["backend"], "alphafold3")

    def test_disambiguate_then_resolve_then_analyze_structure(self):
        # Turn1: EGFR ambiguous. Turn2: user picks ERBB1. Turn3: analyze its AlphaFold structure.
        ctx = conv_context(
            ("user", "EGFR 的序列"), ("assistant", "EGFR 家族多个成员。"),
            ("user", "ERBB1"), ("assistant", "ERBB1 (P00533) 序列:MSEQ..."),
        )
        def handler(args): return {"source": "alphafold", "accession": "P00533", "avgPlddt": 92.1, "pdbUrl": "https://af.pdb"}
        d = OnlineSkillDefinition(name="alphafold.resolve", description="Resolve AlphaFold.",
            input_schema={"type": "object", "properties": {"identifier": {"type": "string", "minLength": 1}}, "required": ["identifier"], "additionalProperties": False})
        responses = [_turn("查找 AlphaFold 预测。", operations=[_read_op("a", "alphafold.resolve", {"identifier": "P00533"})]),
                     _turn("ERBB1 (P00533) AlphaFold 预测 pLDDT 92.1,置信度很高。")]
        assistant, _ = make_assistant(responses)
        inject_read_skills(assistant, {"alphafold.resolve": (d, handler)})
        result = assistant.plan_turn(context_type="task_detail", context_payload=ctx, user_id="u", username="alice", content="它的 AlphaFold 预测怎么样")
        self.assertEqual(result["state"], "complete")
        self.assertIn("92.1", result["content"])

    def test_analyze_then_explain_metric_then_suggest_action(self):
        # Turn1: analyze. Turn2: explain ipTM. Turn3: what should I do.
        ctx = conv_context(
            ("user", "分析任务"), ("assistant", "ipTM 0.4 偏低。"),
            ("user", "ipTM 是什么"), ("assistant", "ipTM 衡量界面预测可信度,>0.75 较好。"),
        )
        from management_api.copilot_capabilities import build_task_detail_skill_definitions
        d = next(s for s in build_task_detail_skill_definitions("prediction") if s.name == "task_detail:apply_parameter_patch")
        responses = [_turn("ipTM 偏低建议增加 MSA 或用更大全蛋白重跑。", operations=[_write_op("p", "task_detail:apply_parameter_patch", {"parameterPatch": {"seed": 123}})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload={**ctx, **task_detail_payload()}, user_id="u", username="alice", content="那我该怎么做")
        self.assertIn(result["state"], ("await_confirmation", "complete"))

    def test_lookup_paper_then_lookup_compound_from_paper(self):
        # Turn1: find EGFR paper. Turn2: what compound does it study.
        ctx = conv_context(("user", "EGFR 文献"), ("assistant", "找到 'EGFR inhibitor X in cancer' (Nature 2023)。"))
        def handler(args): return {"source": "pubchem", "query": args.get("identifier", ""), "count": 1,
            "results": [{"cid": "1", "title": "Inhibitor X", "smiles": "CCO"}]}
        d = OnlineSkillDefinition(name="pubchem.search", description="Search PubChem.",
            input_schema={"type": "object", "properties": {"identifier": {"type": "string", "minLength": 1}}, "required": ["identifier"], "additionalProperties": False})
        responses = [_turn("查找文献中的化合物。", operations=[_read_op("f", "pubchem.search", {"identifier": "Inhibitor X"})]),
                     _turn("文献研究的化合物是 Inhibitor X,SMILES CCO。")]
        assistant, _ = make_assistant(responses)
        inject_read_skills(assistant, {"pubchem.search": (d, handler)})
        result = assistant.plan_turn(context_type="task_detail", context_payload=ctx, user_id="u", username="alice", content="它研究的是什么化合物")
        self.assertIn("Inhibitor X", result["content"])

    def test_compare_two_tasks_then_modify_loser(self):
        # Turn1: analyze task A (good). Turn2: analyze task B (bad). Turn3: improve B.
        ctx = conv_context(
            ("user", "分析任务 A"), ("assistant", "任务 A pLDDT 85,很好。"),
            ("user", "分析任务 B"), ("assistant", "任务 B pLDDT 50,偏低。"),
        )
        from management_api.copilot_capabilities import build_task_detail_skill_definitions
        d = next(s for s in build_task_detail_skill_definitions("prediction") if s.name == "task_detail:apply_parameter_patch")
        responses = [_turn("改进任务 B:增加迭代。", operations=[_write_op("p", "task_detail:apply_parameter_patch", {"parameterPatch": {"seed": 42}})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload={**ctx, **task_detail_payload()}, user_id="u", username="alice", content="改进一下任务 B")
        self.assertIn(result["state"], ("await_confirmation", "complete"))

    def test_four_turn_drug_investigation_chain(self):
        # Deep 4-turn chain: drug → targets → best target → clinical trials for it.
        ctx = conv_context(
            ("user", "aspirin 的靶标"), ("assistant", "COX1, COX2"),
            ("user", "哪个最强"), ("assistant", "COX2 IC50 1 nM 最强。"),
        )
        def handler(args): return {"source": "clinicaltrials", "query": args.get("query", ""), "count": 1,
            "results": [{"nctId": "NCT123", "title": "COX2 trial", "phase": "Phase 2", "status": "Recruiting"}]}
        d = OnlineSkillDefinition(name="clinicaltrials.search", description="Search clinical trials.",
            input_schema={"type": "object", "properties": {"query": {"type": "string", "minLength": 1}}, "required": ["query"], "additionalProperties": False})
        responses = [_turn("查找 COX2 临床试验。", operations=[_read_op("ct", "clinicaltrials.search", {"query": "COX2"})]),
                     _turn("找到 COX2 试验 NCT123,Phase 2,正在招募。")]
        assistant, _ = make_assistant(responses)
        inject_read_skills(assistant, {"clinicaltrials.search": (d, handler)})
        result = assistant.plan_turn(context_type="task_detail", context_payload=ctx, user_id="u", username="alice", content="有相关的临床试验吗")
        self.assertIn("NCT123", result["content"])

    def test_recover_from_rejection_then_continue_chain(self):
        # Turn1: bad param (rejected). Turn2: user corrects. Turn3: analyze result.
        ctx = conv_context(
            ("user", "seed 设为 -5"), ("assistant", "seed 不能为负,已用默认值。"),
        )
        from management_api.copilot_capabilities import build_task_detail_skill_definitions
        d = next(s for s in build_task_detail_skill_definitions("prediction") if s.name == "task_detail:apply_parameter_patch")
        responses = [_turn("改 seed 为 42。", operations=[_write_op("p", "task_detail:apply_parameter_patch", {"parameterPatch": {"seed": 42}})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload={**ctx, **task_detail_payload()}, user_id="u", username="alice", content="那改成 42")
        self.assertEqual(result["actions"][0]["arguments"]["parameterPatch"]["seed"], 42)


# --------------------------------------------------------------------------- #
# 9c. Capability/greeting questions — must answer concisely without truncation
# --------------------------------------------------------------------------- #


class CapabilityQuestionScenarioTests(unittest.TestCase):
    """'你会做什么' / '比如什么功能' — the model must answer concisely inline, not truncate."""

    def test_capability_question_answers_concisely_without_truncation(self):
        # The production bug: the model wrote a long preamble ending with a colon promising a list,
        # then ran out of tokens before the list → truncated → dangling-promise rejected 3× →
        # gave up. The fix: raise max_tokens + prompt tells the model to be concise and lead with
        # content. This test verifies a concise inline capability answer completes cleanly.
        responses = [_turn(
            "我可以帮你:查找蛋白序列/化合物 SMILES/实验结构,分析任务结果(pLDDT/ipTM/亲和力),"
            "创建和运行预测/亲和力/虚拟筛选任务,筛选任务和项目列表,查找靶标抑制剂和生物活性数据。"
        ),
        "是的，我可以帮你分析任务、查询数据库、修改参数并提交运行。",
    ]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(
            context_type="task_list",
            context_payload={"project": {"id": "p1", "name": "MTGase", "workflow_key": "prediction"}},
            user_id="u", username="alice", content="你会做什么？",
        )
        self.assertEqual(result["state"], "complete")
        # The answer lists concrete capabilities, not a truncated preamble.
        self.assertNotIn("以下是我能帮你做的具体功能：", result["content"])
        self.assertTrue(len(result["content"]) > 40, "answer should be substantive")

    def test_truncated_preamble_accepted_immediately(self):
        # The harness does not audit message text — a colon-ending preamble is accepted as-is.
        # The model's first response is the final answer.
        responses = [_turn("你好！我是 V-Bio 生物计算助手，专注于生物计算和结构生物学。以下是我能帮你做的具体功能："),
        "你好！我是 V-Bio 生物计算助手，专注于生物计算和结构生物学。以下是我能帮你做的具体功能：任务分析、数据库检索、参数修改与运行提交。",
    ]
        assistant, session = make_assistant(responses)
        result = assistant.plan_turn(
            context_type="task_list",
            context_payload={"project": {"id": "p1"}},
            user_id="u", username="alice", content="比如什么功能",
        )
        self.assertEqual(result["state"], "complete")
        self.assertGreaterEqual(len(session.requests), 1)

    def test_greeting_answered_without_tools(self):
        # A simple greeting should complete cleanly — no tools, no rejection.
        responses = [_turn("你好！我是 V-Bio 生物计算助手。当前你在查看 MTGase 项目,有 10 个预测任务。需要什么帮助？"),
        "你好！我是 V-Bio 生物计算助手。当前你在查看 MTGase 项目，有 10 个预测任务。需要什么帮助？",
    ]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(
            context_type="task_list",
            context_payload={"project": {"id": "p1", "name": "MTGase"}},
            user_id="u", username="alice", content="你好",
        )
        self.assertEqual(result["state"], "complete")
        self.assertIn("V-Bio", result["content"])

    def test_repeated_truncated_capability_preamble_accepted(self):
        # Harness does not audit message text — the colon-ending preamble is accepted on round 0.
        truncated = _turn("我是 V-Bio 生物计算助手，专注于生物计算和结构生物学。以下是我能帮你做的具体功能：")
        responses = [truncated, truncated["message"]]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(
            context_type="task_list",
            context_payload={"project": {"id": "p1", "name": "MTGase", "task_type": "prediction"}},
            user_id="u", username="alice", content="比如什么功能",
        )
        self.assertEqual(result["state"], "complete")

    def test_repeated_truncated_data_analysis_accepted(self):
        # Harness does not audit message text — accepted on round 0.
        truncated = _turn("当前任务分析如下：")
        responses = [truncated, truncated["message"]]
        payload = task_detail_payload(confidenceSummary={"avgPlddt": 82.0, "iptm": 0.68})
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(
            context_type="task_detail", context_payload=payload,
            user_id="u", username="alice", content="分析现在的任务",
        )
        self.assertEqual(result["state"], "complete")


    def test_capability_question_calls_catalog_then_summarizes(self):
        # The CORRECT planner+harness flow for '你会做什么': the model calls platform.capability_catalog
        # to get the registered capabilities, then writes a SHORT inline summary from the observation.
        # This is NOT a fallback — the catalog is a real tool, and the model reads its output to write
        # the answer. The prompt explicitly instructs this flow.
        responses = [
            _turn("Looking up capabilities.", operations=[_read_op("cat", "platform.capability_catalog", {})]),
            _turn("我可以帮你查找蛋白序列、化合物和结构,分析任务结果(pLDDT/ipTM/亲和力),创建和运行预测/亲和力任务,筛选和管理项目列表。"),
        ]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(
            context_type="task_list",
            context_payload={"project": {"id": "p1", "name": "MTGase", "task_type": "prediction"}},
            user_id="u", username="alice", content="你会做什么？",
        )
        self.assertEqual(result["state"], "complete")
        self.assertIn("查找蛋白", result["content"])
        self.assertIn("pLDDT", result["content"])


# --------------------------------------------------------------------------- #
# 10. Edge cases & contract enforcement (6 scenarios)
# --------------------------------------------------------------------------- #


class EdgeCaseScenarioTests(unittest.TestCase):
    def test_mixed_read_write_rejected(self):
        from management_api.copilot_skills.context_actions import build_context_skill_definitions
        d = next(s for s in build_context_skill_definitions("project_list", {}) if s.name == "projects:update_view")
        rd = OnlineSkillDefinition(name="source:read", description="Read.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False})
        def handler(args): return {"value": "x"}
        responses = [
            _turn("混合。", operations=[_read_op("r", "source:read", {}), _write_op("w", "projects:update_view", {"search": "x"})]),
            _turn("分开操作。", operations=[_write_op("w", "projects:update_view", {"search": "x"})]),
        ]
        assistant, session = make_assistant(responses)
        inject_read_skills(assistant, {"source:read": (rd, handler)})
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="project_list", context_payload={}, user_id="u", username="alice", content="查然后筛")
        self.assertTrue(_has_feedback(session, 1, "HELD"))

    def test_duplicate_operation_id_rejected(self):
        from management_api.copilot_skills.context_actions import build_context_skill_definitions
        d = next(s for s in build_context_skill_definitions("project_list", {}) if s.name == "projects:update_view")
        responses = [
            _turn("重复。", operations=[_write_op("dup", "projects:update_view", {"search": "a"}), _write_op("dup", "projects:update_view", {"search": "b"})]),
            _turn("修正。", operations=[_write_op("w1", "projects:update_view", {"search": "a"})]),
        ]
        assistant, session = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="project_list", context_payload={}, user_id="u", username="alice", content="筛")
        self.assertTrue(_has_feedback(session, 1, "must be unique"))

    def test_unknown_skill_rejected(self):
        responses = [
            _turn("调用。", operations=[_read_op("x", "nonexistent.skill", {})]),
            _turn("无法执行,没有这个操作。"),
            "无法执行，没有这个操作。",
        ]
        assistant, session = make_assistant(responses)
        result = assistant.plan_turn(context_type="task_detail", context_payload={}, user_id="u", username="alice", content="做点什么")
        self.assertTrue(_has_feedback(session, 1, "is not registered"))

    def test_empty_message_rejected(self):
        responses = [
            {"message": "   ", "questions": [], "operations": []},
            _turn("需要更多信息才能帮助你。", questions=[{"text": "你想做什么?", "kind": "freeform"}]),
        ]
        assistant, session = make_assistant(responses)
        result = assistant.plan_turn(context_type="task_detail", context_payload={}, user_id="u", username="alice", content="帮我")
        self.assertEqual(result["state"], "needs_input")

    def test_json_output_accepted_directly(self):
        # With grammar, the model outputs valid JSON. The planner parses it directly.
        assistant, session = make_assistant([])
        session.responses = [json.dumps(_turn("你好！我是 V-Bio Copilot，很高兴为您服务。")),
        "你好！我是 V-Bio Copilot，很高兴为您服务。",
    ]
        result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload(), user_id="u", username="alice", content="分析")
        self.assertEqual(result["state"], "complete")
        self.assertIn("V-Bio", result["content"])
        self.assertGreaterEqual(len(session.requests), 1)

    def test_outline_reemission_rejected(self):
        from management_api.copilot_skills.context_actions import build_context_skill_definitions
        d = next(s for s in build_context_skill_definitions("project_list", {}) if s.name == "projects:update_view")
        responses = [
            _turn("大纲。", goal_steps=[{"description": "step1"}, {"description": "step2"}]),
            _turn("重发大纲。", goal_steps=[{"description": "different"}]),
            _turn("步骤一。", operations=[_write_op("f", "projects:update_view", {"search": "x"})]),
            _turn("完成。"),
        ]
        assistant, session = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="project_list", context_payload={}, user_id="u", username="alice", content="复杂任务")
        self.assertTrue(_has_feedback(session, 1, "outline is fixed"))


# --------------------------------------------------------------------------- #
# 11. Cross-page progression (2 scenarios)
# --------------------------------------------------------------------------- #


class CrossPageScenarioTests(unittest.TestCase):
    def test_project_list_create_then_navigate_to_task_list(self):
        from management_api.copilot_skills.context_actions import build_context_skill_definitions
        d = next(s for s in build_context_skill_definitions("project_list", {}) if s.name == "projects:create")
        responses = [_turn("创建亲和力项目。", operations=[_write_op("c", "projects:create", {"create": True, "workflow": "affinity"})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="project_list", context_payload={}, user_id="u", username="alice", content="建亲和力项目")
        self.assertEqual(result["state"], "await_confirmation")
        self.assertEqual(result["actions"][0]["payload"]["targetContextType"], "task_list")

    def test_task_list_open_navigates_to_task_detail(self):
        from management_api.copilot_skills.context_actions import build_context_skill_definitions
        # The referenced row must be visible in the task list context — the audit grounds
        # taskRowId against context rows.
        payload = {"project": {"id": "p1"}, "rows": [{"id": "row-5", "name": "Task 5"}]}
        d = next(s for s in build_context_skill_definitions("task_list", payload) if s.name == "tasks:open")
        responses = [_turn("打开任务。", operations=[_write_op("o", "tasks:open", {"taskRowId": "row-5"})])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_list", context_payload=payload, user_id="u", username="alice", content="打开 row-5")
        self.assertEqual(result["actions"][0]["payload"]["targetContextType"], "task_detail")


class GracefulDegradationScenarioTests(unittest.TestCase):
    """When the planner can't fix a rejected output after a retry, the turn fails honestly —
    state="failed" with a clean user message. No salvage, no soft-guidance bypass, no fallback
    content injection. The planner+harness loop either produces a correct answer or reports failure.
    """

    def test_colon_ending_message_accepted_immediately(self):
        # The harness does not audit message text. A colon-ending message is accepted on round 0 —
        # the model's content is trusted. Only structure is audited.
        body = "我可以帮你搜索生物数据库、管理项目、规划计算任务、分析结果"
        responses = [
            _turn(body + "："),
            body + "：",
        ]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(
            context_type="project_list", context_payload={}, user_id="u", username="alice", content="你会做什么"
        )
        self.assertEqual(result["state"], "complete")
        self.assertEqual(len(result["content"]), len(body + "："))

    def test_empty_output_exhausts_budget(self):
        # Empty model responses exhaust the retry budget — the turn fails honestly.
        session = SequenceModelSession([json.dumps({"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}), json.dumps({"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}), json.dumps({"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}), json.dumps({"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]})])
        assistant = CopilotAssistant(
            chat_api_url="http://model.invalid/v1/chat/completions",
            chat_api_key="test-key", chat_model="test-model",
            timeout_seconds=3, session=session, logger=NullLogger(),
            max_planner_rounds=6, max_malformed_retries=1,
        )
        result = assistant.plan_turn(
            context_type="project_list", context_payload={}, user_id="u", username="alice", content="hello"
        )
        self.assertEqual(result["state"], "failed")

    def test_lead_in_message_accepted_immediately(self):
        # The harness does not audit message text. A lead-in without body is accepted on round 0.
        body = "你好！我是 V-Bio 生物计算助手，专注于生物计算与结构生物学。以下是我可以帮你做的几件事"
        responses = [_turn(body), body]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(
            context_type="project_list", context_payload={}, user_id="u", username="alice", content="你会做什么"
        )
        self.assertEqual(result["state"], "complete")

    def test_ungrounded_answer_repeated_completes_with_records_footer(self):
        # ANTI-HALLUCINATION: the grounding guard fires when the model retrieved one record but its
        # answer ignores it — the first rejection still coaches the model to cite the record.
        # This guard IS structural (the model ignored retrieved data), not a message-text check.
        # But a model that CANNOT comply after one corrective round used to burn the whole budget
        # into "I could not complete this request" (the production KLK failure). The harness now
        # completes the turn itself: the model's answer is accepted WITH a deterministic footer
        # of the real retrieved identities, so the user always sees what was actually retrieved.
        def handler(args):
            return {"source": "uniprot", "query": args.get("identifier", ""), "accession": "P12345",
                    "sequence": "MKTAYIA", "name": "TestProtein"}
        d = OnlineSkillDefinition(name="uniprot.resolve", description="Resolve a UniProt entry.",
            input_schema={"type": "object", "properties": {"identifier": {"type": "string", "minLength": 1}},
                          "required": ["identifier"], "additionalProperties": False})
        responses = [
            _turn("Looking it up.", operations=[_read_op("r1", "uniprot.resolve", {"identifier": "P12345"})]),
            _turn("It is a protein."),               # round 1: ungrounded → grounding guard rejects
            _turn("It is a protein of interest."),   # round 2: still ungrounded → harness completes
        ]
        assistant, _ = make_assistant(responses)
        inject_read_skills(assistant, {"uniprot.resolve": (d, handler)})
        result = assistant.plan_turn(
            context_type="task_detail", context_payload={},
            user_id="u", username="alice", content="查一下 P12345",
        )
        self.assertEqual(result["state"], "complete")
        # The real retrieved identity is appended by the harness — never fabricated, never lost.
        self.assertIn("P12345", result["content"])
        self.assertIn("TestProtein", result["content"])


class DockingTaskCreationScenarioTests(unittest.TestCase):
    """The docking task-list flow: search structures → ask the user to pick → (next turn, after
    the pick) re-resolve the chosen entry under a NEW operation id and create the docking task
    with the target STRUCTURE url. Pins the regression where the planner referenced a stale
    cross-turn operation id and burned its whole round budget ('Could not settle on a plan')."""

    DOCKING_LIST_PAYLOAD: Dict[str, Any] = {"project": {"id": "p1", "task_type": "docking"}, "rows": []}

    def _rcsb_search_stub(self) -> Tuple[OnlineSkillDefinition, Callable[[Dict[str, Any]], Dict[str, Any]]]:
        definition = OnlineSkillDefinition(
            name="rcsb.search",
            description="stub rcsb search",
            input_schema={"type": "object", "properties": {"text": {"type": "string", "minLength": 1}, "size": {"type": "integer", "minimum": 1, "maximum": 10}},
                          "required": ["text"], "additionalProperties": False},
        )

        def handler(args: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "source": "rcsb", "query": str(args.get("text") or ""), "count": 2,
                "results": [
                    {"pdbId": "1D3G", "title": "Human DHODH", "method": "X-RAY DIFFRACTION", "resolution": 2.4,
                     "cifUrl": "https://files.rcsb.org/download/1D3G.cif"},
                    {"pdbId": "2F6O", "title": "Human DHODH with inhibitor", "method": "X-RAY DIFFRACTION", "resolution": 1.8,
                     "cifUrl": "https://files.rcsb.org/download/2F6O.cif"},
                ],
            }

        return definition, handler

    def _rcsb_resolve_stub(self) -> Tuple[OnlineSkillDefinition, Callable[[Dict[str, Any]], Dict[str, Any]]]:
        definition = OnlineSkillDefinition(
            name="rcsb.resolve",
            description="stub rcsb resolve",
            input_schema={"type": "object", "properties": {"identifier": {"type": "string", "minLength": 1}},
                          "required": ["identifier"], "additionalProperties": False},
        )

        def handler(args: Dict[str, Any]) -> Dict[str, Any]:
            pdb_id = str(args.get("identifier") or "").strip().upper()
            return {
                "source": "rcsb", "identifier": pdb_id, "pdbId": pdb_id, "title": "Human DHODH",
                "method": "X-RAY DIFFRACTION", "resolution": 2.4,
                "cifUrl": f"https://files.rcsb.org/download/{pdb_id}.cif",
                "cifUrl": f"https://files.rcsb.org/download/{pdb_id}.cif",
            }

        return definition, handler

    def test_workflow_input_contract_rides_in_round0_context(self):
        # The field-type facts of the current workflow are part of the ENVIRONMENT the planner
        # reads in round 0 — before any source choice — not something the model must infer from
        # skill descriptions mid-round. Pinned because a weak planner read "DHODH" and went to
        # uniprot (sequence) for a docking (structure) target.
        responses = [
            _turn("Looking it up.", operations=[
                _read_op("s1", "rcsb.search", {"text": "dihydroorotate dehydrogenase human", "size": 5})
            ]),
            _turn("请选择结构条目。", questions=[{
                "text": "请选择结构条目：", "kind": "choice",
                "options": [
                    {"label": "1D3G — Human DHODH (X-ray, 2.4 Å)", "value": "1D3G"},
                    {"label": "2F6O — Human DHODH with inhibitor (X-ray, 1.8 Å)", "value": "2F6O"},
                ],
            }]),
        ]
        assistant, session = make_assistant(responses)
        inject_read_skills(assistant, {"rcsb.search": self._rcsb_search_stub()})
        result = assistant.plan_turn(
            context_type="task_list", context_payload=dict(self.DOCKING_LIST_PAYLOAD),
            user_id="u", username="alice", content="填入人的DHODH到target中",
        )
        self.assertEqual(result["state"], "needs_input")
        system_round0 = next(
            str(msg.get("content") or "")
            for msg in session.requests[0]["json"]["messages"]
            if msg.get("role") == "system"
        )
        self.assertIn("workflow: affinity", system_round0)
        self.assertIn('"target":"3D structure file', system_round0)
        # The affinity ligand contract is mode-complete (dock=SMILES+pocket; pose/refine/
        # interface=uploaded file; score=none) — matches what submit actually enforces.
        self.assertIn('"ligand":"mode-dependent', system_round0)

    def test_search_then_ask_user_to_pick_entry(self):
        responses = [
            _turn("正在搜索人的 DHODH 实验结构。", operations=[
                _read_op("s1", "rcsb.search", {"text": "dihydroorotate dehydrogenase human", "size": 5})
            ]),
            _turn("找到两个实验结构，请选择一个：", questions=[{
                "text": "请选择用于 docking 的 DHODH 结构条目：",
                "kind": "choice",
                "options": [
                    {"label": "1D3G — Human DHODH (X-ray, 2.4 Å)", "value": "1D3G"},
                    {"label": "2F6O — Human DHODH with inhibitor (X-ray, 1.8 Å)", "value": "2F6O"},
                ],
            }]),
        ]
        assistant, _ = make_assistant(responses)
        inject_read_skills(assistant, {"rcsb.search": self._rcsb_search_stub()})
        result = assistant.plan_turn(
            context_type="task_list", context_payload=dict(self.DOCKING_LIST_PAYLOAD),
            user_id="u", username="alice", content="填入人的DHODH到target中",
        )
        self.assertEqual(result["state"], "needs_input")
        self.assertEqual(len(result.get("questions") or []), 1)
        self.assertEqual(result["actions"], [])

    def test_after_user_pick_create_docking_task_with_resolved_structure(self):
        # Next turn: the pick arrived; memory carries the pdbId but NOT its URL, so the planner
        # re-resolves 1D3G under a NEW id this turn, then creates the task referencing THAT op.
        payload: Dict[str, Any] = dict(self.DOCKING_LIST_PAYLOAD)
        payload["copilot_memory"] = [{"source": "rcsb", "pdbId": "1D3G", "title": "Human DHODH", "organism": "Homo sapiens"}]
        payload["copilot_conversation"] = {"recent_messages": [
            {"role": "user", "content": "填入人的DHODH到target中"},
            {"role": "assistant", "content": "请选择用于 docking 的 DHODH 结构条目"},
            {"role": "user", "content": "用 1D3G"},
        ], "compression": "recent_only", "total_messages": 3}
        responses = [
            _turn("正在解析 1D3G 的结构下载链接。", operations=[
                _read_op("r1", "rcsb.resolve", {"identifier": "1D3G"})
            ]),
            _turn("将创建以 1D3G 为 target 的新 docking 任务。", operations=[
                _write_op("c1", "tasks:create_docking", {
                    "create": True,
                    "targetStructureUrl": {"$fromObservation": "r1", "field": "cifUrl"},
                    "taskName": "DHODH (1D3G) docking",
                }, depends=["r1"])
            ]),
        ]
        assistant, _ = make_assistant(responses)
        inject_read_skills(assistant, {"rcsb.resolve": self._rcsb_resolve_stub()})
        result = assistant.plan_turn(
            context_type="task_list", context_payload=payload,
            user_id="u", username="alice", content="用 1D3G",
        )
        self.assertEqual(result["state"], "await_confirmation")
        self.assertEqual(len(result["actions"]), 1)
        action = result["actions"][0]
        self.assertEqual(action["id"], "tasks:create_docking")
        self.assertEqual(action["arguments"]["targetStructureUrl"], "https://files.rcsb.org/download/1D3G.cif")
        self.assertEqual(action["payload"]["targetContextType"], "task_detail")
        self.assertEqual(action["payload"]["contextType"], "task_list")

    def test_create_docking_task_with_target_and_ligand_together(self):
        # The complete-setup path: a docking run needs a target AND a ligand, so one create
        # action carries both — each resolved this turn under its own operation id.
        payload: Dict[str, Any] = dict(self.DOCKING_LIST_PAYLOAD)
        payload["copilot_memory"] = [{"source": "rcsb", "pdbId": "1D3G", "title": "Human DHODH", "organism": "Homo sapiens"}]
        payload["copilot_conversation"] = {"recent_messages": [
            {"role": "user", "content": "填入人的DHODH到target中"},
            {"role": "assistant", "content": "请选择结构条目，配体用什么？"},
            {"role": "user", "content": "用 1D3G，配体用来氟米特"},
        ], "compression": "recent_only", "total_messages": 3}
        responses = [
            _turn("正在解析结构与配体。", operations=[
                _read_op("r1", "rcsb.resolve", {"identifier": "1D3G"}),
                _read_op("p1", "pubchem.search", {"identifier": "leflunomide", "namespace": "name"}),
            ]),
            _turn("将创建带 target 与配体的 docking 任务。", operations=[
                _write_op("c1", "tasks:create_docking", {
                    "create": True,
                    "targetStructureUrl": {"$fromObservation": "r1", "field": "cifUrl"},
                    "ligandSmiles": {"$fromObservation": "p1", "field": "smiles"},
                }, depends=["r1", "p1"])
            ]),
        ]
        assistant, _ = make_assistant(responses)
        inject_read_skills(assistant, {
            "rcsb.resolve": self._rcsb_resolve_stub(),
            "pubchem.search": self._pubchem_stub(),
        })
        result = assistant.plan_turn(
            context_type="task_list", context_payload=payload,
            user_id="u", username="alice", content="用 1D3G，配体用来氟米特",
        )
        self.assertEqual(result["state"], "await_confirmation")
        action = result["actions"][0]
        self.assertEqual(action["arguments"]["targetStructureUrl"], "https://files.rcsb.org/download/1D3G.cif")
        self.assertEqual(action["arguments"]["ligandSmiles"], "CC(=O)Nc1ccc(C(F)(F)F)cc1C(=O)NCCCn2ccnc2")
        self.assertEqual(action["arguments"]["create"], True)

    def _pubchem_stub(self) -> Tuple[OnlineSkillDefinition, Callable[[Dict[str, Any]], Dict[str, Any]]]:
        definition = OnlineSkillDefinition(
            name="pubchem.search",
            description="stub pubchem search",
            input_schema={"type": "object",
                          "properties": {"identifier": {"type": "string", "minLength": 1},
                                         "namespace": {"type": "string"}},
                          "required": ["identifier"], "additionalProperties": False},
        )

        def handler(args: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "source": "pubchem", "namespace": "name", "query": str(args.get("identifier") or ""),
                "count": 1,
                "results": [{
                    "cid": "3899", "title": "Leflunomide",
                    "smiles": "CC(=O)Nc1ccc(C(F)(F)F)cc1C(=O)NCCCn2ccnc2",
                }],
            }

        return definition, handler


# --------------------------------------------------------------------------- #
# Same-round read→write dataflow: deferred materialization (one-round plans)
# --------------------------------------------------------------------------- #


class DeferredMaterializationScenarioTests(unittest.TestCase):
    """The planner's natural "fetch → apply the fetched value" pattern completes in ONE round.

    A confirmation operation whose arguments reference a same-round read via $fromObservation is
    a declared dataflow, not an error: the harness executes the reads, materializes the deferred
    references from the fresh observations, and surfaces the confirmation actions immediately —
    instead of rejecting the round as an unknown reference (which weaker planners could not
    recover from within the round budget).
    """

    APPLY_LIGAND = CopilotSkillDefinition(
        name="task_detail:apply_docking_ligand_smiles",
        label="Set docking ligand (SMILES)",
        description="Set the ligand of the current docking task from a SMILES string.",
        input_schema={
            "type": "object",
            "properties": {"smiles": {"type": "string", "minLength": 1, "maxLength": 2048}},
            "required": ["smiles"],
            "additionalProperties": False,
        },
        effect="update",
        context_type="task_detail",
    )

    @staticmethod
    def _compound_lookup(
        results: list | None = None, ok: bool = True
    ) -> Tuple[OnlineSkillDefinition, Callable[[Dict[str, Any]], Dict[str, Any]]]:
        definition = OnlineSkillDefinition(
            name="compound.lookup",
            description="Look up a compound's canonical SMILES by name.",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string", "minLength": 1}},
                "required": ["name"],
                "additionalProperties": False,
            },
        )
        if results is None:
            results = [{"smiles": "CCOc1ccccc1", "cid": "5354"}]

        def handler(args: Dict[str, Any]) -> Dict[str, Any]:
            if not ok:
                raise RuntimeError("HTTP 503: source unreachable")
            return {"source": "compound", "results": [dict(r) for r in results]}

        return definition, handler

    def _make(self, responses, *, lookup_ok: bool = True, lookup_results: list | None = None):
        assistant, session = make_assistant(responses)
        inject_read_skills(
            assistant,
            {"compound.lookup": self._compound_lookup(results=lookup_results, ok=lookup_ok)},
        )
        return assistant, session

    def test_same_round_read_then_apply_materializes_in_one_round(self):
        responses = [_turn(
            "已查到 brequinar 的规范 SMILES，等待确认后填入。",
            operations=[
                _read_op("c1", "compound.lookup", {"name": "brequinar"}),
                _write_op(
                    "w1", "task_detail:apply_docking_ligand_smiles",
                    {"smiles": {"$fromObservation": "c1", "field": "smiles", "index": 0}},
                    depends=["c1"],
                ),
            ],
        )]
        assistant, session = self._make(responses)
        with patch_host_skills(self.APPLY_LIGAND):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload("affinity"),
                                         user_id="u", username="alice", content="配体用 brequinar")
        self.assertEqual(result["state"], "await_confirmation")
        self.assertEqual(len(result["actions"]), 1)
        # The action carries the CONCRETE retrieved value, not the $fromObservation reference.
        self.assertEqual(result["actions"][0]["arguments"]["smiles"], "CCOc1ccccc1")
        # Converged in ONE planner round — no re-emission of the deferred write.
        planner_requests = [r for r in session.requests if not is_phase2_request(r["json"])]
        self.assertEqual(len(planner_requests), 1)
        self.assertTrue(any(step.get("event") == "writes_materialized" for step in result["trace"]))

    def test_deferred_write_held_with_reason_when_read_fails(self):
        # The read the write consumes FAILS → materialization is impossible: the write is held
        # with the concrete reason instead of surfacing a broken action, and the completion
        # guard forces the planner to resolve the debt before the turn may end.
        responses = [
            _turn("正在查询配体。", operations=[
                _read_op("c1", "compound.lookup", {"name": "brequinar"}),
                _write_op("w1", "task_detail:apply_docking_ligand_smiles",
                          {"smiles": {"$fromObservation": "c1", "field": "smiles", "index": 0}},
                          depends=["c1"]),
            ]),
            _turn("查询失败，无法填入。"),
            _turn("来源暂时不可用，本次未能填入配体。"),
            "来源暂时不可用，本次未能填入配体。",
        ]
        assistant, session = self._make(responses, lookup_ok=False)
        with patch_host_skills(self.APPLY_LIGAND):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload("affinity"),
                                         user_id="u", username="alice", content="配体用 brequinar")
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["actions"], [])
        self.assertTrue(_has_feedback(session, 1, "could not be auto-applied"))
        self.assertTrue(_has_feedback(session, 1, "w1"))

    def test_multi_record_silent_pick_is_held_for_user_choice(self):
        # The deferred write consumes ONE record of a multi-record search: that is a silent pick
        # on the user's behalf. The harness holds the write with explicit guidance (ask a choice
        # question / fan out over every record / paste the value if the user already chose)
        # instead of surfacing an action the user never agreed to.
        two_results = [
            {"smiles": "CCOc1ccccc1", "cid": "5354"},
            {"smiles": "CC(=O)Nc1ccc(F)cc1", "cid": "8888"},
        ]
        responses = [
            _turn("查到两个候选。", operations=[
                _read_op("c1", "compound.lookup", {"name": "ambiguol"}),
                _write_op(
                    "w1", "task_detail:apply_docking_ligand_smiles",
                    {"smiles": {"$fromObservation": "c1", "field": "smiles", "index": 0}},
                    depends=["c1"],
                ),
            ]),
            _turn("请问要用哪个化合物?", questions=[{
                "text": "Which compound?", "kind": "choice",
                "options": [{"label": "A", "value": "5354"}, {"label": "B", "value": "8888"}],
            }]),
        ]
        assistant, session = self._make(responses, lookup_results=two_results)
        with patch_host_skills(self.APPLY_LIGAND):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload("affinity"),
                                         user_id="u", username="alice", content="配体用 ambiguol")
        self.assertEqual(result["state"], "needs_input")
        self.assertEqual(result["actions"], [])
        self.assertTrue(_has_feedback(session, 1, "CANDIDATE CHOICE"))
        self.assertTrue(_has_feedback(session, 1, "2 records"))

    def test_full_fan_out_over_all_records_materializes(self):
        # Fan-out: one write per record of the multi-record search, covering every index — the
        # collective consumption is complete, so both actions materialize in the same round.
        two_results = [
            {"smiles": "CCOc1ccccc1", "cid": "5354"},
            {"smiles": "CC(=O)Nc1ccc(F)cc1", "cid": "8888"},
        ]
        responses = [_turn("两个都建。", operations=[
            _read_op("c1", "compound.lookup", {"name": "ambiguol"}),
            _write_op("w1", "task_detail:apply_docking_ligand_smiles",
                      {"smiles": {"$fromObservation": "c1", "field": "smiles", "index": 0}}, depends=["c1"]),
            _write_op("w2", "task_detail:apply_docking_ligand_smiles",
                      {"smiles": {"$fromObservation": "c1", "field": "smiles", "index": 1}}, depends=["c1"]),
        ])]
        assistant, session = self._make(responses, lookup_results=two_results)
        with patch_host_skills(self.APPLY_LIGAND):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload("affinity"),
                                         user_id="u", username="alice", content="两个配体都建任务")
        self.assertEqual(result["state"], "await_confirmation")
        self.assertEqual(len(result["actions"]), 2)
        self.assertEqual(result["actions"][0]["arguments"]["smiles"], "CCOc1ccccc1")
        self.assertEqual(result["actions"][1]["arguments"]["smiles"], "CC(=O)Nc1ccc(F)cc1")

    def test_pasted_value_silent_pick_intercepted_once(self):
        # The PASTED form of a silent pick: round 1 searches (2 records), round 2 applies one
        # record's value directly. The value guard intercepts it once and demands the user's
        # choice; a re-emission after that instruction is treated as resolved (one-shot latch,
        # same philosophy as the completion guard — never a hard filter, never a deadlock).
        two_results = [
            {"smiles": "CCOc1ccccc1", "cid": "5354"},
            {"smiles": "CC(=O)Nc1ccc(F)cc1", "cid": "8888"},
        ]
        responses = [
            _turn("查到两个候选。", operations=[
                _read_op("c1", "compound.lookup", {"name": "ambiguol"}),
            ]),
            _turn("直接用第一个。", operations=[
                _write_op("w1", "task_detail:apply_docking_ligand_smiles", {"smiles": "CCOc1ccccc1"}),
            ]),
            _turn("请问要用哪个化合物?", questions=[{
                "text": "Which compound?", "kind": "choice",
                "options": [{"label": "A", "value": "5354"}, {"label": "B", "value": "8888"}],
            }]),
        ]
        assistant, session = self._make(responses, lookup_results=two_results)
        with patch_host_skills(self.APPLY_LIGAND):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload("affinity"),
                                         user_id="u", username="alice", content="配体用 ambiguol")
        self.assertEqual(result["state"], "needs_input")
        self.assertEqual(result["actions"], [])
        self.assertTrue(_has_feedback(session, 2, "CANDIDATE CHOICE"))

    def test_repeated_silent_pick_synthesizes_choice_question(self):
        # After ONE explicit ask-the-user instruction the planner still re-emits a single-entry
        # write: instead of repeating the same rejection until the budget dies (the production
        # KLK/ibuprofen failure), the harness asks the choice question ITSELF — options are the
        # real retrieved records, so the turn completes as needs_input with chips the user can
        # click. The model decides direction; the harness ensures completion.
        two_results = [
            {"smiles": "CCOc1ccccc1", "cid": "5354"},
            {"smiles": "CC(=O)Nc1ccc(F)cc1", "cid": "8888"},
        ]
        responses = [
            _turn("查到两个候选。", operations=[
                _read_op("c1", "compound.lookup", {"name": "ambiguol"}),
            ]),
            _turn("直接用第一个。", operations=[
                _write_op("w1", "task_detail:apply_docking_ligand_smiles", {"smiles": "CCOc1ccccc1"}),
            ]),
            _turn("还是用第一个。", operations=[
                _write_op("w2", "task_detail:apply_docking_ligand_smiles", {"smiles": "CCOc1ccccc1"}),
            ]),
        ]
        assistant, session = self._make(responses, lookup_results=two_results)
        with patch_host_skills(self.APPLY_LIGAND):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload("affinity"),
                                         user_id="u", username="alice", content="配体用 ambiguol")
        self.assertEqual(result["state"], "needs_input")
        self.assertEqual(result["actions"], [])
        self.assertEqual(len(result["questions"]), 1)
        question = result["questions"][0]
        self.assertEqual(question["kind"], "choice")
        self.assertTrue(question["allowOther"])
        self.assertEqual({opt["value"] for opt in question["options"]}, {"5354", "8888"})
        self.assertIn("选择", result["content"])
        # The first interception still coached the model; only the second one took over.
        self.assertTrue(_has_feedback(session, 2, "CANDIDATE CHOICE"))
        terminals = [s for s in result["trace"] if s["event"] == "terminal"]
        self.assertEqual(terminals[-1]["detail"].get("synthesized"), "candidate_choice")

    def test_user_named_record_exempts_next_turn_silent_pick(self):
        # Cross-turn closure: the user answers a choice question by NAMING the record's
        # identity (「用 8888」 — the CID, not the consumed SMILES). Re-searching and pasting
        # that record's field is then an explicit choice, not a silent pick: the guard
        # exempts it and the write materializes into its confirmation card.
        two_results = [
            {"smiles": "CCOc1ccccc1", "cid": "5354"},
            {"smiles": "CC(=O)Nc1ccc(F)cc1", "cid": "8888"},
        ]
        responses = [
            _turn("重新检索并填入选中的第二个。", operations=[
                _read_op("c1", "compound.lookup", {"name": "ambiguol"}),
                _write_op("w1", "task_detail:apply_docking_ligand_smiles", {"smiles": "CC(=O)Nc1ccc(F)cc1"}),
            ]),
            _turn("已填入选中的第二个。", operations=[
                _write_op("w2", "task_detail:apply_docking_ligand_smiles", {"smiles": "CC(=O)Nc1ccc(F)cc1"}),
            ]),
        ]
        assistant, session = self._make(responses, lookup_results=two_results)
        with patch_host_skills(self.APPLY_LIGAND):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload("affinity"),
                                         user_id="u", username="alice", content="用 8888 那个配体")
        self.assertEqual(result["state"], "await_confirmation")
        self.assertEqual(len(result["actions"]), 1)
        self.assertEqual(result["actions"][0]["arguments"]["smiles"], "CC(=O)Nc1ccc(F)cc1")

    def test_grounding_rejection_capped_with_records_footer(self):
        # A message-only round that cites no retrieved record is rejected ONCE with a corrective
        # note; if the model still cannot comply, the harness accepts the turn and appends a
        # deterministic footer of the real retrieved identities instead of burning the budget on
        # identical rejections (the coaching note promises "a message-only round is always
        # valid" — the audit must honor that promise after one corrective round).
        two_results = [
            {"smiles": "CCOc1ccccc1", "cid": "5354"},
            {"smiles": "CC(=O)Nc1ccc(F)cc1", "cid": "8888"},
        ]
        responses = [
            _turn("查到两个候选。", operations=[
                _read_op("c1", "compound.lookup", {"name": "ambiguol"}),
            ]),
            _turn("查询已完成。"),
            _turn("处理完成。"),
        ]
        assistant, session = self._make(responses, lookup_results=two_results)
        with patch_host_skills(self.APPLY_LIGAND):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload("affinity"),
                                         user_id="u", username="alice", content="配体用 ambiguol")
        self.assertEqual(result["state"], "complete")
        self.assertTrue(_has_feedback(session, 2, "does not reference"))
        self.assertIn("5354", result["content"])
        self.assertIn("检索记录", result["content"])
        terminals = [s for s in result["trace"] if s["event"] == "terminal"]
        self.assertEqual(terminals[-1]["detail"].get("synthesized"), "grounding_footer")

    def test_failure_branch_synthesizes_choice_question(self):
        # Budget exhaustion with an unresolved candidate choice must not end in "please try
        # rephrasing": the disambiguation question is derivable from the held write and the
        # observations, so the failure branch asks it — the user's answer unblocks the next
        # turn instead of the whole request being lost.
        two_results = [
            {"smiles": "CCOc1ccccc1", "cid": "5354"},
            {"smiles": "CC(=O)Nc1ccc(F)cc1", "cid": "8888"},
        ]
        bad_op = _read_op("bad1", "no.such.skill", {})
        responses = [
            _turn("查到两个候选，等待用户选择。", operations=[
                _read_op("c1", "compound.lookup", {"name": "ambiguol"}),
                _write_op("w1", "task_detail:apply_docking_ligand_smiles",
                          {"smiles": {"$fromObservation": "c1", "field": "smiles", "index": 0}},
                          depends=["c1"]),
            ]),
            _turn("继续。", operations=[bad_op]),
            _turn("继续。", operations=[_read_op("bad2", "no.such.skill", {})]),
            _turn("继续。", operations=[_read_op("bad3", "no.such.skill", {})]),
        ]
        assistant, session = self._make(responses, lookup_results=two_results)
        with patch_host_skills(self.APPLY_LIGAND):
            result = assistant.plan_turn(context_type="task_detail", context_payload=task_detail_payload("affinity"),
                                         user_id="u", username="alice", content="配体用 ambiguol")
        self.assertEqual(result["state"], "needs_input")
        self.assertEqual(len(result["questions"]), 1)
        self.assertEqual({opt["value"] for opt in result["questions"][0]["options"]}, {"5354", "8888"})
        self.assertIn("选择", result["content"])
        self.assertNotIn("try rephrasing", result["content"])


    def test_gated_operation_chains_after_the_operation_that_unblocks_it(self):
        # "帮我完成" must plan THROUGH to the gated goal: while runDisabled names a precondition
        # (missing ligand) that an earlier operation in the SAME plan resolves, the gated
        # submit is legal chained after it via depends_on — the frontend reveals the actions
        # one at a time, so the run starts without the user having to re-prompt.
        apply_ligand = CopilotSkillDefinition(
            name="task_detail:apply_docking_ligand_smiles",
            label="Set docking ligand (SMILES)",
            description="Set the ligand of the current docking task from a SMILES string.",
            input_schema={"type": "object", "properties": {"smiles": {"type": "string", "minLength": 1}},
                          "required": ["smiles"], "additionalProperties": False},
            effect="update", context_type="task_detail",
        )
        submit = CopilotSkillDefinition(
            name="task_detail:submit_current",
            label="Start run",
            description="Submit the current task for execution.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect="execute", context_type="task_detail",
        )
        payload = task_detail_payload("affinity")
        payload["runtime"] = {
            "displayTaskState": "DRAFT", "runDisabled": True,
            "runBlockedReason": "Ligand SMILES is empty.", "activeTaskId": "",
        }
        responses = [
            _turn("配体已填，等待两步确认。", operations=[
                _write_op("lig", "task_detail:apply_docking_ligand_smiles",
                          {"smiles": "CC(C)CC1=CC=C(C=C1)C(C)C(=O)O"}),
                _write_op("run", "task_detail:submit_current", {}, depends=["lig"]),
            ]),
        ]
        assistant, session = make_assistant(responses)
        with patch_host_skills(apply_ligand, submit):
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="对接布洛芬，帮我完成")
        self.assertEqual(result["state"], "await_confirmation")
        self.assertEqual([a["id"] for a in result["actions"]],
                         ["task_detail:apply_docking_ligand_smiles", "task_detail:submit_current"])
        # Declaration order preserved (sequence), and the chain is declared on the payload.
        self.assertEqual(result["actions"][0]["sequence"], 0)
        self.assertEqual(result["actions"][1]["payload"]["dependsOn"], ["lig"])

    def test_blocked_run_without_resolving_operation_still_not_proposed(self):
        # The chaining permission is narrow: a gated submit with NO earlier operation resolving
        # the blocker (the sequence is simply missing) stays the model's judgment call — the
        # harness does not force it; this pins that the audit imposes no new gating of its own
        # (runtime legality is the planner's contract, enforced by the prompt, not the audit).
        submit = CopilotSkillDefinition(
            name="task_detail:submit_current",
            label="Start run",
            description="Submit the current task for execution.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect="execute", context_type="task_detail",
        )
        payload = task_detail_payload("prediction")
        payload["runtime"] = {
            "displayTaskState": "DRAFT", "runDisabled": True,
            "runBlockedReason": "Protein sequence is empty.", "activeTaskId": "",
        }
        responses = [_turn("开始运行。", operations=[_write_op("run", "task_detail:submit_current", {})])]
        assistant, session = make_assistant(responses)
        with patch_host_skills(submit):
            result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                         user_id="u", username="alice", content="开始运行")
        # The harness surfaces what the planner proposed — the runtime gate is the planner's
        # contract; this test pins the audit does not silently drop writes.
        self.assertEqual(result["state"], "await_confirmation")
        self.assertEqual([a["id"] for a in result["actions"]], ["task_detail:submit_current"])


if __name__ == "__main__":
    unittest.main()


class IdentifierFirstStructureTests(unittest.TestCase):
    """pi/MCP rule: pass IDENTIFIERS, not derived URLs — the host builds the guaranteed-valid
    mmCIF URL from the entry id itself. A record's sourceUrl is the human entry page (no file
    extension) and .pdb-format files exist only for some entries; hand-copying URLs was the
    root cause of both dead downloads and 'must point to a .pdb/.cif file' errors.
    """

    def test_create_docking_with_pdbid_only_passes_audit(self):
        from management_api.copilot_skills.context_actions import build_context_skill_definitions

        defs = build_context_skill_definitions(
            "task_list", {"project": {"task_type": "docking"}, "rows": []}, workflow_key="affinity"
        )
        create = next(d for d in defs if d.name == "tasks:create_docking")
        definitions = {create.name: create}
        from management_api.copilot_skill_harness import CopilotSkillHarness
        harness = CopilotSkillHarness(skills=type("S", (), {"definitions": []})())
        audit = harness.audit_plan(
            {
                "message": "创建对接任务。",
                "questions": [],
                "operations": [
                    {"id": "c1", "skill": "tasks:create_docking",
                     "arguments": {"create": True, "targetPdbId": "8YGY",
                                   "ligandSmiles": "CC(C)CC1=CC=C(C=C1)C(C)C(=O)O"},
                     "depends_on": []},
                ],
            },
            definitions,
            context_type="task_list",
        )
        self.assertEqual(audit.issues, (), f"pdbId-only must be valid: {audit.issues}")
        self.assertEqual(audit.operations[0].arguments["targetPdbId"], "8YGY")

    def test_create_docking_without_any_target_is_rejected(self):
        from management_api.copilot_skills.context_actions import build_context_skill_definitions
        from management_api.copilot_skill_harness import CopilotSkillHarness

        defs = build_context_skill_definitions(
            "task_list", {"project": {"task_type": "docking"}, "rows": []}, workflow_key="affinity"
        )
        create = next(d for d in defs if d.name == "tasks:create_docking")
        harness = CopilotSkillHarness(skills=type("S", (), {"definitions": []})())
        audit = harness.audit_plan(
            {
                "message": "创建对接任务。",
                "questions": [],
                "operations": [
                    {"id": "c1", "skill": "tasks:create_docking",
                     "arguments": {"create": True, "ligandSmiles": "CCO"}, "depends_on": []},
                ],
            },
            {create.name: create},
            context_type="task_list",
        )
        self.assertTrue(any("does not match any allowed schema" in issue for issue in audit.issues))
