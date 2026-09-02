"""Structured planner trace — observability for the planner + skills + harness loop.

The planner loop (one model-server call per round, harness audit, read-skill execution,
replan) is the core of the Copilot. ``PlannerTrace`` captures its trajectory as a flat,
serializable list of steps so a host UI can render the planner's "reasoning steps"
(what it searched, what the harness audited and fixed, what it proposed) and so the
server log gains a single condensation line per turn.

This mirrors the agent-trace pattern used by production agent SDKs and the OpenTelemetry
GenAI semantic conventions: domain-agnostic event names plus compact detail, never raw
payload or message bodies.

The trace is a passive recorder — it never influences planning decisions. The harness
remains the single source of truth for audit and execution; this only observes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping


# The lifecycle of one planner turn. Kept general: the same names describe any
# model-server + audit-harness loop regardless of which skills are registered.
TRACE_MODEL_REQUEST = "model_request"
TRACE_MALFORMED_OUTPUT = "malformed_output"
TRACE_AUDIT_REJECTED = "audit_rejected"
TRACE_SKILL_OBSERVATIONS = "skill_observations"
TRACE_TERMINAL = "terminal"
TRACE_NO_CONVERGENCE = "no_convergence"
# Hierarchical planning events: the planner emitted a goal_steps outline, and the harness drives
# step-by-step concretization with progress tracking.
TRACE_OUTLINE = "outline"
TRACE_STEP_DONE = "step_done"
TRACE_WRITES_MATERIALIZED = "writes_materialized"


@dataclass
class PlannerTraceStep:
    """One observable step in the planner loop."""

    round: int
    event: str
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"round": self.round, "event": self.event, "detail": dict(self.detail)}


class PlannerTrace:
    """Records the planner loop's reasoning/audit trajectory for observability.

    An optional ``on_step`` observer is invoked synchronously for each recorded step, which lets a
    host (e.g. an SSE endpoint) stream the planner's progress live as it happens. The trace itself
    stays the source of truth; the observer only mirrors what is recorded.
    """

    def __init__(self, on_step: Callable[[Dict[str, Any]], None] | None = None) -> None:
        self._steps: List[PlannerTraceStep] = []
        self._on_step = on_step

    def record(self, round_index: int, event: str, **detail: Any) -> None:
        """Append one step. ``detail`` must be JSON-serializable and compact."""
        step = PlannerTraceStep(round=int(round_index), event=str(event), detail=dict(detail))
        self._steps.append(step)
        if self._on_step is not None:
            self._on_step(step.to_dict())

    def steps(self) -> List[Dict[str, Any]]:
        return [step.to_dict() for step in self._steps]

    def summary(self) -> str:
        """One-line condensation for server logs (counts only, no payload)."""
        rounds = {step.round for step in self._steps}
        counts = {
            TRACE_SKILL_OBSERVATIONS: 0,
            TRACE_AUDIT_REJECTED: 0,
            TRACE_MALFORMED_OUTPUT: 0,
            TRACE_WRITES_MATERIALIZED: 0,
        }
        terminal_state: str | None = None
        for step in self._steps:
            if step.event in counts:
                counts[step.event] += 1
            elif step.event == TRACE_TERMINAL:
                terminal_state = step.detail.get("state") or terminal_state
        parts = [
            f"rounds={len(rounds)}",
            f"reads={counts[TRACE_SKILL_OBSERVATIONS]}",
            f"audit_rejected={counts[TRACE_AUDIT_REJECTED]}",
            f"malformed={counts[TRACE_MALFORMED_OUTPUT]}",
        ]
        if counts[TRACE_WRITES_MATERIALIZED]:
            parts.append(f"writes_materialized={counts[TRACE_WRITES_MATERIALIZED]}")
        if terminal_state:
            parts.append(f"terminal={terminal_state}")
        return "copilot_trace: " + ", ".join(parts)


def compact_observations(observations: Mapping[str, Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Reduce an observations mapping to one compact, serializable entry per observation.

    Used for the ``skill_observations`` trace detail so the step stays small while still
    showing what was looked up and whether it succeeded. Never copies record bodies.
    """
    compacted: List[Dict[str, Any]] = []
    for obs_id, observation in observations.items():
        if not isinstance(observation, Mapping):
            continue
        compacted.append(
            {
                "id": str(obs_id),
                "skill": str(observation.get("skill") or ""),
                "ok": bool(observation.get("ok")),
                "count": int(observation.get("count") or 0),
                "successCount": int(observation.get("successCount") or 0),
            }
        )
    return compacted


def compact_operations(operations: Iterable[Any]) -> List[Dict[str, Any]]:
    """Reduce prepared operations to compact ``{skill, effect}`` entries for the trace."""
    compacted: List[Dict[str, Any]] = []
    for operation in operations:
        skill = getattr(operation, "skill", None)
        effect = getattr(getattr(operation, "definition", None), "effect", None)
        if skill is None:
            continue
        compacted.append({"skill": str(skill), "effect": str(effect or "")})
    return compacted


# Map the OpenAI-compatible usage object to the OpenTelemetry GenAI semantic-convention names so
# the trace speaks the same metric vocabulary as standard agent observability tooling.
_USAGE_TOKEN_FIELDS = (
    ("prompt_tokens", "input_tokens"),
    ("completion_tokens", "output_tokens"),
    ("total_tokens", "total_tokens"),
)


def compact_usage(usage: Mapping[str, Any]) -> Dict[str, Any]:
    """Reduce a model-server usage object to its standard token metrics (empty when absent)."""
    if not isinstance(usage, Mapping):
        return {}
    compacted: Dict[str, Any] = {}
    for source, target in _USAGE_TOKEN_FIELDS:
        value = usage.get(source)
        if isinstance(value, int):
            compacted[target] = value
    return compacted
