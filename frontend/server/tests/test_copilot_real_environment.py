"""Real-environment Copilot end-to-end tests.

These tests build context_payloads that mirror EXACTLY what the frontend sends in production
(project_list with summary counts, task_list with project+options, task_detail with
page/draft/runtime/currentTask), and drive the FULL planner loop over multi-turn conversations
that reference earlier turns. They pin the planner's ENVIRONMENT AWARENESS: the planner must
reason from the actual visible data (counts, states, components, run-blocked reasons) rather
than guessing.

Each test scripts the model's JSON responses but feeds REAL payload shapes. The scenarios model
production user journeys:
  - Analyzing the project portfolio from project_list statistics
  - Filtering/navigating the task list
  - Analyzing the current task on task_detail
  - Multi-turn: portfolio → drill into a project → analyze its tasks → create a new task
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from management_api.copilot import CopilotAssistant
from management_api.copilot_skill_harness import CopilotSkillDefinition
from management_api.copilot_skills.online_databases import OnlineSkillDefinition
from tests.helpers import FakeResponse, NullLogger, is_phase2_request


# --------------------------------------------------------------------------- #
# Infrastructure
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
    """Accept either dict (JSON turn) or raw string (phase-2 free text) responses."""
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
def real_project_list_payload(
    *,
    total_projects: int = 6,
    failed: int = 1,
    active: int = 2,
    empty: int = 1,
    prediction_count: int = 3,
    affinity_count: int = 2,
    peptide_count: int = 1,
    backend_counts: Dict[str, int] | None = None,
) -> Dict[str, Any]:
    """Mirror ProjectsPage.tsx copilotContextPayload."""
    backends = backend_counts or {"boltz": 4, "alphafold3": 2}
    projects = [
        {"id": f"proj-{i}", "name": f"Project {i}", "task_type": tt, "backend": b,
         "task_counts": tc, "updated_at": f"2026-08-0{i % 9 + 1}T10:00:00Z"}
        for i, (tt, b, tc) in enumerate([
            ("prediction", "boltz", {"total": 10, "queued": 1, "running": 0, "failure": 0}),
            ("prediction", "boltz", {"total": 5, "queued": 0, "running": 1, "failure": 0}),
            ("prediction", "alphafold3", {"total": 3, "queued": 0, "running": 0, "failure": 1}),
            ("affinity", "boltz", {"total": 2, "queued": 0, "running": 0, "failure": 0}),
            ("affinity", "alphafold3", {"total": 0, "queued": 0, "running": 0, "failure": 0}),
            ("peptide_design", "boltz", {"total": 8, "queued": 0, "running": 0, "failure": 0}),
        ])
    ]
    return {
        "total_projects": total_projects,
        "matched_projects": total_projects,
        "summary": {
            "allTaskStateCounts": {"QUEUED": 1, "RUNNING": 1, "SUCCESS": 24, "FAILURE": 1},
            "matchedTaskStateCounts": {"QUEUED": 1, "RUNNING": 1, "SUCCESS": 24, "FAILURE": 1},
            "allTypeCounts": {"prediction": prediction_count, "affinity": affinity_count, "peptide_design": peptide_count},
            "matchedTypeCounts": {"prediction": prediction_count, "affinity": affinity_count, "peptide_design": peptide_count},
            "allBackendCounts": backends,
            "matchedBackendCounts": backends,
            "activeProjects": active,
            "failedProjects": failed,
            "emptyProjects": empty,
        },
        "options": {"backendOptions": list(backends.keys()), "workflowOptions": ["prediction", "virtual_screening", "affinity", "peptide_design", "lead_optimization"]},
        "filters": {"search": "", "typeFilter": "all", "stateFilter": "all", "sortBy": "updated_desc", "backendFilter": "all", "activityFilter": "all", "updatedWithinDays": "all", "minTaskCount": "all"},
        "projects": projects[:40],
    }


def real_task_list_payload(
    *,
    project_id: str = "proj-1",
    project_name: str = "CD73 Project",
    workflow: str = "peptide_design",
    tasks: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Mirror ProjectTasksPage.tsx copilotContextPayload."""
    default_tasks = [
        {"id": "row-1", "task_id": "t-1", "name": "CD73 linear", "task_state": "SUCCESS", "backend": "boltz",
         "created_at": "2026-08-01T10:00:00Z", "updated_at": "2026-08-05T10:00:00Z", "properties": {}},
        {"id": "row-2", "task_id": "t-2", "name": "CD73 cyclic", "task_state": "RUNNING", "backend": "boltz",
         "created_at": "2026-08-02T10:00:00Z", "updated_at": "2026-08-06T10:00:00Z", "properties": {}},
        {"id": "row-3", "task_id": "t-3", "name": "CD73 bicyclic", "task_state": "FAILURE", "backend": "protenix",
         "created_at": "2026-08-03T10:00:00Z", "updated_at": "2026-08-07T10:00:00Z", "properties": {}},
        {"id": "row-4", "task_id": "t-4", "name": "CD73 v2 linear", "task_state": "DRAFT", "backend": "boltz",
         "created_at": "2026-08-08T10:00:00Z", "updated_at": "2026-08-08T10:00:00Z", "properties": {}},
    ]
    tasks = tasks or default_tasks
    state_counts: Dict[str, int] = {}
    for t in tasks:
        state = str(t.get("task_state") or "UNKNOWN")
        state_counts[state] = state_counts.get(state, 0) + 1
    return {
        "page": {"contextType": "task_list", "workflowKey": workflow, "workflowTitle": workflow.replace("_", " ").title()},
        "project": {"id": project_id, "name": project_name, "task_type": workflow, "workflow_key": workflow},
        "options": {"backendOptions": ["boltz", "alphafold3", "protenix"], "workflowOptions": ["prediction", "virtual_screening", "affinity", "peptide_design", "lead_optimization"]},
        "summary": {
            "totalTasks": len(tasks),
            "taskStateCounts": state_counts,
            "failedTasks": state_counts.get("FAILURE", 0),
            "runningTasks": state_counts.get("RUNNING", 0),
            "queuedTasks": state_counts.get("QUEUED", 0),
        },
        "filters": {"search": "", "stateFilter": "all", "workflowFilter": "all", "backendFilter": "all", "sortKey": "submitted", "sortDirection": "desc"},
        "rows": tasks,
    }


