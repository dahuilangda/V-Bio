"""Copilot evaluation suite — a scored, runnable quality gate for the planner + harness.

Production LLM systems ship an eval harness (OpenAI Evals, Inspect AI, promptfoo, LangSmith) that
codifies the agent's behavioral contract into a registry of cases run repeatedly. This module is
that artifact for the V-Bio Copilot: a reusable scorer over ``PlanAudit``, a registry of
*archetypal* planner behaviors (not domain-specific fixtures — every case is a structural planner
situation), and a runner that produces a pass/fail report and a CLI entry point.

The suite evaluates the harness audit + grounding layer (the ``harness`` in planner+skills+harness)
against invariants the hand-written unit tests assert one-at-a-time. Adding a case is one registry
entry; CI can run ``python -m management_api.copilot_eval`` as a gate.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Tuple

from management_api.copilot_skill_harness import CopilotSkillDefinition, CopilotSkillHarness, PlanAudit
from management_api.copilot_skills.compute_skills import register_compute_skills
from management_api.copilot_skills.online_databases import OnlineSkillDefinition


# Skill names the eval harness provisions. The cases below reference only these, so the registry
# stays general (it exercises planner structure, not any real database or host action).
_READ_SKILL = "eval.read"
_PATTERNED_SKILL = "eval.patterned"
_WRITE_SKILL = "eval.write"
_GROUNDED_RECORD = {"accession": "EVAL1", "sequence": "MTESTSEQUENCE"}


class _FakeSkills:
    """Minimal read-only skills registry backing the eval harness (no network).

    Register-capable so compute skills (and any future category) mount the same way as in the live
    assistant. Provisions a plain read skill and one whose schema carries a ``pattern`` constraint,
    so the eval covers the harness's pattern re-validation.
    """

    def __init__(self) -> None:
        self.definitions: list[OnlineSkillDefinition] = []
        self._handlers: dict[str, Any] = {}
        self.register(
            OnlineSkillDefinition(
                name=_READ_SKILL,
                description="Eval read skill.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
            self._read,
        )
        self.register(
            OnlineSkillDefinition(
                name=_PATTERNED_SKILL,
                description="Eval skill with a pattern constraint on its code argument.",
                input_schema={
                    "type": "object",
                    "properties": {"code": {"type": "string", "pattern": "^EVAL[0-9]+$"}},
                    "required": ["code"],
                    "additionalProperties": False,
                },
            ),
            self._read,
        )

    def register(self, definition: OnlineSkillDefinition, handler: Any) -> None:
        self.definitions.append(definition)
        self._handlers[definition.name] = handler

    def execute(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        handler = self._handlers.get(name)
        if handler is None:
            raise ValueError(f"Unknown eval skill: {name}")
        return handler(arguments)

    @staticmethod
    def _read(_arguments: Dict[str, Any]) -> Dict[str, Any]:
        return {"source": "eval", "results": [dict(_GROUNDED_RECORD)]}


def build_eval_harness() -> Tuple[CopilotSkillHarness, Dict[str, CopilotSkillDefinition]]:
    """Construct a real CopilotSkillHarness provisioned with read, patterned-read, compute, and write skills."""
    skills = _FakeSkills()
    register_compute_skills(skills)
    harness = CopilotSkillHarness(skills=skills)
    definitions = harness.definitions(
        [
            CopilotSkillDefinition(
                name=_WRITE_SKILL,
                label="Eval write",
                description="Eval write skill.",
                input_schema={
                    "type": "object",
                    "properties": {"value": {"type": "string", "minLength": 1}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                effect="update",
            )
        ]
    )
    return harness, definitions


def _read_op(op_id: str = "r1") -> Dict[str, Any]:
    return {"id": op_id, "skill": _READ_SKILL, "arguments": {}, "depends_on": []}


def _write_op(op_id: str = "w1", arguments: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return {"id": op_id, "skill": _WRITE_SKILL, "arguments": arguments or {"value": "declared"}, "depends_on": []}


def _turn(message: str, operations=None, questions=None) -> Dict[str, Any]:
    return {"message": message, "questions": questions if questions is not None else [], "operations": operations or []}


@dataclass(frozen=True)
class EvalCase:
    """One archetypal planner scenario. ``expect_rejected`` asserts the audit surfaces an issue;
    ``expect_state`` (if set) asserts the derived turn state."""

    name: str
    description: str
    candidate: Dict[str, Any]
    observations: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    expect_state: str = ""
    expect_rejected: bool = False


@dataclass(frozen=True)
class EvalDimension:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class EvalCaseResult:
    name: str
    passed: bool
    dimensions: Tuple[EvalDimension, ...]


@dataclass(frozen=True)
class EvalReport:
    results: Tuple[EvalCaseResult, ...]

    @property
    def passed_count(self) -> int:
        return sum(1 for result in self.results if result.passed)

    @property
    def all_passed(self) -> bool:
        return self.passed_count == len(self.results)

    def render(self) -> str:
        lines = [f"copilot eval: {self.passed_count}/{len(self.results)} cases passed"]
        for result in self.results:
            marker = "PASS" if result.passed else "FAIL"
            lines.append(f"  [{marker}] {result.name}")
            for dimension in result.dimensions:
                if not dimension.passed:
                    lines.append(f"        - {dimension.name}: {dimension.detail}")
        return "\n".join(lines)


def score_audit(audit: PlanAudit, case: EvalCase) -> EvalCaseResult:
    """Score a PlanAudit against a case's expectations. General — works on any audit/case pair."""
    has_issues = bool(audit.issues)
    dimensions: list[EvalDimension] = [
        EvalDimension(
            "outcome",
            has_issues == case.expect_rejected,
            f"issues={'yes' if has_issues else 'no'}, expected_rejected={case.expect_rejected}",
        )
    ]
    if case.expect_state:
        dimensions.append(
            EvalDimension("state", audit.state == case.expect_state, f"state={audit.state}, expected={case.expect_state}")
        )
    return EvalCaseResult(case.name, all(dimension.passed for dimension in dimensions), tuple(dimensions))


