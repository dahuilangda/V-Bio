import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from management_api.copilot_capabilities import (
    build_task_submission_actions,
    infer_workflow_key,
    sanitize_task_parameter_patch,
)
from management_api.copilot import CopilotAssistant, strip_internal_capability_lines


class FakeResponse:
    ok = True
    status_code = 200
    text = ""

    def json(self):
        return {"choices": [{"message": {"content": "**分析结论**\n任务可以继续处理。"}}]}


class FakeSession:
    def __init__(self):
        self.last_request = None

    def post(self, url, headers, json, timeout):
        self.last_request = {
            "url": url,
            "headers": headers,
            "json": json,
            "timeout": timeout,
        }
        return FakeResponse()


class TaskChatTests(unittest.TestCase):
    def test_answer_context_calls_model_with_copilot_prompt(self):
        session = FakeSession()
        assistant = CopilotAssistant(
            chat_api_url="http://example.test/v1/chat/completions",
            chat_api_key="secret",
            chat_model="gemma4-31b",
            timeout_seconds=3,
            session=session,
            logger=None,
        )

        content = assistant.answer_context(
            context_type="task_detail",
            context_payload={"project": {"task_type": "Structure Prediction"}, "task": {"task_state": "SUCCESS"}},
            user_id="user-1",
            username="alice",
            content="解释这个结果是否可靠",
        )

        self.assertIn("分析结论", content)
        self.assertEqual(session.last_request["headers"]["Authorization"], "Bearer secret")
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
        self.assertEqual(actions[0]["id"], "task_detail:apply_parameter_patch")
        self.assertEqual(actions[1]["id"], "task_detail:apply_patch_and_submit")
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
        self.assertEqual(actions[0]["id"], "task_detail:apply_parameter_patch")
        patch = actions[0]["payload"]["parameterPatch"]
        self.assertEqual(patch["componentsReplacement"]["components"][0]["type"], "protein")
        self.assertEqual(patch["componentsReplacement"]["components"][0]["sequence"], "GGSGGS")
        self.assertEqual(actions[1]["id"], "task_detail:apply_patch_and_submit")

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


if __name__ == "__main__":
    unittest.main()
