"""Convergence-failure behavior — the "task_001" regression.

Production failure (management_api.log, 14:35 UTC): the planner, asked to dock KLK with
ibuprofen on an EMPTY task list, invented a task row id (task_001) for an existing-task
action. The audit rejected it, the model repeated the identical output, and the turn died
into "I could not complete this request. Please try rephrasing." — telling a user with a
perfectly good request to rephrase. This suite pins the three fixes:

1. The row-reference audit TEACHES the environment boundary (row count, legal ids, or that
   no rows exist so only the create action is legal).
2. The no-convergence terminal speaks the user's language, names the actual blocker, and
   explicitly says the request needs no rephrasing.
3. Every rejected round is logged (visibility for exactly this kind of diagnosis).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from management_api.copilot import (
    CopilotAssistant,
    _no_convergence_failure_message,
    _user_text_looks_chinese,
)
from management_api.copilot_capabilities import CopilotSkillDefinition
from tests.test_copilot_workflow_environment import (
    SequenceModelSession,
    make_assistant,
    patch_host_skills,
)


def _task_open_skill() -> CopilotSkillDefinition:
    # The real tasks:open shape (task_list context, taskRowId required) — minimal stand-in
    # exercising the same audit path.
    return CopilotSkillDefinition(
        name="tasks:open",
        label="Open task",
        description="Open a task by id.",
        context_type="task_list",
        target_context="task_detail",
        effect="navigate",
        input_schema={
            "type": "object",
            "properties": {"taskRowId": {"type": "string"}},
            "required": ["taskRowId"],
            "additionalProperties": False,
        },
    )


FABRICATED_OPEN_TURN = {
    "message": "正在打开任务。",
    "questions": [],
    "operations": [
        {"id": "o1", "skill": "tasks:open", "arguments": {"taskRowId": "task_001"}, "depends_on": []}
    ],
}


class NoConvergenceMessageUnitTests(unittest.TestCase):
    def test_detects_user_language_by_cjk_ratio(self) -> None:
        self.assertTrue(_user_text_looks_chinese("我想对接klk和布洛芬"))
        self.assertFalse(_user_text_looks_chinese("dock KLK with ibuprofen please"))

    def test_fabricated_row_id_gets_tailored_chinese_copy(self) -> None:
        message = _no_convergence_failure_message(
            ["operations[0].arguments.taskRowId (task_001) is not a task row in the current context"],
            context_row_count=0,
            pending_held_writes=[],
            user_text="我想对接klk和布洛芬",
        )
        self.assertIn("task_001", message)
        self.assertIn("没有任何任务", message)
        self.assertIn("再发一次", message)
        self.assertNotIn("rephrasing", message)

    def test_fabricated_row_id_gets_tailored_english_copy(self) -> None:
        message = _no_convergence_failure_message(
            ["operations[0].arguments.taskRowId (task_001) is not a task row in the current context"],
            context_row_count=3,
            pending_held_writes=[],
            user_text="dock KLK with ibuprofen please",
        )
        self.assertIn("task_001", message)
        self.assertIn("has no such task id", message)
        self.assertIn("send it again", message)
        self.assertNotIn("rephrasing", message)

    def test_unknown_family_keeps_honest_generic_copy_with_held_writes(self) -> None:
        class _Op:
            skill = "tasks:create_docking"

        message = _no_convergence_failure_message(
            ["operations[0].arguments: schema violation"],
            context_row_count=0,
            pending_held_writes=[_Op()],
            user_text="我想对接klk和布洛芬",
        )
        self.assertIn("没能", message)
        self.assertIn("tasks:create_docking", message)


class ConvergenceTurnTests(unittest.TestCase):
    def test_repeated_fabricated_row_id_gets_boundary_feedback_and_honest_failure(self) -> None:
        """End-to-end replay of the production failure: the model repeats the fabricated
        task_001 operation until the loop breaks. The FEEDBACK must teach the boundary (no
        rows → create action), and the user-visible failure must explain the real blocker in
        Chinese instead of demanding a rephrase."""
        responses = [FABRICATED_OPEN_TURN, FABRICATED_OPEN_TURN, FABRICATED_OPEN_TURN]
        assistant, session = make_assistant(responses)
        with patch_host_skills(_task_open_skill()):
            result = assistant.plan_turn(
                context_type="task_list",
                context_payload={"page": {"contextType": "task_list"}, "rows": []},
                user_id="u",
                username="alice",
                content="我想对接klk和布洛芬",
            )
        self.assertEqual(result["state"], "failed")
        # 1. The planner was TAUGHT the environment boundary, not just the violation.
        feedback_texts = [
            str(message.get("content") or "")
            for request in session.requests
            for message in request["json"]["messages"]
        ]
        self.assertTrue(
            any("NO task rows at all" in text and "create action" in text for text in feedback_texts),
            "row-reference rejection must teach: no rows exist, use the create action",
        )
        # 2. The user sees the honest, specific, Chinese failure — not "please rephrase".
        self.assertIn("task_001", result["content"])
        self.assertIn("没有任何任务", result["content"])
        self.assertIn("再发一次", result["content"])
        self.assertNotIn("rephrasing", result["content"])

    def test_nonempty_row_list_feedback_names_legal_ids(self) -> None:
        responses = [FABRICATED_OPEN_TURN, FABRICATED_OPEN_TURN, FABRICATED_OPEN_TURN]
        assistant, session = make_assistant(responses)
        with patch_host_skills(_task_open_skill()):
            result = assistant.plan_turn(
                context_type="task_list",
                context_payload={
                    "page": {"contextType": "task_list"},
                    "rows": [{"id": "row-a"}, {"id": "row-b"}],
                },
                user_id="u",
                username="alice",
                content="dock KLK with ibuprofen please",
            )
        self.assertEqual(result["state"], "failed")
        feedback_texts = [
            str(message.get("content") or "")
            for request in session.requests
            for message in request["json"]["messages"]
        ]
        self.assertTrue(
            any("2 task row(s)" in text and "row-a" in text for text in feedback_texts),
            "row-reference rejection must name the legal row ids",
        )
        self.assertIn("has no such task id", result["content"])


if __name__ == "__main__":
    unittest.main()
