"""Copilot evaluation suite — a scored, runnable quality gate for the planner + harness.

Production LLM systems ship an eval harness (OpenAI Evals, Inspect AI, promptfoo, LangSmith) that
codifies the agent's behavioral contract into a registry of cases run repeatedly. This module is
that artifact for the V-Bio Copilot. It mirrors the Inspect shape — *dataset* (archetypal cases) +
*scorer* (multi-dimensional, over both the final ``PlanAudit`` and the planner *trajectory*) +
*metrics* (per-dimension pass rates) — so the gate reports the industry-standard dimensions
(outcome, state, tool-selection, efficiency, loop-health) rather than a single pass/fail.

The suite evaluates the harness audit + grounding + trajectory layer (the ``harness`` in
planner+skills+harness) against invariants the hand-written unit tests assert one-at-a-time. Every
case is a *structural* planner situation — never a specific compound/protein — so the suite exercises
the contract itself rather than memorized answers. Adding a case is one registry entry; CI can run
``python -m management_api.copilot_eval`` as a gate.
"""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Tuple

from management_api.copilot_skill_harness import CopilotSkillDefinition, CopilotSkillHarness, PlanAudit
from management_api.copilot_skills.compute_skills import register_compute_skills
from management_api.copilot_skills.online_databases import OnlineSkillDefinition
from management_api.copilot_trace import (
    TRACE_AUDIT_REJECTED,
    TRACE_MALFORMED_OUTPUT,
    TRACE_MODEL_REQUEST,
    TRACE_NO_CONVERGENCE,
    TRACE_SKILL_OBSERVATIONS,
    TRACE_TERMINAL,
    PlannerTraceStep,
)


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


def _turn(message: str, operations=None, questions=None, goal_steps=None) -> Dict[str, Any]:
    turn: Dict[str, Any] = {"message": message, "questions": questions if questions is not None else [], "operations": operations or []}
    if goal_steps is not None:
        turn["goal_steps"] = goal_steps
    return turn


