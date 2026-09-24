"""Test skill: task_list context — live skill registry surface.

Covers build_context_skill_definitions, the single live entry point that turns the declarative
task-list action schemas into schema-backed planner skills (including per-workflow gating such as
the virtual-screening-only create action). The former legacy build_context_actions sanitizers were
production-dead and have been removed; model-driven plan_actions flows are covered by
test_copilot_turn / test_copilot_protocol against the planner+harness contract.
"""

from __future__ import annotations

import unittest

from management_api.copilot_skills.context_actions import build_context_skill_definitions


class TestTaskListSchemaGeneration(unittest.TestCase):
    """task_list: the live skill registry exposes the expected action ids."""

    def _names(self) -> set:
        return {
            definition.name
            for definition in build_context_skill_definitions("task_list", {"project": {"task_type": "prediction"}}, workflow_key="prediction")
        }

    def test_filter_actions(self):
        names = self._names()
        # Duplicate filter shortcuts were consolidated into tasks:update_view.
        self.assertIn("tasks:update_view", names)
        self.assertIn("tasks:clear_filters", names)

    def test_sort_actions(self):
        names = self._names()
        # Sort shortcuts were consolidated into tasks:update_view.
        self.assertIn("tasks:update_view", names)

    def test_create_actions(self):
        names = self._names()
        self.assertIn("tasks:create", names)
        self.assertIn("tasks:create_with_sequence", names)

    def test_delete_action(self):
        self.assertIn("tasks:delete", self._names())

    def test_open_rename_cancel_actions(self):
        names = self._names()
        for expected in ("tasks:open", "tasks:rename", "tasks:cancel", "tasks:copy"):
            self.assertIn(expected, names)

    def test_copy_is_atomic_without_parameter_patch(self):
        # tasks:copy is a single unit: copy the row into a new draft. Parameter changes are
        # separate operations on the task-detail page (task_detail:apply_parameter_patch), so
        # the copy skill must NOT carry a fused parameterPatch/component patch.
        defs = build_context_skill_definitions("task_list", {"project": {"task_type": "prediction"}}, workflow_key="prediction")
        copy = next(d for d in defs if d.name == "tasks:copy")
        self.assertEqual(copy.effective_target_context, "task_detail")
        properties = copy.input_schema["properties"]
        self.assertNotIn("parameterPatch", properties)
        self.assertNotIn("componentsPatch", properties)
        self.assertEqual(copy.input_schema["required"], ["taskRowId"])
        self.assertIs(copy.input_schema["additionalProperties"], False)
        # The fused copy+patch skill must not exist anywhere in the catalog.
        self.assertNotIn("tasks:copy_with_patch", {d.name for d in defs})


class TestVirtualScreeningSkillGating(unittest.TestCase):
    """tasks:create_virtual_screening is exposed only for the virtual_screening workflow."""

    def _names(self, workflow_key, task_type):
        return {
            d.name for d in build_context_skill_definitions("task_list", {"project": {"task_type": task_type}, "rows": []}, workflow_key=workflow_key)
        }

    def test_skill_only_exposed_for_virtual_screening_workflow(self):
        vs = self._names("virtual_screening", "Virtual Screening")
        pred = self._names("prediction", "Structure Prediction")
        self.assertIn("tasks:create_virtual_screening", vs)
        self.assertNotIn("tasks:create_virtual_screening", pred)


class TestDockingSkillGating(unittest.TestCase):
    """tasks:create_docking is the affinity (docking) workflow's create action — a target
    STRUCTURE url, never a sequence — and is withheld from every other workflow."""

    def _names(self, workflow_key, task_type):
        return {
            d.name for d in build_context_skill_definitions("task_list", {"project": {"task_type": task_type}, "rows": []}, workflow_key=workflow_key)
        }

    def test_skill_only_exposed_for_affinity_workflow(self):
        dock = self._names("affinity", "docking")
        pred = self._names("prediction", "Structure Prediction")
        self.assertIn("tasks:create_docking", dock)
        self.assertNotIn("tasks:create_docking", pred)
        # The sequence-based create path stays withheld for docking — a sequence cannot fill
        # a docking target.
        self.assertNotIn("tasks:create_with_sequence", dock)

    def test_docking_create_carries_target_structure_not_sequence(self):
        defs = build_context_skill_definitions(
            "task_list", {"project": {"task_type": "docking"}, "rows": []}, workflow_key="affinity"
        )
        create = next(d for d in defs if d.name == "tasks:create_docking")
        self.assertEqual(create.effective_target_context, "task_detail")
        # The preferred path is the entry IDENTIFIER (targetPdbId) — the host builds the
        # guaranteed-valid mmCIF URL itself; a raw URL is the explicit fallback only.
        self.assertEqual(create.input_schema["required"], ["create"])
        variants = create.input_schema["anyOf"]
        self.assertIn({"required": ["targetPdbId"]}, variants)
        self.assertIn({"required": ["targetStructureUrl"]}, variants)
        properties = create.input_schema["properties"]
        self.assertIn("targetPdbId", properties)
        self.assertNotIn("components", properties)
        self.assertNotIn("sequence", properties)
        self.assertNotIn("proteinSequence", properties)
        self.assertIn("targetStructureUrl", properties)
        self.assertIn("ligandSmiles", properties)


if __name__ == "__main__":
    unittest.main()
