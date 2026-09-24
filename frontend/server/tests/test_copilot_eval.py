"""Tests for the Copilot evaluation suite.

Pins the suite as a regression gate: the archetypal registry must stay green, and the scorer must
actually detect failures (not pass trivially). The registry itself is the contract; adding a case
that regresses turns this suite red and the ``python -m management_api.copilot_eval`` CLI non-zero.
"""

from __future__ import annotations

import unittest

from management_api.copilot_eval import (
    ARCHETYPAL_CASES,
    TRAJECTORY_CASES,
    EvalCase,
    TrajectoryCase,
    build_eval_harness,
    run_eval,
    run_trajectory_eval,
    score_audit,
    score_trajectory,
)
from management_api.copilot_trace import TRACE_MODEL_REQUEST, PlannerTraceStep


class CopilotEvalRegistryTests(unittest.TestCase):
    def test_archetypal_registry_is_green(self) -> None:
        report = run_eval()
        self.assertTrue(report.all_passed)
        self.assertEqual(report.passed_count, len(ARCHETYPAL_CASES))
        self.assertEqual(len(report.results), len(ARCHETYPAL_CASES))
        self.assertGreater(len(ARCHETYPAL_CASES), 8)  # the registry covers the contract broadly

    def test_report_render_marks_each_case(self) -> None:
        report = run_eval()
        rendered = report.render()
        self.assertIn("copilot eval:", rendered)
        for result in report.results:
            self.assertIn(result.name, rendered)


class CopilotEvalScorerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness, self.definitions = build_eval_harness()

    def test_clean_audit_scored_pass_when_rejection_not_expected(self) -> None:
        candidate = {"message": "ok", "questions": [], "operations": []}
        audit = self.harness.audit_plan(candidate, self.definitions)
        result = score_audit(audit, EvalCase("ok", "clean", candidate, expect_state="complete"))
        self.assertTrue(result.passed)

    def test_dirty_audit_scored_fail_when_rejection_not_expected(self) -> None:
        # An unknown skill produces an audit issue; expecting it to pass must fail the dimension.
        candidate = {
            "message": "nope",
            "questions": [],
            "operations": [{"id": "x1", "skill": "eval.bogus", "arguments": {}, "depends_on": []}],
        }
        audit = self.harness.audit_plan(candidate, self.definitions)
        result = score_audit(audit, EvalCase("bad", "dirty", candidate, expect_rejected=False))
        self.assertFalse(result.passed)
        # ...and the same audit passes when rejection IS expected (the scorer tracks expectation, not just issues).
        accepted = score_audit(audit, EvalCase("bad-accepted", "dirty", candidate, expect_rejected=True))
        self.assertTrue(accepted.passed)

    def test_state_mismatch_fails_the_state_dimension(self) -> None:
        candidate = {"message": "awaiting", "questions": [], "operations": [
            {"id": "w1", "skill": "eval.write", "arguments": {"value": "x"}, "depends_on": []}
        ]}
        audit = self.harness.audit_plan(candidate, self.definitions)
        # Actual state is await_confirmation; expecting complete must fail.
        result = score_audit(audit, EvalCase("state", "mismatch", candidate, expect_state="complete"))
        self.assertFalse(result.passed)
        self.assertTrue(any(d.name == "state" and not d.passed for d in result.dimensions))

    def test_tool_selection_dimension_detects_wrong_skill(self) -> None:
        # The candidate selects eval.read, but the case asserts eval.write was expected.
        candidate = _candidate_with_read()
        audit = self.harness.audit_plan(candidate, self.definitions)
        result = score_audit(audit, EvalCase("ts", "wrong skill", candidate, expect_skills=("eval.write",)))
        self.assertFalse(result.passed)
        self.assertTrue(any(d.name == "tool_selection" and not d.passed for d in result.dimensions))

    def test_dimension_summary_lists_every_dimension(self) -> None:
        report = run_eval()
        summary = report.dimension_summary()
        for name in ("outcome", "state", "tool_selection", "efficiency"):
            self.assertIn(name, summary)


class CopilotTrajectoryEvalTests(unittest.TestCase):
    def test_trajectory_registry_is_green(self) -> None:
        report = run_trajectory_eval()
        self.assertTrue(report.all_passed)
        self.assertEqual(report.passed_count, len(TRAJECTORY_CASES))
        self.assertEqual(len(report.results), len(TRAJECTORY_CASES))
        self.assertGreaterEqual(len(TRAJECTORY_CASES), 3)

    def test_score_trajectory_flags_missing_terminal(self) -> None:
        # A trace with only a model request (no terminal/fallback) must fail reached_terminal when one is expected.
        steps = (PlannerTraceStep(round=0, event=TRACE_MODEL_REQUEST, detail={}),)
        case = TrajectoryCase("stuck", "no terminal", steps, expect_terminal=True)
        result = score_trajectory(case)
        self.assertFalse(result.passed)
        self.assertTrue(any(d.name == "reached_terminal" and not d.passed for d in result.dimensions))

    def test_score_trajectory_flags_round_budget_exceeded(self) -> None:
        steps = (PlannerTraceStep(round=12, event=TRACE_MODEL_REQUEST, detail={}),)
        case = TrajectoryCase("over", "exceeded budget", steps, expect_terminal=False, max_rounds=8)
        result = score_trajectory(case)
        self.assertFalse(result.passed)
        self.assertTrue(any(d.name == "round_budget" and not d.passed for d in result.dimensions))


def _candidate_with_read() -> dict:
    return {"message": "reading", "questions": [], "operations": [
        {"id": "r1", "skill": "eval.read", "arguments": {}, "depends_on": []}
    ]}


if __name__ == "__main__":
    unittest.main()
