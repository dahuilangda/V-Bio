"""Test skill: task_detail context — live skill registry surface.

Covers build_task_detail_skill_definitions (the single live entry point for task-detail skills,
including the per-workflow apply_parameter_patch schema) and infer_workflow_key. The former legacy
build_task_submission_actions / sanitize_task_parameter_patch functions were production-dead — the
harness path builds actions via build_confirmation_actions and the parameter constraints now live
in _task_parameter_json_schema, enforced by the model-server grammar + harness audit — so they have
been removed; their constraint semantics are now asserted here against the live schema.
"""

from __future__ import annotations

import unittest
from typing import Any, Dict

from management_api.copilot_capabilities import (
    build_task_detail_skill_definitions,
    infer_workflow_key,
)


class TestTaskDetailParameterPatchSchema(unittest.TestCase):
    """task_detail: the live apply_parameter_patch skill schema encodes the per-workflow parameter
    constraints (the model-server grammar enforces them at generation time; the harness re-validates).
    Live successor to the former sanitize_task_parameter_patch legacy tests."""

    def _patch_schema(self, workflow_key: str) -> Dict[str, Any]:
        definitions = {d.name: d for d in build_task_detail_skill_definitions(workflow_key)}
        self.assertIn("task_detail:apply_parameter_patch", definitions)
        return definitions["task_detail:apply_parameter_patch"].input_schema["properties"]["parameterPatch"]

    def test_prediction_exposes_seed_backend_replacement(self):
        props = self._patch_schema("prediction")["properties"]
        self.assertIn("seed", props)
        self.assertEqual(
            props["backend"]["enum"],
            ["boltz", "alphafold3", "protenix", "protenix2dock", "boltz2dock"])
        self.assertIn("componentsReplacement", props)

    def test_virtual_screening_backend_is_nesso_only(self):
        props = self._patch_schema("virtual_screening")["properties"]
        self.assertEqual(props["backend"]["enum"], ["nesso"])

    def test_peptide_design_exposes_chirality_and_families(self):
        props = self._patch_schema("peptide_design")["properties"]
        self.assertEqual(props["peptideChirality"]["enum"], ["l", "d"])
        self.assertEqual(props["peptideBicyclicLinkerCcd"]["enum"], ["SEZ", "29N", "BS3"])
        for key in ("peptideLengthMin", "peptideLengthMax",
                    "peptideNonNaturalMin", "peptideNonNaturalMax",
                    "peptidePocketResidues"):
            self.assertIn(key, props)
        self.assertNotIn("peptideMutationRate", props)

    def test_affinity_exposes_mode(self):
        props = self._patch_schema("affinity")["properties"]
        # dock leads the enum — it is the default docking mode; the description names it as
        # default so the planner never invents unsupported calculation modes.
        self.assertEqual(props["affinityMode"]["enum"], ["dock", "score", "pose", "refine", "interface"])
        self.assertIn("DEFAULT mode", props["affinityMode"]["description"])

    def test_lead_optimization_has_no_parameter_patch_skill(self):
        definitions = {d.name: d for d in build_task_detail_skill_definitions("lead_optimization")}
        self.assertNotIn("task_detail:apply_parameter_patch", definitions)


class TestStructureTemplateSkill(unittest.TestCase):
    def test_apply_structure_template_registered_for_task_detail(self) -> None:
        definitions = {d.name: d for d in build_task_detail_skill_definitions("prediction")}
        self.assertIn("task_detail:apply_structure_template", definitions)
        schema = definitions["task_detail:apply_structure_template"].input_schema
        # Identifier-first: pdbId (host builds the mmCIF URL) or an explicit cifUrl — one of the two.
        self.assertEqual(schema["required"], [])
        self.assertIn({"required": ["structurePdbId"]}, schema["anyOf"])
        self.assertIn({"required": ["structureUrl"]}, schema["anyOf"])
        self.assertIn("structureUrl", schema["properties"])

    def test_affinity_structure_and_ligand_skills_registered(self) -> None:
        definitions = {d.name: d for d in build_task_detail_skill_definitions("affinity")}
        self.assertIn("task_detail:apply_docking_target_structure", definitions)
        self.assertIn("task_detail:apply_docking_ligand_smiles", definitions)
        self.assertEqual(
            definitions["task_detail:apply_docking_ligand_smiles"].input_schema["required"], ["smiles"]
        )


class TestInferWorkflowKey(unittest.TestCase):
    def test_prediction_variants(self) -> None:
        self.assertEqual(infer_workflow_key({"workflow": "prediction"}), "prediction")
        self.assertEqual(infer_workflow_key({"workflow": "boltz_2_prediction"}), "prediction")

    def test_affinity_variants(self):
        self.assertEqual(infer_workflow_key({"project": {"task_type": "Affinity Scoring"}}), "affinity")

    def test_virtual_screening_variants(self):
        self.assertEqual(infer_workflow_key({"workflow": "virtual-screening"}), "virtual_screening")
        self.assertEqual(infer_workflow_key({"project": {"task_type": "Virtual Screening"}}), "virtual_screening")

    def test_peptide_design_variants(self):
        self.assertEqual(infer_workflow_key({"project": {"task_type": "Peptide Design"}}), "peptide_design")

    def test_lead_optimization_variants(self):
        self.assertEqual(infer_workflow_key({"project": {"task_type": "Lead Optimization"}}), "lead_optimization")

    def test_defaults_to_prediction(self):
        self.assertEqual(infer_workflow_key({}), "prediction")