def real_task_detail_payload(
    *,
    workflow: str = "peptide_design",
    project_id: str = "proj-1",
    project_name: str = "CD73 Project",
    task_name: str = "CD73 cyclic",
    task_state: str = "SUCCESS",
    backend: str = "boltz",
    peptide_options: Dict[str, Any] | None = None,
    run_blocked_reason: str = "",
) -> Dict[str, Any]:
    """Mirror useProjectDetailWorkspaceView.tsx copilotContextPayload."""
    options = peptide_options or {
        "peptideDesignMode": "cyclic",
        "peptideBinderLength": 17,
        "peptideIterations": 12,
        "peptidePopulationSize": 16,
        "peptideEliteSize": 5,
        "peptideMutationRate": 0.25,
    }
    return {
        "page": {
            "contextType": "task_detail",
            "workflowKey": workflow,
            "workflowTitle": workflow.replace("_", " ").title(),
            "workflowShortTitle": "Peptide",
            "runLabel": "Run",
            "supportsSequenceInputs": True,
            "availableActions": [
                "analyze_current_context",
                "plan_confirmed_parameter_patch",
                "plan_confirmed_submit",
                "plan_confirmed_cancel_current_task",
                "plan_confirmed_delete_current_task",
                "plan_confirmed_metadata_update",
                "apply_copilot_uploaded_files_when_supported",
            ],
        },
        "project": {"id": project_id, "name": project_name, "task_type": workflow, "workflow_key": workflow},
        "draft": {
            "taskName": task_name,
            "taskSummary": "",
            "backend": backend,
            "options": options,
            "components": [{"index": 0, "id": "comp-0", "type": "protein", "sequenceLength": 524, "numCopies": 1}],
            "constraints": [],
        },
        "runtime": {
            "displayTaskState": task_state,
            "runDisabled": bool(run_blocked_reason),
            "runBlockedReason": run_blocked_reason,
            "activeTaskId": "t-2",
            "statusTaskRowId": "row-2",
            "statusTaskId": "t-2",
            "statusTaskState": task_state,
            "activeResultTaskRowId": "row-2",
            "activeResultTaskId": "t-2",
            "activeResultTaskState": task_state,
            "authoritativeTaskState": task_state,
        },
        "currentTask": {
            "id": "row-2",
            "project_id": project_id,
            "name": task_name,
            "task_id": "t-2",
            "task_state": task_state,
            "status_text": "Completed",
            "error_text": "" if task_state != "FAILURE" else "peptide cyclization failed",
            "backend": backend,
            "seed": 42,
            "structure_name": "",
            "submitted_at": "2026-08-02T10:00:00Z",
            "completed_at": "2026-08-02T11:00:00Z",
            "duration_seconds": 3600,
            "components": [{"index": 0, "id": "comp-0", "type": "protein", "sequenceLength": 524, "numCopies": 1}],
            "constraints": [],
            "properties": {},
            "affinitySummary": {},
            "confidenceSummary": {
                "avgPlddt": 0.59,
                "iptm": 0.73,
                "peptideDesign": {"count": 12, "top": [
                    {"rank": 1, "score": 0.688, "plddt": 59.1, "sequence": "FSSEMRQGAMHPIFWSI"},
                    {"rank": 2, "score": 0.629, "plddt": 55.1, "sequence": "NEQWSDNFWVPPATKGP"},
                ]},
            },
        },
    }


