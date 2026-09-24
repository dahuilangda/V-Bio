"""ENTITY IDENTITY contract — the "KLK" ambiguity regression.

Production history: a user asking to dock "KLK" with ibuprofen never said WHICH kallikrein
isoform or WHICH organism, yet the old contracts told the planner to "default the search to
the human form" — a silent fallback that also swallowed the isoform dimension entirely (the
search returned a mix of KLK4/KLK5 entries and even an unrelated BRD4 structure ranked
first). Per the pi-agent principles the project follows (minimal prompt, contracts live on
the skills, no silent fallbacks): an unstated identity dimension is an UNRESOLVED CHOICE the
user must resolve, and every candidate offered must state its identity dimensions.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from management_api.copilot import CopilotAssistant, _no_convergence_failure_message
from management_api.copilot_skills.online_databases import OnlineDatabaseSkills, OnlineSkillDefinition
from tests.helpers import NullLogger
from tests.test_copilot_workflow_environment import (
    SequenceModelSession,
    _read_op,
    _turn,
    inject_read_skills,
    make_assistant,
    patch_host_skills,
)


HUMAN_DEFAULT_MARKERS = (
    "defaults to the human form",
    "default the search to the human form",
)


def _real_database_skill_descriptions() -> dict[str, str]:
    session = SequenceModelSession([])
    skills = OnlineDatabaseSkills(session=session, timeout_seconds=3)
    return {definition.name: definition.description for definition in skills.definitions}


class SkillContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.descriptions = _real_database_skill_descriptions()

    def test_no_silent_human_default_remains_in_any_skill_contract(self) -> None:
        for name, description in self.descriptions.items():
            for marker in HUMAN_DEFAULT_MARKERS:
                self.assertNotIn(marker, description, f"{name} still carries the human fallback")

    def test_rcsb_search_declares_identity_resolution_contract(self) -> None:
        description = self.descriptions["rcsb.search"]
        self.assertIn("unresolved choice", description)  # never a default
        self.assertIn("isoform", description)
        self.assertIn("organism", description)
        self.assertIn("Never assume an unstated organism", description)

    def test_uniprot_search_declares_identity_resolution_contract(self) -> None:
        description = self.descriptions["uniprot.search"]
        self.assertIn("unresolved choice", description)
        self.assertIn("isoform", description)
        self.assertIn("organism", description)


class SystemPromptContractTests(unittest.TestCase):
    def _system_prompt(self) -> str:
        responses = [_turn("好的。")]
        assistant, session = make_assistant(responses)
        with patch_host_skills():
            assistant.plan_turn(
                context_type="task_detail",
                context_payload={"page": {"contextType": "task_detail", "workflowKey": "prediction"}, "project": {"id": "p"}},
                user_id="u",
                username="alice",
                content="继续",
            )
        return str(session.requests[0]["json"]["messages"][0]["content"])

    def test_entity_identity_is_a_required_determination(self) -> None:
        prompt = self._system_prompt()
        self.assertIn("ENTITY IDENTITY is a required determination", prompt)
        self.assertIn("UNRESOLVED choice", prompt)
        self.assertIn("isoform", prompt)

    def test_human_default_fallback_is_gone_from_the_prompt(self) -> None:
        prompt = self._system_prompt()
        for marker in HUMAN_DEFAULT_MARKERS:
            self.assertNotIn(marker, prompt)

    def test_prompt_carries_no_concrete_case_examples(self) -> None:
        prompt = self._system_prompt()
        self.assertNotIn("(e.g.", prompt)
        self.assertNotIn("例如", prompt)


class KlkAmbiguityTurnTests(unittest.TestCase):
    """End-to-end: 'KLK' without isoform/organism → search without an invented dimension,
    then a choice question whose options state identity dimensions. The loop must ACCEPT
    that question turn (regression guard for the prompt/contract change)."""

    def _klk_rcsb_skill(self) -> OnlineSkillDefinition:
        return OnlineSkillDefinition(
            name="rcsb.search",
            description="Search RCSB PDB by free text.",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string", "minLength": 1}, "size": {"type": "integer", "minimum": 1, "maximum": 10}},
                "required": ["text"],
                "additionalProperties": False,
            },
        )

    def _klk_handler(self, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "source": "rcsb",
            "query": args.get("text", ""),
            "count": 3,
            "results": [
                {"pdbId": "2BDG", "title": "Human Kallikrein 4 complex with nickel and p-aminobenzamidine", "method": "X-ray", "resolution": 1.95},
                {"pdbId": "1FK9", "title": "Mouse kallikrein 1 related peptidase", "method": "X-ray", "resolution": 2.0},
                {"pdbId": "2PSX", "title": "Human Kallikrein 5 in complex with Leupeptin", "method": "X-ray", "resolution": 2.3},
            ],
        }

    def test_klk_without_organism_ends_in_identity_stating_choice(self) -> None:
        responses = [
            _turn("检索中。", operations=[_read_op("s1", "rcsb.search", {"text": "kallikrein", "size": 5})]),
            _turn(
                "KLK 是一个基因家族，需要确定亚型和物种。请选择靶点：",
                operations=[],
                questions=[
                    {
                        "kind": "choice",
                        "text": "请选择 kallikrein 的具体亚型和物种：",
                        "options": [
                            {"label": "2BDG — Human Kallikrein 4（X-ray, 1.95 Å）", "value": "2BDG"},
                            {"label": "1FK9 — Mouse kallikrein 1（X-ray, 2.0 Å）", "value": "1FK9"},
                            {"label": "2PSX — Human Kallikrein 5（X-ray, 2.3 Å）", "value": "2PSX"},
                        ],
                    }
                ],
            ),
        ]
        assistant, session = make_assistant(responses)
        inject_read_skills(assistant, {"rcsb.search": (self._klk_rcsb_skill(), self._klk_handler)})
        with patch_host_skills():
            result = assistant.plan_turn(
                context_type="task_list",
                context_payload={"page": {"contextType": "task_list"}, "rows": []},
                user_id="u",
                username="alice",
                content="我想对接klk和布洛芬",
            )
        self.assertEqual(result["state"], "needs_input")
        self.assertEqual(len(result["questions"]), 1)
        option_labels = " | ".join(opt["label"] for opt in result["questions"][0]["options"])
        # Identity dimensions are stated per option: organism AND isoform.
        self.assertIn("Human Kallikrein 4", option_labels)
        self.assertIn("Mouse kallikrein 1", option_labels)
        self.assertIn("Human Kallikrein 5", option_labels)

    def test_silent_single_pick_across_isoforms_is_held_for_choice(self) -> None:
        """The structural backstop behind the prompt rule: a write consuming ONE record of a
        multi-isoform search, when the user named only the family, must be held and turned
        into a question — the planner may not silently pick KLK4 when the user said KLK.
        The docking skill is registered so the round-1 read actually EXECUTES (the question
        audit requires cited ids to come from real observations)."""
        from management_api.copilot_capabilities import CopilotSkillDefinition

        create_docking = CopilotSkillDefinition(
            name="tasks:create_docking",
            label="New docking task",
            description="Create a docking task with a target structure and ligand SMILES.",
            context_type="task_list",
            target_context="task_detail",
            effect="create",
            input_schema={
                "type": "object",
                "properties": {
                    "create": {"type": "boolean"},
                    "targetPdbId": {"type": "string"},
                    "ligandSmiles": {"type": "string"},
                },
                "required": ["create"],
                "additionalProperties": False,
            },
        )
        responses = [
            _turn(
                "检索并选定。",
                operations=[
                    _read_op("s1", "rcsb.search", {"text": "kallikrein", "size": 5}),
                    {
                        "id": "w1",
                        "skill": "tasks:create_docking",
                        "arguments": {
                            "create": True,
                            "targetPdbId": {"$fromObservation": "s1", "index": 0},
                            "ligandSmiles": "CC(C)CC1=CC=C(C=C1)C(C)C(=O)O",
                        },
                        "depends_on": ["s1"],
                    },
                ],
            ),
        ]
        assistant, session = make_assistant(responses + [
            _turn(
                "请选择结构：",
                questions=[
                    {
                        "kind": "choice",
                        "text": "请选择 kallikrein 的具体亚型和物种：",
                        "options": [
                            {"label": "2BDG — Human Kallikrein 4", "value": "2BDG"},
                            {"label": "1FK9 — Mouse kallikrein 1", "value": "1FK9"},
                        ],
                    }
                ],
            ),
        ])
        inject_read_skills(assistant, {"rcsb.search": (self._klk_rcsb_skill(), self._klk_handler)})
        with patch_host_skills(create_docking):
            result = assistant.plan_turn(
                context_type="task_list",
                context_payload={
                    "page": {"contextType": "task_list", "workflowKey": "affinity"},
                    "rows": [],
                },
                user_id="u",
                username="alice",
                content="我想对接klk和布洛芬",
            )
        # The silent pick never surfaced as an action; the turn ends asking the choice —
        # and the question is grounded in the round's real observations.
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["state"], "needs_input")
        self.assertEqual(len(result["questions"]), 1)
        self.assertIn("Human Kallikrein 4", result["questions"][0]["options"][0]["label"])


if __name__ == "__main__":
    unittest.main()


class CapabilityContractAlignmentTests(unittest.TestCase):
    """Capability limits must be declared where they are enforced — found by the three-way
    audit and pinned here so they cannot silently drift again."""

    def setUp(self) -> None:
        self.descriptions = _real_database_skill_descriptions()

    def test_resolve_family_declares_identity_pinning(self) -> None:
        self.assertIn("pinned to one organism", self.descriptions["alphafold.resolve"])
        self.assertIn("pinned to one organism", self.descriptions["uniprot.resolve"])
        self.assertIn("verify both against the user's intent", self.descriptions["rcsb.resolve"])
        self.assertIn("Returns no results", self.descriptions["rcsb.resolve"])

    def test_chembl_has_no_human_default(self) -> None:
        for name, description in self.descriptions.items():
            self.assertNotIn("Defaults to Homo sapiens", description, name)
            self.assertNotIn("defaults to Homo sapiens", description, name)
        self.assertIn("unresolved choice", self.descriptions["chembl.target_activity"])
        self.assertIn("verify", self.descriptions["chembl.bioactivity"].lower())

    def test_workflow_input_contract_matches_submit_requirements(self) -> None:
        from management_api.copilot_capabilities import WORKFLOW_INPUT_CONTRACT
        affinity = WORKFLOW_INPUT_CONTRACT["affinity"]["ligand"]
        self.assertIn("mode-dependent", affinity)
        self.assertIn("pocket search box", affinity)
        vs = WORKFLOW_INPUT_CONTRACT["virtual_screening"]
        self.assertIn("200", vs["library"])
        self.assertIn("DNA/RNA are rejected", vs["target"])

    def test_affinity_mode_description_matches_submit_exemptions(self) -> None:
        from management_api.copilot_capabilities import TASK_PARAMETER_SCHEMA
        description = TASK_PARAMETER_SCHEMA["affinityMode"]["description"]
        # score is exempt from ligand files at submit — the description must say so.
        self.assertIn("score mode requires the uploaded target structure only", description)
        self.assertIn("pocket search box", description)

    def test_failure_copy_carries_no_scenario_walkthrough(self) -> None:
        # The fabricated-rowId failure message must stay generic (no docking-specific
        # "retrieve target structure and ligand" narration for non-docking requests).
        message = _no_convergence_failure_message(
            ["operations[0].arguments.taskRowId (task_001) is not a task row in the current context"],
            context_row_count=0,
            pending_held_writes=[],
            user_text="我想对接dhodh和布洛芬",
        )
        self.assertNotIn("检索靶点结构", message)
