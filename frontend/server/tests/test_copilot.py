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
from management_api.copilot import (
    CopilotAssistant,
    normalize_chat_messages_for_template,
    sanitize_context_payload,
    strip_internal_capability_lines,
)


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
        self.assertEqual([item["role"] for item in sent_messages].count("system"), 1)
        self.assertEqual(sent_messages[0]["role"], "system")

    def test_normalize_chat_messages_merges_system_messages_for_strict_templates(self):
        messages = normalize_chat_messages_for_template(
            [
                {"role": "system", "content": "base"},
                {"role": "system", "content": "context"},
                {"role": "user", "content": "hello"},
                {"role": "system", "content": "repair"},
            ]
        )
        self.assertEqual([item["role"] for item in messages], ["system", "user"])
        self.assertIn("base", messages[0]["content"])
        self.assertIn("context", messages[0]["content"])
        self.assertIn("repair", messages[0]["content"])

    def test_call_model_retries_empty_message_once(self):
        class EmptyThenContentSession:
            def __init__(self):
                self.responses = ["", "正常回复"]
                self.requests = []

            def post(self, url, headers, json, timeout):
                self.requests.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
                return FakeResponse(self.responses.pop(0))

        session = EmptyThenContentSession()
        assistant = CopilotAssistant(
            chat_api_url="http://example.test/v1/chat/completions",
            chat_api_key="test-key",
            chat_model="gemma4-31b",
            timeout_seconds=3,
            session=session,
            logger=type("Logger", (), {"warning": lambda *args, **kwargs: None})(),
        )
        content = assistant.answer_context(
            context_type="task_detail",
            context_payload={"project": {"task_type": "Structure Prediction"}},
            user_id="user-1",
            username="alice",
            content="解释这个结果",
        )
        self.assertEqual(content, "正常回复")
        self.assertEqual(len(session.requests), 2)
        self.assertIn("previous response was empty", session.requests[1]["json"]["messages"][0]["content"])
        self.assertNotIn("chat_template_kwargs", session.requests[0]["json"])
        self.assertNotIn("chat_template_kwargs", session.requests[1]["json"])

    def test_json_planning_uses_thinking_off_only_after_reasoning_empty(self):
        class JsonModeReasoningEmptySession:
            def __init__(self):
                self.requests = []
                self.calls = 0

            def post(self, url, headers, json, timeout):
                self.requests.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
                self.calls += 1
                if self.calls == 1:
                    return type(
                        "ReasoningResponse",
                        (),
                        {
                            "ok": True,
                            "status_code": 200,
                            "text": "",
                            "json": lambda self: {
                                "choices": [
                                    {
                                        "message": {"content": "", "reasoning": "thinking but no final"},
                                        "finish_reason": "length",
                                    }
                                ]
                            },
                        },
                    )()
                return FakeResponse(
                    '{"parameter_patch":{"componentsReplacement":{"mode":"replace","components":['
                    '{"type":"protein","sequence":"AAAEEEDD","numCopies":1,"useMsa":true},'
                    '{"type":"ligand","sequence":"c1ccccc1","numCopies":1,"inputMethod":"smiles"}'
                    '],"clearConstraints":true}},"needs_confirmation":true,"execute_now":false}'
                )

        session = JsonModeReasoningEmptySession()
        assistant = CopilotAssistant(
            chat_api_url="http://example.test/v1/chat/completions",
            chat_api_key="test-key",
            chat_model="gemma4-31b",
            timeout_seconds=3,
            session=session,
            logger=type("Logger", (), {"warning": lambda *args, **kwargs: None})(),
        )
        actions = assistant.plan_actions(
            context_type="task_detail",
            context_payload={"project": {"task_type": "Structure Prediction"}},
            user_id="user-1",
            username="alice",
            content="请帮我新建一个任务 蛋白为AAAEEEDD 小分子是c1ccccc1",
        )
        self.assertEqual(actions[0]["id"], "task_detail:apply_patch_and_submit")
        self.assertEqual(session.requests[0]["json"].get("response_format"), {"type": "json_object"})
        self.assertEqual(session.requests[1]["json"]["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(session.requests[1]["json"].get("response_format"), {"type": "json_object"})

    def test_call_model_ignores_reasoning_content_without_final_answer(self):
        class ReasoningOnlySession:
            def __init__(self):
                self.requests = []

            def post(self, url, headers, json, timeout):
                self.requests.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
                return type(
                    "ReasoningResponse",
                    (),
                    {
                        "ok": True,
                        "status_code": 200,
                        "text": "",
                        "json": lambda self: {
                            "choices": [
                                {
                                    "message": {
                                        "content": "",
                                        "reasoning_content": '{"parameter_patch":{"seed":42}}',
                                    },
                                    "finish_reason": "stop",
                                }
                            ]
                        },
                    },
                )()

        session = ReasoningOnlySession()
        assistant = CopilotAssistant(
            chat_api_url="http://example.test/v1/chat/completions",
            chat_api_key="test-key",
            chat_model="gemma4-31b",
            timeout_seconds=3,
            session=session,
            logger=type("Logger", (), {"warning": lambda *args, **kwargs: None})(),
        )
        with self.assertRaisesRegex(RuntimeError, "empty message"):
            assistant.answer_context(
                context_type="task_detail",
                context_payload={"project": {"task_type": "Structure Prediction"}},
                user_id="user-1",
                username="alice",
                content="解释这个结果",
            )
        self.assertEqual(len(session.requests), 3)
        self.assertNotIn("chat_template_kwargs", session.requests[0]["json"])
        self.assertEqual(session.requests[1]["json"]["chat_template_kwargs"], {"enable_thinking": False})

    def test_strip_internal_capability_lines_hides_tool_names(self):
        content = strip_internal_capability_lines("分析结论\n能力使用：task_result_analysis\n可以继续检查。")
        self.assertNotIn("能力使用", content)
        self.assertNotIn("task_result_analysis", content)
        self.assertIn("可以继续检查", content)

    def test_sanitize_context_payload_omits_file_bodies(self):
        large_file_body = "ATOM " * 2000
        payload = {
            "draft": {
                "templateUploads": [
                    {
                        "fileName": "target.cif",
                        "format": "cif",
                        "content": large_file_body,
                        "templateChainId": "A",
                        "targetChainIds": ["A"],
                    }
                ],
                "components": [
                    {"type": "protein", "sequence": "AAACCCDDDEE"},
                ],
            }
        }
        sanitized = sanitize_context_payload(payload)
        upload = sanitized["draft"]["templateUploads"][0]
        self.assertEqual(upload["fileName"], "target.cif")
        self.assertEqual(upload["format"], "cif")
        self.assertIn("omitted file/text payload", upload["content"])
        self.assertNotIn("ATOM ATOM ATOM", upload["content"])

    def test_task_detail_labeled_components_schema_creates_submit_button(self):
        assistant, session = make_assistant(
            '{"parameter_patch":{"componentsReplacement":{"mode":"replace","components":['
            '{"type":"protein","sequence":"AAAEEE","numCopies":1,"useMsa":true},'
            '{"type":"ligand","sequence":"c1ccccc1","numCopies":1,"inputMethod":"smiles"}'
            '],"clearConstraints":true}},"needs_confirmation":true,"execute_now":false}'
        )
        actions = assistant.plan_actions(
            context_type="task_detail",
            context_payload={"project": {"task_type": "Structure Prediction"}},
            user_id="user-1",
            username="alice",
            content="帮我提交一下新的任务\n\n蛋白序列为AAAEEE\n小分子为c1ccccc1",
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["id"], "task_detail:apply_patch_and_submit")
        components = actions[0]["payload"]["parameterPatch"]["componentsReplacement"]["components"]
        self.assertEqual(
            [(item["type"], item["sequence"]) for item in components],
            [("protein", "AAAEEE"), ("ligand", "c1ccccc1")],
        )
        self.assertEqual(session.last_request["json"]["response_format"], {"type": "json_object"})

    def test_task_detail_plan_repair_uses_model_schema_not_local_fallback(self):
        class SequenceSession:
            def __init__(self):
                self.responses = [
                    "我会帮你处理，但没有 JSON。",
                    '{"parameter_patch":{"componentsReplacement":{"mode":"replace","components":['
                    '{"type":"protein","sequence":"AAAEEE","numCopies":1,"useMsa":true},'
                    '{"type":"ligand","sequence":"c1ccccc1","numCopies":1,"inputMethod":"smiles"}'
                    '],"clearConstraints":true}},"needs_confirmation":true,"execute_now":false}',
                ]
                self.requests = []

            def post(self, url, headers, json, timeout):
                self.requests.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
                return FakeResponse(self.responses.pop(0))

        session = SequenceSession()
        assistant = CopilotAssistant(
            chat_api_url="http://example.test/v1/chat/completions",
            chat_api_key="test-key",
            chat_model="gemma4-31b",
            timeout_seconds=3,
            session=session,
            logger=type("Logger", (), {"warning": lambda *args, **kwargs: None})(),
        )
        actions = assistant.plan_actions(
            context_type="task_detail",
            context_payload={"project": {"task_type": "Structure Prediction"}},
            user_id="user-1",
            username="alice",
            content="帮我提交一下新的任务\n\n蛋白序列为AAAEEE\n小分子为c1ccccc1",
        )
        self.assertEqual(len(session.requests), 2)
        self.assertIn("previous planning output did not satisfy the schema", session.requests[1]["json"]["messages"][0]["content"])
        self.assertEqual(actions[0]["id"], "task_detail:apply_patch_and_submit")

    def test_task_detail_plan_schema_failure_is_visible_for_mutating_request(self):
        class InvalidSchemaSession:
            def __init__(self):
                self.responses = [
                    '{"actions":[]}',
                    '{"capability":"task_submission_planning","parameter_patch":{},"needs_confirmation":true,"execute_now":false}',
                ]

            def post(self, url, headers, json, timeout):
                return FakeResponse(self.responses.pop(0))

        session = InvalidSchemaSession()
        assistant = CopilotAssistant(
            chat_api_url="http://example.test/v1/chat/completions",
            chat_api_key="test-key",
            chat_model="gemma4-31b",
            timeout_seconds=3,
            session=session,
            logger=type("Logger", (), {"warning": lambda *args, **kwargs: None})(),
        )
        with self.assertRaisesRegex(ValueError, "valid confirmation action"):
            assistant.plan_actions(
                context_type="task_detail",
                context_payload={"project": {"task_type": "Structure Prediction"}},
                user_id="user-1",
                username="alice",
                content="帮我提交一下新的任务\n\n蛋白序列为AAAEEE\n小分子为c1ccccc1",
            )

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

    def test_task_detail_attachment_application_requires_confirmation(self):
        actions = build_task_submission_actions(
            {
                "capability": "attachment_application",
                "intent": "apply_uploaded_files",
                "attachment_applications": [
                    {"attachmentId": "file-target", "fileName": "target.cif", "role": "target"},
                    {"attachmentId": "file-ligand", "fileName": "ligand.sdf", "role": "ligand"},
                ],
                "needs_confirmation": True,
                "execute_now": False,
            },
            "把 @target.cif 作为 target，把 @ligand.sdf 作为 ligand",
            "affinity",
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["id"], "task_detail:apply_copilot_attachments")
        self.assertTrue(actions[0]["needs_confirmation"])
        self.assertFalse(actions[0]["execute_now"])
        self.assertEqual(
            actions[0]["payload"]["attachmentApplications"],
            [
                {"attachmentId": "file-target", "fileName": "target.cif", "role": "target"},
                {"attachmentId": "file-ligand", "fileName": "ligand.sdf", "role": "ligand"},
            ],
        )

    def test_task_detail_attachment_application_rejects_wrong_workflow_roles(self):
        prediction_actions = build_task_submission_actions(
            {
                "capability": "attachment_application",
                "attachment_applications": [
                    {"attachmentId": "file-target", "fileName": "target.cif", "role": "target"},
                ],
                "needs_confirmation": True,
                "execute_now": False,
            },
            "把 @target.cif 作为 target",
            "prediction",
        )
        peptide_actions = build_task_submission_actions(
            {
                "capability": "attachment_application",
                "attachment_applications": [
                    {"attachmentId": "file-ligand", "fileName": "ligand.sdf", "role": "ligand"},
                ],
                "needs_confirmation": True,
                "execute_now": False,
            },
            "把 @ligand.sdf 作为 ligand",
            "peptide_design",
        )
        self.assertEqual(prediction_actions, [])
        self.assertEqual(peptide_actions, [])

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

    def test_task_list_create_with_sequence_rejects_missing_declared_ligand(self):
        actions = build_context_actions(
            "task_list",
            {
                "actions": [
                    {
                        "id": "tasks:create_with_sequence",
                        "payload": {
                            "create": True,
                            "components": [
                                {"type": "protein", "sequence": "AAAEEE", "numCopies": 1, "useMsa": True},
                            ],
                        },
                    }
                ]
            },
            {"project": {"task_type": "Structure Prediction"}, "rows": []},
            "帮我新建一个任务\n\n蛋白序列为AAAEEE 小分子为CCCCO",
        )
        self.assertEqual(actions, [])

    def test_task_list_create_with_sequence_accepts_uppercase_declared_ligand(self):
        actions = build_context_actions(
            "task_list",
            {
                "actions": [
                    {
                        "id": "tasks:create_with_sequence",
                        "payload": {
                            "create": True,
                            "components": [
                                {"type": "protein", "sequence": "AAAEEE", "numCopies": 1, "useMsa": True},
                                {"type": "ligand", "sequence": "CCCCO", "numCopies": 1, "inputMethod": "smiles"},
                            ],
                        },
                    }
                ]
            },
            {"project": {"task_type": "Structure Prediction"}, "rows": []},
            "帮我新建一个任务\n\n蛋白序列为AAAEEE 小分子为CCCCO",
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(
            actions[0]["payload"]["components"],
            [
                {"type": "protein", "sequence": "AAAEEE", "numCopies": 1, "useMsa": True},
                {"type": "ligand", "sequence": "CCCCO", "numCopies": 1, "inputMethod": "smiles"},
            ],
        )

    def test_context_actions_do_not_confirm_when_missing_questions_present(self):
        actions = build_context_actions(
            "task_list",
            {
                "missing_questions": ["这个未标注的 CCCC 是小分子、CCD ID 还是肽段？"],
                "actions": [
                    {
                        "id": "tasks:create_with_sequence",
                        "payload": {
                            "create": True,
                            "components": [
                                {"type": "protein", "sequence": "CCCC", "numCopies": 1, "useMsa": True},
                            ],
                        },
                    }
                ],
            },
            {"project": {"task_type": "Structure Prediction"}, "rows": []},
            "新建 CCCC",
        )
        self.assertEqual(actions, [])

    def test_task_detail_missing_questions_do_not_create_confirmation_action(self):
        actions = build_task_submission_actions(
            {
                "capability": "clarification_needed",
                "intent": "ask_clarifying_question",
                "missing_questions": ["请说明 CCCC 是小分子、CCD ID 还是蛋白/肽段序列。"],
                "needs_confirmation": False,
                "execute_now": False,
            },
            "把 CCCC 填进去并运行",
            "prediction",
        )
        self.assertEqual(actions, [])

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