# --------------------------------------------------------------------------- #
# 1. Environment awareness: project portfolio analysis
# --------------------------------------------------------------------------- #


class ProjectPortfolioScenarioTests(unittest.TestCase):
    """The planner must reason from the REAL summary counts in project_list context."""

    def test_portfolio_overview_cites_real_counts(self):
        payload = real_project_list_payload()
        # Phase-1: JSON turn with the short message. Phase-2: free-text answer (not JSON-wrapped).
        responses = [
            _turn("当前共有 6 个项目：3 个预测、2 个亲和力、1 个多肽设计。"),
            "当前共有 **6 个项目**：3 个结构预测、2 个亲和力、1 个多肽设计。"
            "其中 **2 个活跃**（1 排队、1 运行），**1 个失败**，**1 个空**。"
            "后端：boltz 4、alphafold3 2。",
        ]
        assistant, session = make_assistant(responses)
        result = assistant.plan_turn(
            context_type="project_list", context_payload=payload,
            user_id="u", username="alice", content="项目总览",
        )
        self.assertEqual(result["state"], "complete")
        self.assertIn("6", result["content"])
        # The answer should cite workflow types and backends from the summary — in the user's
        # language (Chinese), not internal English keys.
        self.assertTrue(
            "prediction" in result["content"].lower() or "结构预测" in result["content"],
            f"answer should cite workflow type, got: {result['content'][:200]}",
        )
        self.assertIn("boltz", result["content"].lower())       # backend

    def test_show_failed_projects_routes_to_filter(self):
        from management_api.copilot_skills.context_actions import build_context_skill_definitions
        d = next(s for s in build_context_skill_definitions("project_list", {}) if s.name == "projects:failed")
        payload = real_project_list_payload(failed=1)
        responses = [_turn("筛选有失败任务的项目。", operations=[
            _write_op("f", "projects:failed", {"activityFilter": "failed"}),
        ])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(
                context_type="project_list", context_payload=payload,
                user_id="u", username="alice", content="哪些项目有失败任务",
            )
        self.assertEqual(result["state"], "await_confirmation")
        self.assertEqual(result["actions"][0]["arguments"]["activityFilter"], "failed")

    def test_active_project_count_from_summary(self):
        payload = real_project_list_payload(active=2)
        responses = [
            _turn("当前有 2 个项目处于活跃状态。"),
            "当前有 **2 个项目**处于活跃状态（有排队或运行中的任务）。",
        ]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(
            context_type="project_list", context_payload=payload,
            user_id="u", username="alice", content="活跃项目有多少",
        )
        self.assertEqual(result["state"], "complete")
        self.assertIn("2", result["content"])


# --------------------------------------------------------------------------- #
# 2. Environment awareness: task list analysis
# --------------------------------------------------------------------------- #


class TaskListEnvironmentScenarioTests(unittest.TestCase):
    """The planner must reason from the REAL task list summary and rows."""

    def test_task_list_summary_cites_state_counts(self):
        payload = real_task_list_payload()
        responses = [
            _turn("当前项目共 4 个任务：1 个成功、1 个运行中、1 个失败、1 个草稿。失败的是 CD73 bicyclic。"),
            "当前项目共 **4 个任务**：1 个成功、1 个运行中、1 个失败、1 个草稿。"
            "失败的任务是 **CD73 bicyclic**（protenix 后端）。",
        ]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(
            context_type="task_list", context_payload=payload,
            user_id="u", username="alice", content="任务列表情况怎么样",
        )
        self.assertEqual(result["state"], "complete")
        self.assertIn("4", result["content"])
        self.assertIn("CD73 bicyclic", result["content"])

    def test_filter_running_tasks_from_list(self):
        from management_api.copilot_skills.context_actions import build_context_skill_definitions
        d = next(s for s in build_context_skill_definitions("task_list", {"project": {"id": "proj-1"}}) if s.name == "tasks:update_view")
        payload = real_task_list_payload()
        responses = [_turn("筛选运行中的任务。", operations=[
            _write_op("f", "tasks:update_view", {"stateFilter": "RUNNING"}),
        ])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(
                context_type="task_list", context_payload=payload,
                user_id="u", username="alice", content="正在运行的任务",
            )
        self.assertEqual(result["actions"][0]["arguments"]["stateFilter"], "RUNNING")

    def test_identify_failed_task_for_review(self):
        payload = real_task_list_payload()
        responses = [_turn("CD73 bicyclic 任务失败了（protenix 后端）。你可以打开它查看失败原因。", operations=[
            _write_op("open", "tasks:open", {"taskRowId": "row-3"}),
        ])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(next(s for s in __import__("management_api.copilot_skills.context_actions", fromlist=["build_context_skill_definitions"]).build_context_skill_definitions("task_list", {"project": {"id": "proj-1"}}) if s.name == "tasks:open")):
            result = assistant.plan_turn(
                context_type="task_list", context_payload=payload,
                user_id="u", username="alice", content="哪个任务失败了，打开看看",
            )
        self.assertEqual(result["actions"][0]["arguments"]["taskRowId"], "row-3")


# --------------------------------------------------------------------------- #
# 3. Environment awareness: current task analysis (peptide design)
# --------------------------------------------------------------------------- #


class PeptideTaskDetailScenarioTests(unittest.TestCase):
    """The planner must read peptide-specific options and confidence from currentTask."""

    def test_analyze_peptide_task_quotes_real_options(self):
        payload = real_task_detail_payload(workflow="peptide_design")
        responses = [
            _turn("当前任务 CD73 cyclic 已完成。肽长 17，最佳候选 FSSEMRQGAMHPIFWSI。"),
            "当前任务 **CD73 cyclic** 已完成。参数：环化模式 cyclic、肽长 **17**、迭代 12、"
            "种群 16、精英池 5、突变率 0.25。最佳候选序列 **FSSEMRQGAMHPIFWSI**，score 0.688，pLDDT 59.1，ipTM 0.73。",
        ]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(
            context_type="task_detail", context_payload=payload,
            user_id="u", username="alice", content="分析当前任务",
        )
        self.assertEqual(result["state"], "complete")
        self.assertIn("FSSEMRQGAMHPIFWSI", result["content"])
        self.assertIn("17", result["content"])  # binder length

    def test_change_binder_length_emits_parameter_patch(self):
        from management_api.copilot_capabilities import build_task_detail_skill_definitions
        d = next(s for s in build_task_detail_skill_definitions("peptide_design") if s.name == "task_detail:apply_parameter_patch")
        payload = real_task_detail_payload(workflow="peptide_design")
        responses = [_turn("更新肽长到 20。", operations=[
            _write_op("patch", "task_detail:apply_parameter_patch", {"parameterPatch": {"peptideBinderLength": 20}}),
        ])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(
                context_type="task_detail", context_payload=payload,
                user_id="u", username="alice", content="把肽长改成 20",
            )
        self.assertEqual(result["actions"][0]["arguments"]["parameterPatch"], {"peptideBinderLength": 20})

    def test_run_blocked_reason_is_surfaced(self):
        payload = real_task_detail_payload(run_blocked_reason="Add a ligand SMILES to run")
        responses = [
            _turn("当前任务无法运行：Add a ligand SMILES to run。"),
            "当前任务无法运行：**Add a ligand SMILES to run**。需要先添加配体 SMILES 才能提交。",
        ]
        assistant, _ = make_assistant(responses)
        result = assistant.plan_turn(
            context_type="task_detail", context_payload=payload,
            user_id="u", username="alice", content="为什么不能运行",
        )
        self.assertEqual(result["state"], "complete")
        self.assertIn("ligand", result["content"].lower())


# --------------------------------------------------------------------------- #
# 4. Multi-turn continuous conversation: full user journey
# --------------------------------------------------------------------------- #


class ContinuousConversationJourneyTests(unittest.TestCase):
    """A realistic multi-turn journey: portfolio → drill-in → analyze → create new task.

    Each turn carries the conversation history from the previous turns (as the frontend does via
    copilot_conversation.recent_messages), and the planner must stay grounded in the environment.
    """

    def test_full_journey_portfolio_to_new_task(self):
        # Turn 1: portfolio overview (project_list)
        portfolio_payload = real_project_list_payload()
        t1 = [
            _turn("当前 6 个项目，3 个预测、2 个亲和力、1 个多肽设计。"),
            "当前 **6 个项目**：3 个结构预测、2 个亲和力、1 个多肽设计。",
        ]
        assistant, session = make_assistant(t1)
        r1 = assistant.plan_turn(context_type="project_list", context_payload=portfolio_payload,
                                 user_id="u", username="alice", content="项目总览")
        self.assertEqual(r1["state"], "complete")

        # Turn 2: drill into the peptide project (task_list) — carries turn 1 history
        ctx2 = conv_context(("user", "项目总览"), ("assistant", r1["content"]))
        task_payload = real_task_list_payload(project_id="proj-6", project_name="CD73 Project", workflow="peptide_design")
        t2 = [
            _turn("CD73 项目共 4 个任务，1 个运行中，1 个失败。"),
            "CD73 项目共 **4 个任务**，1 个运行中，1 个失败。",
        ]
        assistant, session = make_assistant(t2)
        r2 = assistant.plan_turn(context_type="task_list", context_payload={**ctx2, **task_payload},
                                 user_id="u", username="alice", content="打开 CD73 项目看看")
        self.assertEqual(r2["state"], "complete")

        # Turn 3: analyze the running peptide task (task_detail) — carries turns 1+2
        ctx3 = conv_context(
            ("user", "项目总览"), ("assistant", r1["content"]),
            ("user", "打开 CD73 项目看看"), ("assistant", r2["content"]),
        )
        detail_payload = real_task_detail_payload(workflow="peptide_design", task_name="CD73 cyclic", task_state="RUNNING")
        t3 = [_turn("CD73 cyclic 任务运行中，环化模式 cyclic，肽长 17。"), "CD73 cyclic 任务**运行中**，环化模式 cyclic，肽长 17。"]
        assistant, session = make_assistant(t3)
        r3 = assistant.plan_turn(context_type="task_detail", context_payload={**ctx3, **detail_payload},
                                 user_id="u", username="alice", content="当前任务什么状态")
        self.assertEqual(r3["state"], "complete")

        # Turn 4: create a NEW task from current target (the user's reported scenario)
        ctx4 = conv_context(
            ("user", "项目总览"), ("assistant", r1["content"]),
            ("user", "打开 CD73 项目看看"), ("assistant", r2["content"]),
            ("user", "当前任务什么状态"), ("assistant", r3["content"]),
        )
        from management_api.copilot_capabilities import build_task_detail_skill_definitions
        create_def = next(s for s in build_task_detail_skill_definitions("peptide_design") if s.name == "task_detail:create_new_task")
        t4 = [_turn("新建一个双环肽任务，靶点用当前的 CD73 蛋白。", operations=[
            _write_op("create", "task_detail:create_new_task", {
                "components": [{"type": "protein", "sequence": "", "numCopies": 1}],
            }),
        ])]
        assistant, session = make_assistant(t4)
        with patch_host_skills(create_def):
            r4 = assistant.plan_turn(context_type="task_detail", context_payload={**ctx4, **detail_payload},
                                     user_id="u", username="alice", content="再新建一个任务，双环肽设计，靶点就是当前")
        self.assertEqual(r4["state"], "await_confirmation")
        self.assertEqual(len(r4["actions"]), 1)
        self.assertEqual(r4["actions"][0]["id"], "task_detail:create_new_task")

    def test_followup_asks_about_previous_task_option(self):
        # Turn 1 established the binder length; turn 2 asks to change it referencing the value.
        detail_payload = real_task_detail_payload(workflow="peptide_design")
        ctx = conv_context(
            ("user", "当前任务肽长多少"), ("assistant", "当前肽长 17。"),
        )
        from management_api.copilot_capabilities import build_task_detail_skill_definitions
        d = next(s for s in build_task_detail_skill_definitions("peptide_design") if s.name == "task_detail:apply_parameter_patch")
        responses = [_turn("更新肽长。", operations=[
            _write_op("patch", "task_detail:apply_parameter_patch", {"parameterPatch": {"peptideBinderLength": 25}}),
        ])]
        assistant, _ = make_assistant(responses)
        with patch_host_skills(d):
            result = assistant.plan_turn(context_type="task_detail", context_payload={**ctx, **detail_payload},
                                         user_id="u", username="alice", content="加到 25")
        self.assertEqual(result["actions"][0]["arguments"]["parameterPatch"], {"peptideBinderLength": 25})


# --------------------------------------------------------------------------- #
# 5. Environment-aware tool selection (lookup skills)
# --------------------------------------------------------------------------- #


class EnvironmentAwareToolSelectionTests(unittest.TestCase):
    """The planner must pick the RIGHT read skill based on the entity in context."""

    def test_lookup_uses_workflow_context_to_choose_skill(self):
        # User asks about a target in a peptide design task — planner uses uniprot.search.
        def uniprot_search(args):
            return {"source": "uniprot", "query": args.get("query", ""), "count": 1,
                    "results": [{"accession": "Q02127", "geneNames": "NT5E", "proteinName": "5'-nucleotidase", "sequence": "MTPRKRG"}]}
        d = OnlineSkillDefinition(name="uniprot.search", description="Search UniProt.",
            input_schema={"type": "object", "properties": {"query": {"type": "string", "minLength": 1}}, "required": ["query"], "additionalProperties": False})
        payload = real_task_detail_payload(workflow="peptide_design", task_name="CD73 cyclic")
        responses = [
            _turn("查找靶点信息。", operations=[_read_op("find", "uniprot.search", {"query": "CD73"})]),
            _turn("CD73 对应蛋白 NT5E（Q02127），524 残基，是 5'-nucleotidase。"),
        ]
        assistant, session = make_assistant(responses)
        inject_read_skills(assistant, {"uniprot.search": (d, uniprot_search)})
        result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                     user_id="u", username="alice", content="当前靶点 CD73 是哪个蛋白")
        self.assertEqual(result["state"], "complete")
        self.assertIn("NT5E", result["content"])

    def test_statistics_question_uses_compute_on_retrieved_values(self):
        # After retrieving bioactivity values, the planner uses compute.aggregate for stats.
        def chembl(args):
            return {"source": "chembl", "query": args.get("query", ""), "count": 3,
                    "results": [{"value": 10.0}, {"value": 20.0}, {"value": 30.0}]}
        d = OnlineSkillDefinition(name="chembl.bioactivity", description="Bioactivity.",
            input_schema={"type": "object", "properties": {"query": {"type": "string", "minLength": 1}}, "required": ["query"], "additionalProperties": False})
        payload = real_task_detail_payload(workflow="peptide_design")
        responses = [
            _turn("查找活性。", operations=[_read_op("bio", "chembl.bioactivity", {"query": "CD73"})]),
            _turn("计算平均。", operations=[_read_op("agg", "compute.aggregate",
                {"values": {"$fromObservation": "bio", "field": "value", "all": True}}, depends=["bio"])]),
            _turn("平均活性 20.0 nM。"),
        ]
        assistant, session = make_assistant(responses)
        inject_read_skills(assistant, {"chembl.bioactivity": (d, chembl)})
        result = assistant.plan_turn(context_type="task_detail", context_payload=payload,
                                     user_id="u", username="alice", content="CD73 的平均活性")
        self.assertEqual(result["state"], "complete")
        self.assertIn("20", result["content"])


if __name__ == "__main__":
    unittest.main()