@dataclass(frozen=True)
class EvalCase:
    """One archetypal planner scenario scored over its final ``PlanAudit``.

    ``expect_rejected`` asserts the audit surfaces an issue; ``expect_state`` the derived turn state.
    ``expect_skills`` / ``expect_operation_count`` add the tool-selection and efficiency dimensions
    (the industry-standard agent-eval metrics) when set. ``active_outline`` simulates a locked
    goal_steps outline for the direction-immutability cases.
    """

    name: str
    description: str
    candidate: Dict[str, Any]
    observations: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    expect_state: str = ""
    expect_rejected: bool = False
    expect_skills: Tuple[str, ...] = ()
    expect_operation_count: int = -1
    active_outline: Tuple[Dict[str, Any], ...] = ()
    context_row_ids: Tuple[str, ...] = ()


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
    metrics: Tuple[Tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class EvalReport:
    results: Tuple[EvalCaseResult, ...]

    @property
    def passed_count(self) -> int:
        return sum(1 for result in self.results if result.passed)

    @property
    def all_passed(self) -> bool:
        return self.passed_count == len(self.results)

    def dimension_summary(self) -> str:
        """Per-dimension pass-rate rollup — the Inspect-style metric surface."""
        counts: Counter[str] = Counter()
        totals: Counter[str] = Counter()
        for result in self.results:
            for dim in result.dimensions:
                totals[dim.name] += 1
                if dim.passed:
                    counts[dim.name] += 1
        return ", ".join(f"{name}={counts[name]}/{totals[name]}" for name in sorted(totals))

    def render(self) -> str:
        lines = [f"copilot eval: {self.passed_count}/{len(self.results)} cases passed [{self.dimension_summary()}]"]
        for result in self.results:
            marker = "PASS" if result.passed else "FAIL"
            lines.append(f"  [{marker}] {result.name}")
            for dimension in result.dimensions:
                if not dimension.passed:
                    lines.append(f"        - {dimension.name}: {dimension.detail}")
        return "\n".join(lines)


def score_audit(audit: PlanAudit, case: EvalCase) -> EvalCaseResult:
    """Score a PlanAudit against a case's expectations across multiple dimensions.

    General — works on any audit/case pair. Each PreparedOperation already carries its own
    ``definition``, so tool-selection is derivable without a separate registry argument.
    """
    selected_skills = {op.skill for op in audit.operations}
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
    # Tool-selection accuracy: the planner picked exactly the expected skill set.
    if case.expect_skills:
        matches = selected_skills == set(case.expect_skills)
        dimensions.append(
            EvalDimension(
                "tool_selection",
                matches,
                f"selected={sorted(selected_skills)}, expected={sorted(case.expect_skills)}",
            )
        )
    # Step efficiency: the plan uses the expected number of operations (no redundant work).
    if case.expect_operation_count >= 0:
        op_count = len(audit.operations)
        dimensions.append(
            EvalDimension("efficiency", op_count == case.expect_operation_count, f"ops={op_count}, expected={case.expect_operation_count}")
        )

    metrics: Tuple[Tuple[str, Any], ...] = (
        ("ops", len(audit.operations)),
        ("skills", ",".join(sorted(selected_skills))),
        ("issues", len(audit.issues)),
        ("state", audit.state),
    )
    return EvalCaseResult(case.name, all(dim.passed for dim in dimensions), tuple(dimensions), metrics)


# ----------------------------------------------------------------------------------------------- #
# Trajectory scoring — loop-health over the planner trace (not just the final audit).
# Production agent evals score the whole path: BFCL-v3 multi-turn, Anthropic agentevals trace JSON,
# LangSmith trajectory evaluators. This is the deterministic, CI-friendly analogue.
# ----------------------------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TrajectoryCase:
    """A synthetic planner trajectory scored for loop-health invariants."""

    name: str
    description: str
    steps: Tuple[PlannerTraceStep, ...]
    expect_terminal: bool = True
    max_rounds: int = 8


def _step(round_: int, event: str, **detail: Any) -> PlannerTraceStep:
    return PlannerTraceStep(round=round_, event=event, detail=detail)


def score_trajectory(case: TrajectoryCase) -> EvalCaseResult:
    """Score loop-health invariants over a planner trajectory."""
    steps = case.steps
    rounds = [step.round for step in steps]
    events = [step.event for step in steps]
    max_seen = max(rounds) if rounds else 0

    dimensions: list[EvalDimension] = [
        EvalDimension(
            "round_budget",
            max_seen <= case.max_rounds,
            f"max_round_seen={max_seen}, budget={case.max_rounds}",
        )
    ]
    terminal_events = {TRACE_TERMINAL}
    reached_terminal = any(event in terminal_events for event in events)
    dimensions.append(
        EvalDimension(
            "reached_terminal",
            reached_terminal == case.expect_terminal,
            f"reached={reached_terminal}, expected_terminal={case.expect_terminal}",
        )
    )
    # A healthy loop must not spin on repeated rejections without progress. Allow some retries, but
    # never a number approaching the round budget.
    rejected = sum(1 for event in events if event == TRACE_AUDIT_REJECTED)
    dimensions.append(
        EvalDimension("rejection_loop", rejected < case.max_rounds, f"audit_rejected={rejected}")
    )

    metrics: Tuple[Tuple[str, Any], ...] = (
        ("steps", len(steps)),
        ("rounds", max_seen),
        ("events", ",".join(events)),
    )
    return EvalCaseResult(case.name, all(dim.passed for dim in dimensions), tuple(dimensions), metrics)


# The registry: archetypal planner behaviors. Each is a structural situation, never a specific
# compound/protein, so the suite exercises the contract itself rather than memorized answers.
ARCHETYPAL_CASES: Tuple[EvalCase, ...] = (
    # ── Round-3/4 hardening behaviors: the gate must catch their deletion. ──────────────
    EvalCase(
        "deferred_reference_same_round_read",
        "A write consuming a SAME-ROUND read via $fromObservation is deferred (pending_refs), not rejected.",
        candidate=_turn(
            "Fetch then apply.",
            [
                _read_op("rr"),
                {
                    "id": "ww",
                    "skill": _WRITE_SKILL,
                    "arguments": {"value": {"$fromObservation": "rr", "field": "value", "index": 0}},
                    "depends_on": ["rr"],
                },
            ],
        ),
        expect_state="continue",
        expect_operation_count=2,
    ),
    EvalCase(
        "reference_shorthand_string_normalizes",
        'The compact string form "$fromObservation:<id>.<field>" resolves like the object form.',
        candidate=_turn(
            "Apply via shorthand.",
            [{"id": "w9", "skill": _WRITE_SKILL, "arguments": {"value": "$fromObservation:obs1.value"}, "depends_on": []}],
        ),
        observations={"obs1": {"ok": True, "values": [{"value": "resolved"}]}},
        expect_state="await_confirmation",
        expect_operation_count=1,
    ),
    EvalCase(
        "duplicate_question_options_dedup",
        "Duplicate option values are normalized away — the question still reaches the user.",
        candidate=_turn(
            "Pick one.",
            questions=[{
                "text": "Which?",
                "kind": "choice",
                "options": [
                    {"label": "A", "value": "x"},
                    {"label": "A2", "value": "x"},
                    {"label": "B", "value": "y"},
                ],
            }],
        ),
        expect_state="needs_input",
    ),
    EvalCase(
        "questions_with_write_only_are_held",
        "A confirmation-only round carrying a question surfaces the actions; the question is "
        "held visibly on the audit (never shown with the actions, never silently dropped) and "
        "resurfaces via the post-confirmation continuation.",
        candidate=_turn(
            "Submit now?",
            [_write_op()],
            questions=[{"text": "Sure?", "kind": "confirm"}],
        ),
        expect_state="await_confirmation",
        expect_operation_count=1,
    ),
    EvalCase(
        "identical_outline_reemission_is_idempotent",
        "Re-emitting the SAME goal_steps outline is accepted (idempotent), not rejected; the "
        "outline state itself only derives on first registration, so an identical re-emission "
        "with no operations completes.",
        candidate=_turn("Outline again.", goal_steps=[{"description": "a"}]),
        active_outline=({"description": "a"},),
        expect_state="complete",
    ),
    EvalCase(
        "ungrounded_task_row_reference_rejected",
        "A taskRowId not in the visible context rows is rejected at plan time.",
        candidate=_turn(
            "Delete it.",
            [{"id": "d1", "skill": _WRITE_SKILL, "arguments": {"value": "x", "taskRowId": "ghost-row"}, "depends_on": []}],
        ),
        context_row_ids=("row-1",),
        expect_rejected=True,
    ),
    EvalCase(
        "non_english_read_query_rejected",
        "A non-English query to an English-indexed read skill is rejected with the translate remedy.",
        candidate=_turn(
            "查一下。",
            [{"id": "q1", "skill": _READ_SKILL, "arguments": {"query": "布洛芬"}, "depends_on": []}],
        ),
        expect_rejected=True,
    ),
    EvalCase(
        "read_lookup_continues",
        "A read-only operation yields the continue state for a follow-up observation round.",
        candidate=_turn("Looking up.", [_read_op()]),
        expect_state="continue",
        expect_skills=(_READ_SKILL,),
        expect_operation_count=1,
    ),
    EvalCase(
        "write_op_awaits_confirmation",
        "A non-read-only operation becomes a pending confirmation.",
        candidate=_turn("Awaiting confirmation.", [_write_op()]),
        expect_state="await_confirmation",
        expect_skills=(_WRITE_SKILL,),
        expect_operation_count=1,
    ),
    EvalCase(
        "missing_input_requests_input",
        "Unresolved questions with no operations request input.",
        candidate=_turn(
            "I need more detail.",
            questions=[{"text": "Which target?", "kind": "freeform"}],
        ),
        expect_state="needs_input",
        expect_operation_count=0,
    ),
    EvalCase(
        "empty_turn_completes",
        "A message with no operations and no questions completes.",
        candidate=_turn("Here is a general answer."),
        expect_state="complete",
        expect_operation_count=0,
    ),
    EvalCase(
        "unknown_skill_is_rejected",
        "An operation referencing an unregistered skill is rejected.",
        candidate=_turn("Nope.", [{"id": "x1", "skill": "eval.bogus", "arguments": {}, "depends_on": []}]),
        expect_rejected=True,
    ),
    EvalCase(
        "mixed_read_and_write_holds_writes",
        "A turn mixing read-only and confirmation operations executes the reads (state=continue) and holds the writes.",
        candidate=_turn("Mix.", [_read_op(), _write_op()]),
        expect_state="continue",
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
        expect_skills=(_WRITE_SKILL,),
    ),
    EvalCase(
        "pattern_constraint_accepts_matching_value",
        "A skill schema's pattern constraint accepts a matching argument.",
        candidate=_turn("Valid.", [{"id": "p1", "skill": _PATTERNED_SKILL, "arguments": {"code": "EVAL1"}, "depends_on": []}]),
        expect_state="continue",
        expect_skills=(_PATTERNED_SKILL,),
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
        expect_skills=("compute.aggregate",),
    ),
    EvalCase(
        "re_emitted_successful_read_is_idempotent",
        "Re-emitting a SUCCESSFUL read's id is idempotent: the repeat is dropped (no re-execution, "
        "no rejection — its observation stays addressable). A FAILED observation still requires "
        "the NEW-id retry path.",
        candidate=_turn("Again.", [_read_op("existing")]),
        observations={"existing": {"ok": True, "skill": _READ_SKILL,
                                   "items": [{"index": 0, "arguments": {}}],
                                   "values": [{"value": "resolved"}]}},
        expect_state="complete",
    ),
    EvalCase(
        "mixed_read_write_holds_writes_with_observations",
        "A turn mixing reads and writes executes the reads and holds the writes even when observations already exist.",
        candidate=_turn("Mix.", [_read_op(), _write_op()]),
        observations={"existing": {"ok": True, "values": [{"value": "resolved"}]}},
        expect_state="continue",
    ),
    EvalCase(
        "goal_steps_with_operations_holds_ops",
        "An outline emitted with operations registers the outline and holds the operations (state=outline).",
        candidate={
            "message": "Outlining.",
            "questions": [],
            "operations": [_read_op()],
            "goal_steps": [{"description": "Do it."}],
        },
        expect_state="outline",
    ),
    EvalCase(
        "goal_steps_with_questions_normalized",
        "Questions win over an accompanying outline: the outline is dropped (it cannot persist "
        "across turns; the next turn re-outlines with the answer) and the turn asks.",
        candidate={
            "message": "Asking.",
            "questions": [{"text": "Which one?", "kind": "freeform"}],
            "operations": [],
            "goal_steps": [{"description": "Do it."}],
        },
        expect_state="needs_input",
    ),
    EvalCase(
        "malformed_question_is_rejected",
        "A malformed question item is rejected, not silently dropped.",
        candidate=_turn("Asking.", questions=[{"text": "", "kind": "freeform"}]),
        expect_rejected=True,
    ),
    EvalCase(
        "outline_reemission_is_rejected",
        "Re-emitting goal_steps after the outline is locked is rejected (direction is immutable).",
        candidate={
            "message": "Redo.",
            "questions": [],
            "operations": [],
            "goal_steps": [{"description": "Different direction."}],
        },
        active_outline=({"description": "Locked direction."},),
        expect_rejected=True,
    ),
    EvalCase(
        "message_text_never_rejected",
        "The harness does NOT audit message text — a title-only or colon-ending message is accepted. "
        "Message quality is the model's responsibility, guided by the system prompt. The harness "
        "audits structure only.",
        candidate=_turn("当前任务分析如下："),
        expect_state="complete",
    ),
    EvalCase(
        "grounded_context_answer_accepted",
        "A message that cites a real payload value passes cleanly.",
        candidate=_turn("已完成,boltz 后端 pLDDT 85.2。"),
        expect_state="complete",
    ),
    EvalCase(
        "greeting_on_rich_context_accepted",
        "A greeting on a context-rich page is accepted — the harness does not audit message content.",
        candidate=_turn("你好！我是 V-Bio Copilot，有什么可以帮你的吗？"),
        expect_state="complete",
    ),
    EvalCase(
        "small_multirecord_ungrounded_answer_rejected",
        "With 2–3 retrieved records the final answer must name at least one of them.",
        candidate=_turn("找到了两个强效抑制剂靶点。"),
        observations={
            "find": {
                "ok": True,
                "values": [{"results": [{"target": "COX1"}, {"target": "COX2"}]}],
            }
        },
        expect_state="complete",
        expect_rejected=True,
    ),
    EvalCase(
        "small_multirecord_grounded_answer_accepted",
        "Naming one retrieved record grounds a small multi-record answer.",
        candidate=_turn("找到 COX1 与 COX2 两个靶点。"),
        observations={
            "find": {
                "ok": True,
                "values": [{"results": [{"target": "COX1"}, {"target": "COX2"}]}],
            }
        },
        expect_state="complete",
        expect_rejected=False,
    ),
    EvalCase(
        "large_record_set_answer_exempt_from_grounding",
        "A large result set may be summarized in aggregate without quoting one identifier.",
        candidate=_turn("There were several candidates."),
        observations={
            "find": {
                "ok": True,
                "values": [{"results": [{"pdbId": f"1XY{i}"} for i in range(6)]}],
            }
        },
        expect_state="complete",
        expect_rejected=False,
    ),
    EvalCase(
        "outline_intermediate_round_skips_grounding",
        "Mid-outline transition messages are harness-facing, not user answers — grounding is not "
        "enforced while an outline is active (the final message is verified at outline completion).",
        candidate=_turn("数据已返回，继续下一步。"),
        observations={"find": {"ok": True, "values": [{"results": [dict(_GROUNDED_RECORD)]}]}},
        active_outline=({"description": "Locked direction."},),
        expect_state="complete",
        expect_rejected=False,
    ),
    EvalCase(
        "duplicate_successful_call_is_rejected",
        "Repeating the exact (skill, arguments) of a succeeded lookup is redundant work — rejected.",
        candidate=_turn(
            "Again.",
            [{"id": "again", "skill": _PATTERNED_SKILL, "arguments": {"code": "EVAL1"}, "depends_on": []}],
        ),
        observations={
            "done": {
                "ok": True,
                "skill": _PATTERNED_SKILL,
                "items": [{"arguments": {"code": "EVAL1"}, "ok": True}],
                "values": [{"results": [dict(_GROUNDED_RECORD)]}],
            }
        },
        expect_rejected=True,
    ),
    EvalCase(
        "repeat_consuming_prior_observation_is_accepted",
        "The same effective arguments are fine when the operation consumes the prior observation "
        "via $fromObservation and declares depends_on it.",
        candidate=_turn(
            "Consuming.",
            [
                {
                    "id": "next",
                    "skill": _PATTERNED_SKILL,
                    "arguments": {"code": {"$fromObservation": "done", "field": "accession", "index": 0}},
                    "depends_on": ["done"],
                }
            ],
        ),
        observations={
            "done": {
                "ok": True,
                "skill": _READ_SKILL,
                "items": [{"arguments": {"code": "EVAL1"}, "ok": True}],
                "values": [{"results": [{"accession": "EVAL1", "sequence": "MTESTSEQUENCE"}]}],
            }
        },
        expect_state="continue",
    ),
    EvalCase(
        "retry_of_failed_call_is_accepted",
        "Repeating the arguments of a FAILED lookup under a new id is a legitimate retry.",
        candidate=_turn(
            "Retrying.",
            [{"id": "retry", "skill": _PATTERNED_SKILL, "arguments": {"code": "EVAL1"}, "depends_on": []}],
        ),
        observations={
            "failed": {
                "ok": False,
                "skill": _PATTERNED_SKILL,
                "items": [{"arguments": {"code": "EVAL1"}, "ok": False, "error": "HTTP 503"}],
                "values": [],
                "errors": [{"index": 0, "error": "HTTP 503"}],
            }
        },
        expect_state="continue",
    ),
    EvalCase(
        "unit_conversion_skill_is_routable",
        "compute.convert_units validates its concentration-unit contract.",
        candidate=_turn(
            "Converting.",
            [
                {
                    "id": "conv",
                    "skill": "compute.convert_units",
                    "arguments": {"value": 5.0, "from": "nM", "to": "µM"},
                    "depends_on": [],
                }
            ],
        ),
        expect_state="continue",
        expect_skills=("compute.convert_units",),
    ),
    EvalCase(
        "unit_conversion_rejects_unknown_unit",
        "compute.convert_units rejects a unit outside the declared enum.",
        candidate=_turn(
            "Converting.",
            [
                {
                    "id": "conv",
                    "skill": "compute.convert_units",
                    "arguments": {"value": 5.0, "from": "nM", "to": "mol/L"},
                    "depends_on": [],
                }
            ],
        ),
        expect_rejected=True,
    ),
    EvalCase(
        "sequence_stats_skill_is_routable",
        "compute.sequence_stats validates its sequence contract (min 10 residues).",
        candidate=_turn(
            "Analyzing.",
            [
                {
                    "id": "stats",
                    "skill": "compute.sequence_stats",
                    "arguments": {"sequence": "MTESTSEQUENCELONGER"},
                    "depends_on": [],
                }
            ],
        ),
        expect_state="continue",
        expect_skills=("compute.sequence_stats",),
    ),
    EvalCase(
        "sequence_stats_rejects_short_sequence",
        "compute.sequence_stats rejects a sequence shorter than the declared minimum.",
        candidate=_turn(
            "Analyzing.",
            [
                {
                    "id": "stats",
                    "skill": "compute.sequence_stats",
                    "arguments": {"sequence": "MSEQ"},
                    "depends_on": [],
                }
            ],
        ),
        expect_rejected=True,
    ),
)


# Trajectory registry: structural loop-health situations (no LLM needed — synthetic traces).
TRAJECTORY_CASES: Tuple[TrajectoryCase, ...] = (
    TrajectoryCase(
        "healthy_read_then_complete",
        "A healthy turn: model request → skill observation → terminal, within budget.",
        steps=(
            _step(0, TRACE_MODEL_REQUEST, input_tokens=120, output_tokens=40),
            _step(0, TRACE_SKILL_OBSERVATIONS),
            _step(1, TRACE_MODEL_REQUEST, input_tokens=200, output_tokens=60),
            _step(1, TRACE_TERMINAL, state="complete"),
        ),
    ),
    TrajectoryCase(
        "malformed_then_recovers",
        "A malformed output is retried once and the turn still reaches a terminal state.",
        steps=(
            _step(0, TRACE_MODEL_REQUEST),
            _step(0, TRACE_MALFORMED_OUTPUT),
            _step(1, TRACE_MODEL_REQUEST),
            _step(1, TRACE_TERMINAL, state="complete"),
        ),
    ),
    TrajectoryCase(
        "non_converging_repeats_end_in_honest_failure",
        "Repeated audit rejections exhaust the budget; the turn ends with an explicit failure "
        "(no_convergence + terminal state=failed), never a conversational fallback.",
        steps=(
            _step(0, TRACE_MODEL_REQUEST),
            _step(0, TRACE_AUDIT_REJECTED, issues=["x"]),
            _step(1, TRACE_MODEL_REQUEST),
            _step(1, TRACE_AUDIT_REJECTED, issues=["x"]),
            _step(2, TRACE_NO_CONVERGENCE, reason="x"),
            _step(2, TRACE_TERMINAL, state="failed"),
        ),
        max_rounds=8,
    ),
    TrajectoryCase(
        "stuck_no_terminal",
        "A pathological trace that never reaches any terminal event within budget.",
        steps=(
            _step(0, TRACE_MODEL_REQUEST),
            _step(0, TRACE_NO_CONVERGENCE),
        ),
        expect_terminal=False,
    ),
)


def run_eval() -> EvalReport:
    """Run the archetypal registry against a real harness and return the scored report."""
    harness, definitions = build_eval_harness()
    results: list[EvalCaseResult] = []
    for case in ARCHETYPAL_CASES:
        audit = harness.audit_plan(
            case.candidate,
            definitions,
            observations=case.observations,
            active_outline=case.active_outline or None,
            context_row_ids=case.context_row_ids or None,
        )
        results.append(score_audit(audit, case))
    return EvalReport(tuple(results))


def run_trajectory_eval() -> EvalReport:
    """Run the trajectory registry through the loop-health scorer."""
    return EvalReport(tuple(score_trajectory(case) for case in TRAJECTORY_CASES))


if __name__ == "__main__":
    audit_report = run_eval()
    trajectory_report = run_trajectory_eval()
    print(audit_report.render())
    print(trajectory_report.render())
    sys.exit(0 if (audit_report.all_passed and trajectory_report.all_passed) else 1)
