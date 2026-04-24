"""Legacy copilot tests — retained for backward compatibility."""

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.helpers import FakeResponse, FakeSession, make_assistant
from management_api.copilot_capabilities import (
    build_context_actions,
    build_task_submission_actions,
    infer_workflow_key,
    render_context_plan_schema_prompt,
    sanitize_task_parameter_patch,
)
from management_api.copilot import CopilotAssistant, strip_internal_capability_lines


class TaskChatTests(unittest.TestCase):
    def test_answer_context_calls_model_with_copilot_prompt(self):
        assistant, session = make_assistant("**分析结论**\n任务可以继续处理。")
        content = assistant.answer_context(
            context_type="task_detail",
            context_payload={"project": {"task_type": "Structure Prediction"}, "task": {"task_state": "SUCCESS"}},
            user_id="user-1",
            username="alice",
            content="解释这个结果是否可靠",
        )
        self.assertIn("分析结论", content)
        self.assertEqual(session.last_request["headers"]["Authorization"], "Bearer test-key")
        sent_messages = session.last_request["json"]["messages"]
        self.assertTrue(any("V-Bio Copilot" in item["content"] for item in sent_messages))
        self.assertTrue(any("alice:" in item["content"] for item in sent_messages))

    def test_strip_internal_capability_lines_hides_tool_names(self):
        content = strip_internal_capability_lines("分析结论\n能力使用：task_result_analysis\n可以继续检查。")
        self.assertNotIn("能力使用", content)
        self.assertNotIn("task_result_analysis", content)
        self.assertIn("可以继续检查", content)

    def test_sanitize_task_parameter_patch_allows_only_prediction_schema_keys(self):
        patch = sanitize_task_parameter_patch(
            {
                "seed": "999",
                "affinityMode": "pose",
                "peptideBinderLength": 15.9,
                "peptideMutationRate": "0.25",
                "deleteProject": True,
                "peptideIterations": -1,
            },
            "prediction",
        )
        self.assertEqual(patch, {"seed": 999})

    def test_workflow_specific_parameter_patch(self):
        affinity_patch = sanitize_task_parameter_patch(
            {"seed": 7, "affinityMode": "pose", "peptideBinderLength": 15},
            "affinity",
        )
        self.assertEqual(affinity_patch, {"seed": 7, "affinityMode": "pose"})

        peptide_patch = sanitize_task_parameter_patch(
            {"seed": 9, "affinityMode": "pose", "peptideBinderLength": 15, "peptideIterations": 20},
            "peptide_design",
        )
        self.assertEqual(peptide_patch, {"seed": 9, "peptideBinderLength": 15, "peptideIterations": 20})

    def test_infer_workflow_key_from_project_context(self):
        self.assertEqual(infer_workflow_key({"project": {"task_type": "Affinity Scoring"}}), "affinity")
        self.assertEqual(infer_workflow_key({"project": {"task_type": "Peptide Design"}}), "peptide_design")
        self.assertEqual(infer_workflow_key({"project": {"task_type": "Lead Optimization"}}), "lead_optimization")

    def test_build_task_submission_actions_requires_confirmation(self):
        actions = build_task_submission_actions(
            {
                "parameter_patch": {"seed": 999},
                "needs_confirmation": True,
                "execute_now": False,
                "intent": "update_and_submit_task",
            },
            "把 seed 改成 999 并提交",
            "prediction",
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["id"], "task_detail:apply_patch_and_submit")
        self.assertEqual(actions[0]["payload"]["parameterPatch"], {"seed": 999})

    def test_build_task_submission_actions_supports_component_replacement_schema(self):
        actions = build_task_submission_actions(
            {
                "parameter_patch": {
                    "componentsReplacement": {
                        "mode": "replace",
                        "components": [
                            {"type": "peptide", "sequence": "GGSGGS", "numCopies": 1, "useMsa": False}
                        ],
                        "clearConstraints": True,
                    }
                },
                "needs_confirmation": True,
                "execute_now": False,
                "intent": "replace_components_and_submit",
            },
            "帮我提交一个新的任务，只有一段多肽GGSGGS",
            "prediction",
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["id"], "task_detail:apply_patch_and_submit")
        patch = actions[0]["payload"]["parameterPatch"]
        self.assertEqual(patch["componentsReplacement"]["components"][0]["type"], "protein")
        self.assertEqual(patch["componentsReplacement"]["components"][0]["sequence"], "GGSGGS")

    def test_lead_optimization_has_no_generic_parameter_patch_actions(self):
        actions = build_task_submission_actions(
            {
                "parameter_patch": {"seed": 999, "affinityMode": "pose", "peptideIterations": 20},
                "needs_confirmation": True,
                "execute_now": False,
                "intent": "update_and_submit_task",
            },
            "把 seed 改成 999 并提交",
            "lead_optimization",
        )
        self.assertEqual(actions, [])

    def test_task_detail_metadata_patch_requires_confirmation(self):
        actions = build_task_submission_actions(
            {
                "capability": "task_metadata_patch",
                "intent": "rename",
                "metadata_patch": {"taskName": "round 2", "taskSummary": "updated description"},
                "needs_confirmation": True,
                "execute_now": False,
            },
            "重命名任务并更新描述",
            "prediction",
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["id"], "task_detail:apply_metadata_patch")
        self.assertTrue(actions[0]["needs_confirmation"])
        self.assertFalse(actions[0]["execute_now"])
        self.assertEqual(actions[0]["payload"]["schemaVersion"], "vbio-copilot-action-v2")
        self.assertEqual(actions[0]["payload"]["metadataPatch"]["taskName"], "round 2")

    def test_context_schema_includes_mutating_and_navigation_actions(self):
        project_schema = render_context_plan_schema_prompt("project_list", {})
        task_schema = render_context_plan_schema_prompt("task_list", {})
        self.assertIn("projects:open", project_schema)
        self.assertIn("projects:cancel_active", project_schema)
        self.assertIn("tasks:open", task_schema)
        self.assertIn("tasks:rename", task_schema)
        self.assertIn("tasks:cancel", task_schema)

    def test_task_list_wrong_workflow_and_invalid_ids_are_rejected(self):
        wrong_workflow_actions = build_context_actions(
            "task_list",
            {"actions": [{"id": "tasks:create_with_sequence", "payload": {"protein_sequence": "AAADDD"}}]},
            {"project": {"task_type": "Affinity Scoring"}},
        )
        missing_row_actions = build_context_actions(
            "task_list",
            {"actions": [{"id": "tasks:delete", "payload": {"taskRowId": "missing"}}]},
            {"project": {"task_type": "prediction"}, "rows": [{"id": "row-1", "name": "known"}]},
        )
        self.assertEqual(wrong_workflow_actions, [])
        self.assertEqual(missing_row_actions, [])

    def test_task_list_create_with_sequence_preserves_ligand_component(self):
        actions = build_context_actions(
            "task_list",
            {
                "actions": [
                    {
                        "id": "tasks:create_with_sequence",
                        "payload": {
                            "create": True,
                            "components": [
                                {"type": "protein", "sequence": "AAACCCDDDEE", "numCopies": 1, "useMsa": True},
                                {"type": "ligand", "sequence": "c1ccccc1", "numCopies": 1, "inputMethod": "smiles"},
                            ],
                        },
                    }
                ]
            },
            {"project": {"task_type": "Structure Prediction"}, "rows": []},
            "帮我新建一个任务，包含一个蛋白AAACCCDDDEE，一个小分子c1ccccc1",
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["id"], "tasks:create_with_sequence")
        self.assertEqual(
            actions[0]["payload"]["components"],
            [
                {"type": "protein", "sequence": "AAACCCDDDEE", "numCopies": 1, "useMsa": True},
                {"type": "ligand", "sequence": "c1ccccc1", "numCopies": 1, "inputMethod": "smiles"},
            ],
        )

    def test_task_list_rename_and_cancel_are_schema_confirmed(self):
        actions = build_context_actions(
            "task_list",
            {
                "actions": [
                    {"id": "tasks:rename", "payload": {"taskRowId": "row-1", "taskName": "new name"}},
                    {"id": "tasks:cancel", "payload": {"taskRowId": "row-1"}},
                ]
            },
            {"project": {"task_type": "prediction"}, "rows": [{"id": "row-1", "name": "old"}]},
        )
        self.assertEqual([action["id"] for action in actions], ["tasks:rename", "tasks:cancel"])
        self.assertTrue(all(action["needs_confirmation"] for action in actions))
        self.assertFalse(any(action["execute_now"] for action in actions))
        self.assertFalse(actions[0]["payload"]["destructive"])
        self.assertTrue(actions[1]["payload"]["destructive"])

    def test_project_list_open_cancel_and_unknown_action_guard(self):
        actions = build_context_actions(
            "project_list",
            {
                "actions": [
                    {"id": "projects:open", "payload": {"projectId": "p1"}},
                    {"id": "projects:cancel_active", "payload": {"projectId": "p1"}},
                    {"id": "projects:submit_task", "payload": {"projectId": "p1"}},
                ]
            },
            {"projects": [{"id": "p1", "name": "known"}]},
        )
        self.assertEqual([action["id"] for action in actions], ["projects:open", "projects:cancel_active"])
        self.assertFalse(actions[0]["payload"]["destructive"])
        self.assertTrue(actions[1]["payload"]["destructive"])

    def test_project_list_affinity_intent_rejects_boltz_backend_action(self):
        actions = build_context_actions(
            "project_list",
            {"actions": [{"id": "projects:backend_boltz", "payload": {"backendFilter": "boltz"}}]},
            {"projects": []},
            "这里面affinity相关的项目有哪些",
        )
        self.assertEqual(actions, [])

    def test_project_list_affinity_workflow_filter(self):
        actions = build_context_actions(
            "project_list",
            {"actions": [{"id": "projects:workflow_affinity", "payload": {"workflowFilter": "affinity"}}]},
            {"projects": []},
            "这里面affinity相关的项目有哪些",
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["id"], "projects:workflow_affinity")
        self.assertEqual(actions[0]["payload"]["workflowFilter"], "affinity")


if __name__ == "__main__":
    unittest.main()