# The registry: archetypal planner behaviors. Each is a structural situation, never a specific
# compound/protein, so the suite exercises the contract itself rather than memorized answers.
ARCHETYPAL_CASES: Tuple[EvalCase, ...] = (
    EvalCase(
        "read_lookup_continues",
        "A read-only operation yields the continue state for a follow-up observation round.",
        candidate=_turn("Looking up.", [_read_op()]),
        expect_state="continue",
    ),
    EvalCase(
        "write_op_awaits_confirmation",
        "A non-read-only operation becomes a pending confirmation.",
        candidate=_turn("Awaiting confirmation.", [_write_op()]),
        expect_state="await_confirmation",
    ),
    EvalCase(
        "missing_input_requests_input",
        "Unresolved questions with no operations request input.",
        candidate=_turn("I need more detail.", questions=["Which target?"]),
        expect_state="needs_input",
    ),
    EvalCase(
        "empty_turn_completes",
        "A message with no operations and no questions completes.",
        candidate=_turn("Here is a general answer."),
        expect_state="complete",
    ),
    EvalCase(
        "unknown_skill_is_rejected",
        "An operation referencing an unregistered skill is rejected.",
        candidate=_turn("Nope.", [{"id": "x1", "skill": "eval.bogus", "arguments": {}, "depends_on": []}]),
        expect_rejected=True,
    ),
    EvalCase(
        "mixed_read_and_write_rejected",
        "A turn may not mix read-only and confirmation operations.",
        candidate=_turn("Mix.", [_read_op(), _write_op()]),
        expect_rejected=True,
    ),
    EvalCase(
        "duplicate_operation_id_rejected",
        "Operation ids must be unique within a turn.",
        candidate=_turn("Dup.", [_read_op("dup"), _read_op("dup")]),
        expect_rejected=True,
    ),
    EvalCase(
        "schema_keyword_as_data_rejected",
        "Schema keywords smuggled as planner data are rejected.",
        candidate={"message": "Nope.", "questions": [], "operations": [], "additionalProperties": False},
        expect_rejected=True,
    ),
    EvalCase(
        "ungrounded_single_record_answer_rejected",
        "A final answer that ignores the single retrieved record is flagged.",
        candidate=_turn("No match here."),
        observations={"find": {"ok": True, "values": [{"results": [dict(_GROUNDED_RECORD)]}]}},
        expect_state="complete",
        expect_rejected=True,
    ),
    EvalCase(
        "grounded_single_record_answer_accepted",
        "A final answer that names the retrieved record passes.",
        candidate=_turn("Found accession EVAL1."),
        observations={"find": {"ok": True, "values": [{"results": [dict(_GROUNDED_RECORD)]}]}},
        expect_state="complete",
        expect_rejected=False,
    ),
    EvalCase(
        "from_observation_reference_resolves",
        "A write operation consuming a prior observation scalar resolves cleanly.",
        candidate=_turn(
            "Creating from the retrieved value.",
            [
                {
                    "id": "w1",
                    "skill": _WRITE_SKILL,
                    "arguments": {"value": {"$fromObservation": "find", "field": "accession", "index": 0}},
                    "depends_on": ["find"],
                }
            ],
        ),
        observations={"find": {"ok": True, "values": [{"results": [dict(_GROUNDED_RECORD)]}]}},
        expect_state="await_confirmation",
    ),
    EvalCase(
        "pattern_constraint_accepts_matching_value",
        "A skill schema's pattern constraint accepts a matching argument.",
        candidate=_turn("Valid.", [{"id": "p1", "skill": _PATTERNED_SKILL, "arguments": {"code": "EVAL1"}, "depends_on": []}]),
        expect_state="continue",
    ),
    EvalCase(
        "pattern_constraint_rejects_non_matching_value",
        "A skill schema's pattern constraint rejects a non-matching argument (the harness re-validates pattern the grammar strips).",
        candidate=_turn("Invalid.", [{"id": "p1", "skill": _PATTERNED_SKILL, "arguments": {"code": "lowercase"}, "depends_on": []}]),
        expect_rejected=True,
    ),
    EvalCase(
        "compute_consumes_a_numeric_observation_column",
        "compute.aggregate derives stats from a prior observation's numeric column via the all:true reference.",
        candidate=_turn(
            "Aggregating.",
            [
                {
                    "id": "agg",
                    "skill": "compute.aggregate",
                    "arguments": {"values": {"$fromObservation": "nums", "field": "value", "all": True}},
                    "depends_on": ["nums"],
                }
            ],
        ),
        observations={"nums": {"ok": True, "values": [{"results": [{"value": 10.0}, {"value": 20.0}, {"value": 30.0}]}]}},
        expect_state="continue",
    ),
)


def run_eval() -> EvalReport:
    """Run the full archetypal registry against a real harness and return the scored report."""
    harness, definitions = build_eval_harness()
    results: list[EvalCaseResult] = []
    for case in ARCHETYPAL_CASES:
        audit = harness.audit_plan(case.candidate, definitions, observations=case.observations)
        results.append(score_audit(audit, case))
    return EvalReport(tuple(results))


if __name__ == "__main__":
    report = run_eval()
    print(report.render())
    sys.exit(0 if report.all_passed else 1)
