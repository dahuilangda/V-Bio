"""Test skill: project_list context — live skill registry surface.

Covers build_context_skill_definitions, the single live entry point that turns the declarative
project-list action schemas into schema-backed planner skills. (The former legacy
build_context_actions sanitizers were production-dead — /plan_actions proxies plan_turn and the
harness path builds actions via build_confirmation_actions — and have been removed along with the
host-page action model-driven plan_actions flows, which are covered by test_copilot_turn /
test_copilot_protocol against the planner+harness contract.)
"""

from __future__ import annotations

import unittest

from management_api.copilot_skills.context_actions import build_context_skill_definitions


class TestProjectListSchemaGeneration(unittest.TestCase):
    """project_list: the live skill registry exposes the expected action ids."""

    def _names(self) -> set:
        return {definition.name for definition in build_context_skill_definitions("project_list", {})}

    def test_filter_actions(self):
        names = self._names()
        # Duplicate filter shortcuts were consolidated into projects:update_view.
        self.assertIn("projects:update_view", names)
        self.assertIn("projects:clear_filters", names)

    def test_sort_actions(self):
        names = self._names()
        # Sort shortcuts were consolidated into projects:update_view.
        self.assertIn("projects:update_view", names)

    def test_create_action(self):
        self.assertIn("projects:create", self._names())

    def test_delete_action(self):
        self.assertIn("projects:delete", self._names())

    def test_update_view_rejects_empty_payload(self):
        # requires_any_payload is encoded as minProperties: an empty "change nothing" payload
        # must fail the harness audit instead of producing a no-op confirmation.
        from management_api.copilot_skill_harness import CopilotSkillHarness

        class _EmptySkills:
            @property
            def definitions(self):
                return []

        harness = CopilotSkillHarness(skills=_EmptySkills())
        audit = harness.audit_plan(
            {
                "message": "Filtering the list.",
                "questions": [],
                "operations": [
                    {"id": "w1", "skill": "projects:update_view", "arguments": {}, "depends_on": []}
                ],
            },
            {definition.name: definition for definition in build_context_skill_definitions("project_list", {})},
        )
        self.assertTrue(any("fewer properties" in issue for issue in audit.issues))

    def test_open_and_cancel_actions(self):
        names = self._names()
        self.assertIn("projects:open", names)
        self.assertIn("projects:cancel_active", names)


if __name__ == "__main__":
    unittest.main()
