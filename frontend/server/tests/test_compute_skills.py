"""Tests for the compute/derivation skill category and the observation column-reference form.

Covers: the aggregate handler (stats + non-numeric skipping + empty), the new ``$fromObservation``
``all:true`` column extraction, and an end-to-end harness plan where ``compute.aggregate`` consumes
a numeric column from a prior observation.
"""

from __future__ import annotations

import unittest
from typing import Any, Dict

from management_api.copilot_skill_harness import CopilotSkillHarness
from management_api.copilot_skills.compute_skills import aggregate, register_compute_skills
from management_api.copilot_skills.online_databases import OnlineSkillDefinition


class ComputeAggregateHandlerTests(unittest.TestCase):
    def test_summary_stats_odd_count(self) -> None:
        result = aggregate({"values": [3, 1, 2]})["results"][0]
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["sum"], 6.0)
        self.assertEqual(result["min"], 1.0)
        self.assertEqual(result["max"], 3.0)
        self.assertEqual(result["mean"], 2.0)
        self.assertEqual(result["median"], 2.0)

    def test_median_is_average_of_middle_pair_for_even_count(self) -> None:
        result = aggregate({"values": [4, 1, 3, 2]})["results"][0]
        self.assertEqual(result["median"], 2.5)

    def test_skips_non_numeric_and_reports_empty(self) -> None:
        result = aggregate({"values": ["n/a", True, None]})
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["results"], [])

    def test_rejects_nan_and_infinity(self) -> None:
        # NaN/Infinity would silently corrupt stats (nan min/max/mean); they are dropped, not counted.
        import math
        result = aggregate({"values": [math.nan, math.inf, 2, 4]})["results"][0]
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["mean"], 3.0)
        self.assertEqual(result["min"], 2.0)


class _NumericSkills:
    """A read skill returning records with a numeric ``value`` field, plus the compute skill.

    Mirrors OnlineDatabaseSkills' register/execute dispatch so register_compute_skills can target it.
    """

    def __init__(self) -> None:
        self.definitions = []
        self._handlers: dict[str, Any] = {}
        self.register(
            OnlineSkillDefinition(
                name="nums.read",
                description="numeric records",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
            self._read_nums,
        )

    def register(self, definition: OnlineSkillDefinition, handler: Any) -> None:
        self.definitions.append(definition)
        self._handlers[definition.name] = handler

    def execute(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        handler = self._handlers.get(name)
        if handler is None:
            raise ValueError(f"Unknown skill: {name}")
        return handler(arguments)

    @staticmethod
    def _read_nums(_arguments: Dict[str, Any]) -> Dict[str, Any]:
        return {"source": "nums", "results": [{"value": 1.0}, {"value": 2.0}, {"value": 3.0}, {"value": 4.0}]}


class ComputeHarnessIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skills = _NumericSkills()
        register_compute_skills(self.skills)
        self.harness = CopilotSkillHarness(skills=self.skills)
        self.definitions = self.harness.definitions([])

    def test_observation_reference_all_extracts_a_column(self) -> None:
        observations = {"o1": {"ok": True, "values": [{"results": [{"value": 10.0}, {"value": 20.0}, {"value": 30.0}]}]}}
        self.assertEqual(
            self.harness.materialize_observations({"$fromObservation": "o1", "field": "value", "all": True}, observations),
            [10.0, 20.0, 30.0],
        )

    def test_compute_consumes_a_prior_observation_numeric_column(self) -> None:
        prior = {"o1": {"ok": True, "values": [{"results": [{"value": 10.0}, {"value": 20.0}, {"value": 30.0}]}]}}
        plan = {
            "message": "Computing the mean of the retrieved values.",
            "questions": [],
            "operations": [
                {
                    "id": "agg",
                    "skill": "compute.aggregate",
                    "arguments": {"values": {"$fromObservation": "o1", "field": "value", "all": True}},
                    "depends_on": ["o1"],
                }
            ],
        }
        audit = self.harness.audit_plan(plan, self.definitions, observations=prior)
        self.assertEqual(audit.issues, ())
        # The column reference materialized into the literal numeric list before execution.
        self.assertEqual(audit.operations[0].arguments, {"values": [10.0, 20.0, 30.0]})
        observations = self.harness.execute_operations(audit.operations)
        summary = observations["agg"]["values"][0]["results"][0]
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["mean"], 20.0)
        self.assertEqual(summary["median"], 20.0)


if __name__ == "__main__":
    unittest.main()