if __name__ == "__main__":
    unittest.main()


class CapabilityBoundarySyncTests(unittest.TestCase):
    """Sync guards: capability boundaries derive from single sources (schemas, workflow keys), so
    adding / renaming / upgrading a feature keeps every surface in agreement. Each guard pins one
    sync point so a drift fails here instead of in production planning.
    """

    KNOWN_WORKFLOWS = {"prediction", "virtual_screening", "affinity", "peptide_design", "lead_optimization"}

    def test_workflow_gating_lists_reference_known_workflows(self):
        from management_api.copilot_skills.task_list import TASK_LIST_ACTION_SCHEMAS
        from management_api.copilot_skills.project_list import PROJECT_LIST_ACTION_SCHEMAS
        for registry in (TASK_LIST_ACTION_SCHEMAS, PROJECT_LIST_ACTION_SCHEMAS):
            for action_id, schema in registry.items():
                for workflow in schema.get("requires_workflow") or []:
                    self.assertIn(workflow, self.KNOWN_WORKFLOWS, f"{action_id} gates on unknown workflow {workflow}")

    def test_workflow_parameter_keys_reference_known_workflows_and_parameters(self):
        from management_api.copilot_capabilities import TASK_PARAMETER_SCHEMA, WORKFLOW_PARAMETER_KEYS
        for workflow, keys in WORKFLOW_PARAMETER_KEYS.items():
            self.assertIn(workflow, self.KNOWN_WORKFLOWS)
            for key in keys:
                self.assertIn(key, TASK_PARAMETER_SCHEMA, f"{workflow} patches undeclared parameter {key}")

    def test_docking_skills_exposed_for_docking_workflow_only(self):
        docking = {d.name for d in build_task_detail_skill_definitions("affinity")}
        other = {d.name for d in build_task_detail_skill_definitions("prediction")}
        self.assertIn("task_detail:apply_docking_target_structure", docking)
        self.assertIn("task_detail:apply_docking_ligand_smiles", docking)
        self.assertNotIn("task_detail:apply_docking_target_structure", other)
        self.assertNotIn("task_detail:apply_docking_ligand_smiles", other)
        # legacy affinity-named skills are gone everywhere
        for names in (docking, other):
            self.assertNotIn("task_detail:apply_affinity_target_structure", names)
            self.assertNotIn("task_detail:apply_affinity_ligand_smiles", names)

    def test_parameter_patch_description_enumerates_exactly_the_schema_keys(self):
        # The description is DERIVED from the schema (single source) — it can never drift from the
        # keys the audit actually accepts.
        for workflow in self.KNOWN_WORKFLOWS:
            definitions = {d.name: d for d in build_task_detail_skill_definitions(workflow)}
            patch = definitions.get("task_detail:apply_parameter_patch")
            from management_api.copilot_capabilities import _task_parameter_json_schema
            schema = _task_parameter_json_schema(workflow)
            if not schema["properties"]:
                self.assertIsNone(patch, f"{workflow} unexpectedly exposes a parameter patch")
                continue
            self.assertIsNotNone(patch, f"{workflow} is missing its parameter patch")
            for key in schema["properties"]:
                self.assertIn(key, patch.description, f"{workflow} patch description omits {key}")

    def test_capability_catalog_default_mode_matches_schema(self):
        from management_api.copilot_capabilities import build_registered_capability_catalog, TASK_PARAMETER_SCHEMA
        catalog = build_registered_capability_catalog()
        affinity_params = next(w for w in catalog["workflows"] if w["workflow"] == "affinity")["parameters"]
        mode = next(p for p in affinity_params if p["field"] == "affinityMode")
        self.assertEqual(mode["schema"]["values"][0], "dock", "dock must lead the mode enum (it is the default)")
        self.assertIn("DEFAULT mode", mode["schema"]["description"])


class TestDockingPocketBoxSkill(unittest.TestCase):
    """The dock-mode pocket requirement is a first-class skill: the host auto-derives the box
    (co-crystallized ligand site, else whole protein), exposed only for the affinity workflow."""

    def test_registered_for_affinity_only(self) -> None:
        affinity = {d.name for d in build_task_detail_skill_definitions("affinity")}
        prediction = {d.name for d in build_task_detail_skill_definitions("prediction")}
        self.assertIn("task_detail:set_docking_pocket_box", affinity)
        self.assertNotIn("task_detail:set_docking_pocket_box", prediction)

    def test_schema_mode_defaults_sensible(self) -> None:
        definitions = {d.name: d for d in build_task_detail_skill_definitions("affinity")}
        skill = definitions["task_detail:set_docking_pocket_box"]
        self.assertEqual(skill.effect, "update")
        self.assertEqual(skill.input_schema["properties"]["mode"]["enum"], ["auto", "protein"])
        # mode is optional: the host's 'auto' default is the smart path.
        self.assertEqual(skill.input_schema["required"], [])
