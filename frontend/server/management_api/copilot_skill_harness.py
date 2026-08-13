from __future__ import annotations

import copy
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from management_api.copilot_skills.online_databases import OnlineDatabaseSkills


READ_EFFECTS = frozenset({"read", "observe", "resolve", "inspect"})
CONFIRMATION_EFFECTS = frozenset({"create", "update", "delete", "execute", "navigate"})
KNOWN_EFFECTS = READ_EFFECTS | CONFIRMATION_EFFECTS

# Canonical fields of a retrieved record, in identity/display priority order. This is the single
# Record-field contracts shared across consumers. The observation summarizer
# (copilot._summarize_observations) now surfaces EVERY scalar field of a record automatically, so a
# new skill's data reaches the model with no per-field registration. These lists remain for the two
# consumers that need curated anchors: grounding (_grounding_issue) and memory carry-forward
# (_compact_memory_records) use RECORD_IDENTITY_FIELDS; RECORD_LONG_FIELDS also marks the fields the
# summarizer renders in full length (SMILES / sequence) instead of capping at 80 chars.
RECORD_IDENTITY_FIELDS: Tuple[str, ...] = (
    "accession",
    "cid",
    "pdbId",
    "pmid",
    "nctId",
    "entryName",
    "geneNames",
    "proteinName",
    "title",
    "organism",
    "name",
    "chemblId",
    "target",
    "activityType",
    "units",
)
RECORD_LONG_FIELDS: Tuple[str, ...] = ("smiles", "sequence")


def _planner_goal_step_schema() -> Dict[str, Any]:
    """JSON schema for one abstract goal step in a planner outline.

    An outline step describes a high-level goal the planner will later concretize into operations
    when the harness asks it to (one step at a time). Each step is a single description; fan-out over
    a retrieved collection is the planner's responsibility (it emits one operation per element), not a
    declared harness behavior — declaring an unimplemented ``iterate`` here would let the planner
    expect fan-out the harness never performs, so it is intentionally absent.
    """
    return {
        "type": "object",
        "properties": {
            "description": {"type": "string", "minLength": 1, "maxLength": 300},
        },
        "required": ["description"],
        "additionalProperties": False,
    }


def _planner_question_schema() -> Dict[str, Any]:
    """JSON schema for one structured planner question.

    A question asks the user to resolve an ambiguity before the planner can proceed (e.g. which task
    type or which modeling backend). ``choice`` questions enumerate concrete options the frontend
    renders as clickable chips; ``confirm`` is a yes/no; ``freeform`` is open text. The planner emits
    questions WITHOUT operations and waits for the user's answer in the next turn.
    """
    return {
        "type": "object",
        "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": 400},
            "kind": {"type": "string", "enum": ["choice", "confirm", "freeform"]},
            "options": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "minLength": 1, "maxLength": 80},
                        "value": {"type": "string", "minLength": 1, "maxLength": 120},
                        "hint": {"type": "string", "maxLength": 160},
                    },
                    "required": ["label", "value"],
                    "additionalProperties": False,
                },
                "maxItems": 8,
            },
            "defaultValue": {"type": "string", "maxLength": 120},
        },
        "required": ["text", "kind"],
        "additionalProperties": False,
    }


@dataclass(frozen=True)
class CopilotSkillDefinition:
    """The contract for one atomic operation known by the planner."""

    name: str
    description: str
    input_schema: Dict[str, Any]
    effect: str
    label: str = ""
    # The host page whose catalog exposes this skill (where it may be proposed).
    context_type: str | None = None
    # The host page a confirmation of this skill navigates the user to. Read-only skills have no
    # target (they stay on the current page). Navigation/create skills declare the page that should
    # be active after the user confirms, so a multi-step plan chains across page boundaries. When
    # unset, the target defaults to the skill's own context_type (the page does not change).
    target_context: str | None = None
    payload_defaults: Dict[str, Any] = field(default_factory=dict)
    destructive: bool = False

    @property
    def read_only(self) -> bool:
        return self.effect.strip().lower() in READ_EFFECTS

    @property
    def requires_confirmation(self) -> bool:
        return not self.read_only

    @property
    def effective_target_context(self) -> str | None:
        """The page a user lands on after confirming this skill.

        Read-only skills never change the page. For confirmation skills, an explicit
        ``target_context`` wins; otherwise the skill's own ``context_type`` is the target
        (the page does not change after confirming).
        """
        if self.read_only:
            return None
        return self.target_context or self.context_type or None


@dataclass(frozen=True)
class PreparedSkillCall:
    observation_id: str
    skill: str
    arguments: Dict[str, Any]
    metadata: Dict[str, Any]
    index: int


@dataclass(frozen=True)
class PreparedOperation:
    operation_id: str
    skill: str
    arguments: Dict[str, Any]
    label: str
    description: str
    depends_on: Tuple[str, ...]
    index: int
    definition: CopilotSkillDefinition


@dataclass(frozen=True)
class PlanAudit:
    operations: Tuple[PreparedOperation, ...]
    issues: Tuple[str, ...]
    candidate: Dict[str, Any]
    state: str
    goal_steps: Tuple[Dict[str, Any], ...] = ()


class CopilotSkillHarness:
    """Audit planner output and execute only registered read-only operations.

    Intent and corrections stay with the planner. The harness reports contract
    issues instead of changing or silently dropping an operation.
    """

    def __init__(
        self,
        *,
        skills: OnlineDatabaseSkills,
        max_calls_per_round: int = 100,
        max_workers: int = 8,
    ) -> None:
        self.skills = skills
        self.max_calls_per_round = max(1, min(500, int(max_calls_per_round)))
        self.max_workers = max(1, min(16, int(max_workers)))
        self._definitions = {
            definition.name: CopilotSkillDefinition(
                name=definition.name,
                description=definition.description,
                input_schema=definition.input_schema,
                effect="read",
                label=definition.name,
            )
            for definition in skills.definitions
        }

    def definitions(
        self,
        additional: Iterable[CopilotSkillDefinition] = (),
    ) -> Dict[str, CopilotSkillDefinition]:
        result = dict(self._definitions)
        for definition in additional:
            if not definition.name.strip():
                raise ValueError("Skill name is required.")
            if definition.name in result:
                raise ValueError(f"Duplicate skill definition: {definition.name}")
            result[definition.name] = definition
        for definition in result.values():
            self._validate_definition(definition)
        return result

    @staticmethod
    def _validate_definition(definition: CopilotSkillDefinition) -> None:
        name = str(definition.name or "").strip()
        if not name:
            raise ValueError("Skill name is required.")
        effect = str(definition.effect or "").strip().lower()
        if effect not in KNOWN_EFFECTS:
            raise ValueError(f"Skill {name} declares an unsupported effect: {definition.effect}")
        if not isinstance(definition.input_schema, Mapping) or definition.input_schema.get("type") != "object":
            raise ValueError(f"Skill {name} must declare an object input schema.")
        if definition.destructive and effect in READ_EFFECTS:
            raise ValueError(f"Destructive skill {name} cannot be read-only.")
        if effect in CONFIRMATION_EFFECTS:
            if not str(definition.label or "").strip():
                raise ValueError(f"Confirmation skill {name} must declare a label.")
            if not str(definition.description or "").strip():
                raise ValueError(f"Confirmation skill {name} must declare a description.")

    @staticmethod
    def _validate_question(item: Any, path: str) -> str | None:
        """Validate one structured planner question; return an issue string or None if valid."""
        if not isinstance(item, dict):
            return f"{path} must be an object"
        text = str(item.get("text") or "").strip()
        if not text:
            return f"{path}.text is required"
        kind = str(item.get("kind") or "").strip()
        if kind not in ("choice", "confirm", "freeform"):
            return f"{path}.kind must be one of choice, confirm, freeform"
        options = item.get("options")
        if kind == "choice":
            if not isinstance(options, list) or len(options) < 2:
                return f"{path}.options must list at least two choices for a choice question"
            seen_values: set[str] = set()
            for opt_index, option in enumerate(options):
                opt_path = f"{path}.options[{opt_index}]"
                if not isinstance(option, dict):
                    return f"{opt_path} must be an object"
                opt_label = str(option.get("label") or "").strip()
                opt_value = str(option.get("value") or "").strip()
                if not opt_label or not opt_value:
                    return f"{opt_path} must declare label and value"
                if opt_value in seen_values:
                    return f"{opt_path}.value duplicates an earlier option"
                seen_values.add(opt_value)
            if len(options) > 8:
                return f"{path}.options must contain at most 8 choices"
        elif options is not None and not isinstance(options, list):
            return f"{path}.options must be an array when present"
        return None

    def planner_output_schema(
        self,
        definitions: Mapping[str, CopilotSkillDefinition],
    ) -> Dict[str, Any]:
        """Build the strict planner envelope from the authoritative skill registry."""

        operation_variants: List[Dict[str, Any]] = []
        for definition in definitions.values():
            self._validate_definition(definition)
            operation_variants.append(
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "minLength": 1, "maxLength": 128},
                        "skill": {"type": "string", "const": definition.name},
                        "arguments": copy.deepcopy(definition.input_schema),
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1, "maxLength": 128},
                            "maxItems": self.max_calls_per_round,
                        },
                    },
                    "required": ["id", "skill", "arguments", "depends_on"],
                    "additionalProperties": False,
                }
            )
        if not operation_variants:
            raise ValueError("At least one registered skill is required to build the planner contract.")
        return {
            "type": "object",
            "properties": {
                "message": {"type": "string", "minLength": 1},

                "questions": {
                    "type": "array",
                    "items": _planner_question_schema(),
                    "maxItems": 3,
                },
                "operations": {
                    "type": "array",
                    "items": {"oneOf": operation_variants},
                    "maxItems": self.max_calls_per_round,
                },
                "goal_steps": {
                    "type": "array",
                    "items": _planner_goal_step_schema(),
                    "maxItems": 20,
                    "description": "An abstract outline of the plan. Emit this for complex multi-step tasks: the harness will ask you to concretize each step one at a time. Omit for simple single-step tasks.",
                },
            },
            "required": ["message", "questions", "operations"],
            "additionalProperties": False,
        }

    def planner_output_schema_simple(
        self,
        definitions: Mapping[str, CopilotSkillDefinition],
    ) -> Dict[str, Any]:
        """A leaner schema: one operation shape with skill:enum and permissive arguments.

        Used when thinking is enabled — the oneOf with per-skill argument schemas creates too much
        grammar complexity for a model that is simultaneously reasoning, causing conformance failures.
        The harness audit still validates each operation's arguments against the real input_schema.
        """
        skill_names = sorted(definitions.keys())
        return {
            "type": "object",
            "properties": {
                "message": {"type": "string", "minLength": 1},

                "questions": {
                    "type": "array",
                    "items": _planner_question_schema(),
                    "maxItems": 3,
                },
                "operations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "minLength": 1, "maxLength": 128},
                            "skill": {"type": "string", "enum": skill_names},
                            "arguments": {"type": "object"},
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1, "maxLength": 128},
                                "maxItems": self.max_calls_per_round,
                            },
                        },
                        "required": ["id", "skill", "arguments", "depends_on"],
                        "additionalProperties": False,
                    },
                    "maxItems": self.max_calls_per_round,
                },
                "goal_steps": {
                    "type": "array",
                    "items": _planner_goal_step_schema(),
                    "maxItems": 20,
                    "description": "An abstract outline of the plan. Emit this for complex multi-step tasks: the harness will ask you to concretize each step one at a time. Omit for simple single-step tasks.",
                },
            },
            "required": ["message", "questions", "operations"],
            "additionalProperties": False,
        }

    def render_protocol_prompt(
        self,
        definitions: Mapping[str, CopilotSkillDefinition] | None = None,
    ) -> str:
        import json

        available = definitions if definitions is not None else self._definitions
        # Tool catalog in the conventional {name, description, input_schema} shape that tool-calling
        # models are trained on (OpenAI/Anthropic/SmolAgents all use this). Split into read tools
        # (the harness executes them and returns observations) and action tools (the harness surfaces
        # them as user confirmations) because that read/write split is what makes this harness
        # non-standard and must be explained to the model.
        read_tools = []
        action_tools = []
        for definition in available.values():
            tool = {
                "name": definition.name,
                "description": definition.description,
                "input_schema": definition.input_schema,
            }
            if definition.read_only:
                read_tools.append(tool)
            else:
                tool["page"] = definition.context_type or ""
                tool["advances_to"] = definition.effective_target_context or ""
                action_tools.append(tool)
        return (
            "OUTPUT FORMAT: emit a JSON object with these fields:\n"
            "  message: string — what to tell the user\n"
            "  questions: array — ask when a decision is missing (see below)\n"
            "  operations: array — each is {id, skill, arguments, depends_on}\n"
            "  goal_steps: array — for complex tasks, an outline (see below)\n\n"
            "TOOLS:\n"
            "Read tools — the harness executes them and returns results:\n"
            f"{json.dumps(read_tools, ensure_ascii=False, sort_keys=True)}\n\n"
            "Action tools — become user confirmations (not executed immediately):\n"
            f"{json.dumps(action_tools, ensure_ascii=False, sort_keys=True)}\n\n"
            "QUESTIONS — ask when the user must decide something that changes the plan:\n"
            "  {text, kind, options?} where kind=choice/confirm/freeform\n"
            "  choice: options=[{label, value}]; confirm: yes/no; freeform: open text\n\n"
            "GOAL_STEPS — for complex tasks, emit an outline first:\n"
            "  [{description}] — the harness drives step-by-step concretization\n"
            "  Each step is a single description. To fan out over a retrieved collection (one action "
            "per element), emit one operation per element when the harness asks for that step.\n\n"
            "REFERENCE — use a retrieved value in an argument instead of pasting it:\n"
            '  {"$fromObservation": "<id>", "field": "<field>", "index": 0}\n\n'
            "GUIDELINES:\n"
            "- Emit goal_steps at most once per turn: the outline is fixed once the harness accepts it, "
            "and re-emitting it is rejected\n"
            "- Never mix read tools and action tools in the same round — they are separate phases\n"
            "- Retry a failed or empty lookup with a NEW operation id; never reuse an operation id that "
            "already produced an observation — consume it via $fromObservation and depends_on instead\n"
            "- Failed or empty lookups must be retried, asked about, or reported plainly to the user — "
            "never proceed as if the data had been retrieved\n"
            "- Reference retrieved values, never paste long values into arguments\n"
            "- Never fabricate data, identifiers, or observations"
        )

    def audit_plan(
        self,
        candidate: Any,
        definitions: Mapping[str, CopilotSkillDefinition],
        *,
        observations: Mapping[str, Dict[str, Any]] | None = None,
        context_type: str = "",
        active_outline: Sequence[Dict[str, Any]] | None = None,
    ) -> PlanAudit:
        issues: List[str] = []
        if not isinstance(candidate, dict):
            return PlanAudit((), ("planner output must be an object",), {}, "")

        allowed_fields = {"message", "questions", "operations", "goal_steps"}
        issues.extend(f"planner output field is not declared: {key}" for key in candidate if key not in allowed_fields)
        if not isinstance(candidate.get("message"), str) or not str(candidate.get("message") or "").strip():
            issues.append("message must be a non-empty string")
        if "questions" not in candidate:
            issues.append("questions is required")
        questions_raw = candidate.get("questions")
        # Questions are structured objects ({text, kind, options?}). Validate each one and collect the
        # normalized list the audit uses for state decisions. A malformed question is a contract
        # violation: the planner must re-emit a well-formed question, never have a bad item silently
        # dropped.
        questions: List[Dict[str, Any]] = []
        if not isinstance(questions_raw, list):
            issues.append("questions must be an array")
        else:
            if len(questions_raw) > 3:
                issues.append("questions must contain at most 3 items")
            for index, item in enumerate(questions_raw):
                path = f"questions[{index}]"
                issue = self._validate_question(item, path)
                if issue:
                    issues.append(issue)
                elif isinstance(item, dict):
                    questions.append(item)
        raw_operations = candidate.get("operations")
        if not isinstance(raw_operations, list):
            issues.append("operations must be an array")
            state = "needs_input" if questions else "complete"
            return PlanAudit((), tuple(issues), dict(candidate), state)
        if len(raw_operations) > self.max_calls_per_round:
            issues.append(
                f"planner requested {len(raw_operations)} operations; the per-round limit is {self.max_calls_per_round}"
            )

        prepared: List[PreparedOperation] = []
        seen_ids: set[str] = set()
        prior_ids: set[str] = set()
        observation_map = observations or {}
        for index, raw in enumerate(raw_operations):
            path = f"operations[{index}]"
            if not isinstance(raw, dict):
                issues.append(f"{path} must be an object")
                continue
            unknown_fields = set(raw) - {"id", "skill", "arguments", "depends_on"}
            issues.extend(f"{path}.{key} is not declared" for key in sorted(unknown_fields))
            operation_id = str(raw.get("id") or "").strip()
            skill_name = str(raw.get("skill") or "").strip()
            if not operation_id:
                issues.append(f"{path}.id is required")
                continue
            if len(operation_id) > 128:
                issues.append(f"{path}.id is longer than the declared maximum")
                continue
            if operation_id in seen_ids:
                issues.append(f"{path}.id must be unique")
                continue
            seen_ids.add(operation_id)
            if operation_id in observation_map:
                # Re-emitting an operation id that already produced an observation is a contract
                # violation: the observation already exists, so a repeat is either redundant work
                # or an attempt to re-run failed work under the same identity. Reject it and state
                # the two legal paths — consume the observation via $fromObservation + depends_on,
                # or emit a NEW operation id for a retry. No silent skip: the planner must learn
                # the contract, not be patched around it.
                issues.append(
                    f"{path}.id already produced an observation; consume it with $fromObservation "
                    "and depends_on, or use a new operation id to retry"
                )
                continue
            definition = definitions.get(skill_name)
            if definition is None:
                issues.append(f"{path}.skill is not registered: {skill_name}")
                continue
            # Cross-context planning: a skill may target a different host page than the one the turn
            # started on (e.g. a project_list turn proposing a task_detail skill that consumes a prior
            # observation). The skill's target page is carried on the action payload as targetContextType
            # so the frontend navigates there after the user confirms; it is not an audit failure. The
            # planner's skill catalog decides availability, and depends_on/$fromObservation enforce order.
            raw_arguments = raw.get("arguments")
            if not isinstance(raw_arguments, dict):
                issues.append(f"{path}.arguments must be an object")
                continue
            arguments_with_defaults = dict(definition.payload_defaults)
            arguments_with_defaults.update(raw_arguments)
            try:
                arguments = self.materialize_observations(arguments_with_defaults, observation_map)
            except ValueError as exc:
                issues.append(f"{path}.arguments: {exc}")
                continue
            if not isinstance(arguments, dict):
                issues.append(f"{path}.arguments must resolve to an object")
                continue
            issues.extend(
                f"{path}.arguments: {error}"
                for error in self._validate_schema(arguments, definition.input_schema)
            )
            if "depends_on" not in raw:
                issues.append(f"{path}.depends_on is required")
            dependencies = raw.get("depends_on")
            if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
                issues.append(f"{path}.depends_on must be an array of strings")
                dependencies = []
            elif len(dependencies) > self.max_calls_per_round:
                issues.append(f"{path}.depends_on has more items than the declared maximum")
            elif len(dependencies) != len(set(dependencies)):
                issues.append(f"{path}.depends_on must contain unique items")
            for dependency in dependencies:
                if dependency in observation_map and not observation_map[dependency].get("ok"):
                    issues.append(f"{path}.depends_on references a failed observation: {dependency}")
                elif dependency not in prior_ids and dependency not in observation_map:
                    issues.append(f"{path}.depends_on references an unknown prior operation: {dependency}")
            prepared.append(
                PreparedOperation(
                    operation_id=operation_id,
                    skill=skill_name,
                    arguments=arguments,
                    label=str(definition.label or "").strip(),
                    description=str(definition.description or "").strip(),
                    depends_on=tuple(dependencies),
                    index=index,
                    definition=definition,
                )
            )
            prior_ids.add(operation_id)

        read_operations = [item for item in prepared if item.definition.read_only]
        write_operations = [item for item in prepared if not item.definition.read_only]
        if read_operations and write_operations:
            # Read and confirmation operations are separate phases of the loop: reads run in the
            # harness and return observations, confirmations await the user. Mixing them in one
            # round makes the phase boundary unverifiable (the planner could consume observations
            # it never received), so the mix is always rejected — with or without prior
            # observations — and the planner must separate the phases into successive rounds.
            issues.append("a turn must contain either read-only operations or confirmation operations, not both")
        if questions and raw_operations:
            issues.append("questions cannot accompany operations")
        # Validate goal_steps (the abstract outline). When present with no operations and no questions,
        # the state is "outline" — the harness will drive step-by-step concretization.
        goal_steps_raw = candidate.get("goal_steps")
        goal_steps: List[Dict[str, Any]] = []
        if active_outline is not None and goal_steps_raw:
            # The outline is the plan's declared direction. Once the harness locks it, re-emitting
            # goal_steps would let the planner drift from the approved direction mid-execution, so
            # re-emission is rejected outright — the outline is immutable for the rest of the turn.
            issues.append(
                "the plan outline is fixed; do not re-emit goal_steps — the harness drives each "
                "step and will ask for its operations one at a time"
            )
        elif goal_steps_raw is not None:
            if not isinstance(goal_steps_raw, list):
                issues.append("goal_steps must be an array")
            else:
                if len(goal_steps_raw) > 20:
                    # The declared schema caps the outline at 20 steps (maxItems); the audit must
                    # enforce the same bound — an uncapped outline would make the loop drive 21+
                    # rounds, burning the whole round budget on one plan.
                    issues.append("goal_steps must contain at most 20 items")
                for gs_index, gs_item in enumerate(goal_steps_raw):
                    if not isinstance(gs_item, dict):
                        issues.append(f"goal_steps[{gs_index}] must be an object")
                        continue
                    gs_desc = str(gs_item.get("description") or "").strip()
                    if not gs_desc:
                        issues.append(f"goal_steps[{gs_index}].description is required")
                        continue
                    if "iterate" in gs_item:
                        # The ``iterate`` declaration was removed: the harness never performed the
                        # fan-out it implied, so accepting it let the planner expect behavior that
                        # silently never happened. Reject it so the planner emits one operation per
                        # element explicitly when concretizing the step.
                        issues.append(
                            f"goal_steps[{gs_index}].iterate is not supported; fan out by emitting "
                            "one operation per element when the harness asks for this step"
                        )
                        continue
                    goal_steps.append(gs_item)
        if goal_steps and raw_operations:
            # The outline and its operations belong to separate rounds of the loop: an outline round
            # declares direction, an operations round concretizes it. Emitting both at once makes the
            # direction unverifiable (the harness could not tell whether the operations follow the
            # outline), so the round is rejected — the planner must pick one mode.
            issues.append(
                "goal_steps cannot accompany operations; emit the outline alone, then the "
                "harness will ask for each step's operations separately"
            )
        if goal_steps and questions:
            # Same separation: a question resolves an ambiguity that the outline must incorporate
            # first. Emitting both at once is rejected rather than silently preferring one.
            issues.append(
                "goal_steps cannot accompany questions; resolve the ambiguity with a question "
                "first, then emit the outline in the next round"
            )
        if questions:
            state = "needs_input"
        elif goal_steps and not prepared:
            state = "outline"
        elif read_operations:
            state = "continue"
        elif write_operations:
            state = "await_confirmation"
        else:
            state = "complete"
        # Guard against an ungrounded final answer: when the turn ends with a plain message and
        # the skills retrieved exactly one discrete record, the message must reference that record
        # (identity or SMILES/sequence). This refuses a confident-but-ungrounded answer (e.g. a
        # hallucination that ignores the retrieved compound) instead of showing it to the user.
        if state == "complete" and observations:
            grounding_issue = self._grounding_issue(candidate.get("message"), observations)
            if grounding_issue:
                issues.append(grounding_issue)
        # Context-anchored answer guard: when a turn completes with NO tools run and NO question
        # asked (a pure context-answer turn), the message must reference at least one concrete value
        # from the live context_payload — a task state, a metric number, a component type, a backend
        # name, etc. This refuses the specific failure where the model had the data but emitted a
        # title-only or empty-body message ("当前任务分析如下：" with no content). The check is purely
        # structural: it matches REAL tokens extracted from the payload, never language patterns, so
        # it cannot false-reject a legitimate answer (any answer that cites a real value passes). It
        # only fires on context-rich pages where anchors exist; an empty workspace has none.
        #
        # NOTE: The harness does NOT audit the message text. The message field is the model's
        # free-text output — its quality (length, formatting, whether it cites context values) is
        # the MODEL's responsibility, guided by the system prompt. The harness audits STRUCTURE:
        # operations schema, dependencies, read/write separation, observation correctness, and
        # grounding (anti-hallucination when a record was retrieved). Auditing message content
        # (dangling-promise colon check, context-anchor citation check) caused repeated rejections
        # that the model could not self-correct, producing "I could not complete" failures for
        # simple questions like "你会做什么". Those checks are removed — the system prompt guides
        # the model on how to answer, and the harness trusts the model's output.
        return PlanAudit(tuple(prepared), tuple(issues), dict(candidate), state, tuple(goal_steps))

    @classmethod
    def classify_observation(cls, observation: Mapping[str, Any]) -> str:
        """Classify one skill-execution observation into SUCCESS / NO_MATCH / FAILED.

        The audit of "did this unit operation really succeed" has exactly three honest outcomes:
        SUCCESS — at least one discrete record was retrieved; NO_MATCH — the source answered
        authoritatively but found nothing (an honest empty result, distinct from a failure);
        FAILED — a transport/source error, meaning the lookup could not be completed at all.
        The planner must react to NO_MATCH and FAILED (retry, ask, or report) — it may never
        silently proceed as if the data had been retrieved.
        """
        if not observation.get("ok"):
            return "FAILED"
        # A value that carries an explicit `results` key is a search-shaped payload: its records
        # live in that list (possibly empty). A value without `results` IS the discrete record
        # (resolve-shaped). Either shape yields SUCCESS only when a record with at least one
        # populated field actually exists — an empty {} value is not a record, so it must NOT be
        # reported as SUCCESS (that would tell the planner data was retrieved when it wasn't).
        for value in observation.get("values") or []:
            if not isinstance(value, dict):
                continue
            nested = value.get("results")
            if isinstance(nested, list):
                if nested:
                    return "SUCCESS"
                # Search-shaped payload with an empty results list — an authoritative empty
                # answer, never a success.
                continue
            if "results" in value:
                # Search-shaped payload whose results key is missing or not a list
                # (e.g. {"results": null}) — the source returned no list of records, so this is
                # not a retrieved record either. Do not classify it as SUCCESS.
                continue
            if value:
                return "SUCCESS"
        return "NO_MATCH"

    @staticmethod
    def _grounding_issue(message: Any, observations: Mapping[str, Dict[str, Any]]) -> str | None:
        """Return an audit issue when a final message ignores the one record a skill retrieved.

        General, record-driven (no hardcoded identifiers): enforced only when the successful
        observations contain exactly one discrete record. Multi-record list answers are left alone
        because a weaker model often summarizes a list without quoting an exact identifier, and
        rejecting those would hurt UX more than the rare list-hallucination it would catch.
        """
        import re

        normalized_message = str(message or "").strip().lower()
        if not normalized_message:
            return None
        records: List[Dict[str, Any]] = []
        for observation in (observations or {}).values():
            if not isinstance(observation, dict) or not observation.get("ok"):
                continue
            for value in observation.get("values") or []:
                if not isinstance(value, dict):
                    continue
                nested = value.get("results")
                if isinstance(nested, list) and nested:
                    records.extend(item for item in nested if isinstance(item, dict))
                else:
                    records.append(value)
        if len(records) != 1:
            return None
        record = records[0]
        anchors: List[str] = []
        for field in RECORD_IDENTITY_FIELDS:
            text = str(record.get(field) or "").strip().lower()
            if not text:
                continue
            if field == "title":
                # A free-text title (e.g. a drug name) is matched by its alphanumeric tokens, so naming
                # the compound in the answer counts as grounding without the full title verbatim.
                anchors.extend(token for token in re.findall(r"[a-z0-9]+", text) if len(token) >= 4)
            elif len(text) >= 3:
                anchors.append(text)
        for field in RECORD_LONG_FIELDS:
            text = str(record.get(field) or "").strip().lower()
            if len(text) >= 8:
                anchors.append(text[:15])
        if not anchors:
            return None
        if not any(anchor in normalized_message for anchor in anchors):
            return (
                "the answer does not reference the single record the skill retrieved; re-answer using that "
                "record's identity, SMILES, or sequence verbatim, or state plainly that you cannot resolve it"
            )
        return None

    def execute_operations(self, operations: Sequence[PreparedOperation]) -> Dict[str, Dict[str, Any]]:
        operation_ids = {operation.operation_id for operation in operations}
        pending = list(operations)
        completed_ids: set[str] = set()
        observations: Dict[str, Dict[str, Any]] = {}

        for operation in operations:
            if not operation.definition.read_only:
                raise ValueError(f"write operation cannot execute in the harness: {operation.skill}")

        while pending:
            ready = [
                operation
                for operation in pending
                if all(dependency not in operation_ids or dependency in completed_ids for dependency in operation.depends_on)
            ]
            if not ready:
                raise ValueError("read-only operation dependencies cannot be resolved")

            calls: List[PreparedSkillCall] = []
            for operation in ready:
                failed_dependencies = [
                    dependency
                    for dependency in operation.depends_on
                    if dependency in observations and not observations[dependency].get("ok")
                ]
                if failed_dependencies:
                    error = "required observation failed: " + ", ".join(failed_dependencies)
                    observations[operation.operation_id] = {
                        "skill": operation.skill,
                        "items": [
                            {
                                "index": operation.index,
                                "arguments": operation.arguments,
                                "metadata": {},
                                "ok": False,
                                "result": None,
                                "error": error,
                            }
                        ],
                        "values": [],
                        "errors": [{"index": operation.index, "error": error}],
                        "ok": False,
                        "count": 1,
                        "successCount": 0,
                    }
                    continue
                calls.append(
                    PreparedSkillCall(
                        observation_id=operation.operation_id,
                        skill=operation.skill,
                        arguments=operation.arguments,
                        metadata={},
                        index=operation.index,
                    )
                )

            observations.update(self.execute(calls))
            completed_ids.update(operation.operation_id for operation in ready)
            ready_ids = {operation.operation_id for operation in ready}
            pending = [operation for operation in pending if operation.operation_id not in ready_ids]

        return observations

    def build_confirmation_actions(
        self,
        operations: Sequence[PreparedOperation],
        *,
        plan_id: str,
        context_type: str,
        workflow_key: str = "",
    ) -> List[Dict[str, Any]]:
        actions: List[Dict[str, Any]] = []
        for operation in operations:
            if operation.definition.read_only:
                raise ValueError(f"read-only operation cannot require confirmation: {operation.skill}")
            payload = dict(operation.arguments)
            payload.update(
                {
                    "planId": plan_id,
                    "operationId": operation.operation_id,
                    "skill": operation.skill,
                    "effect": operation.definition.effect,
                    # contextType is the host page this operation belongs to (where the user confirms
                    # it); targetContextType is the page confirming it navigates to. For a cross-page
                    # plan emitted from one page, these carry each operation's own page so the frontend
                    # renders it on the right host page and advances correctly after confirmation.
                    "contextType": operation.definition.context_type or context_type,
                    "targetContextType": operation.definition.effective_target_context or context_type,
                    "workflowKey": workflow_key,
                    "destructive": operation.definition.destructive,
                    "dependsOn": list(operation.depends_on),
                }
            )
            actions.append(
                {
                    "id": operation.skill,
                    "operation_id": operation.operation_id,
                    "plan_id": plan_id,
                    "sequence": operation.index,
                    "label": operation.label,
                    "description": operation.description,
                    "arguments": dict(operation.arguments),
                    "payload": payload,
                    "effect": operation.definition.effect,
                    "needs_confirmation": True,
                    "execute_now": False,
                }
            )
        return actions

    @staticmethod
    def group_actions_by_target_context(
        actions: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Order confirmation actions so dependencies resolve before dependents, grouped by target page.

        A single plan may span multiple host pages (project_list → task_list → task_detail). The
        frontend drives the plan page by page: it renders only the actions whose targetContextType
        matches the current page, and navigates to the next page once those are confirmed. Ordering
        must respect the planner's declared dependency graph (an operation that depends_on another
        always comes after it) so the user never sees a dependent step before its prerequisite; the
        target page is the secondary key so a plan reads in navigation order within that constraint.
        """
        # Canonical progression of host pages a plan may move through. Actions whose target is not
        # recognized sort last but are still grouped together.
        page_order = {"project_list": 0, "task_list": 1, "task_detail": 2}
        unknown_order = len(page_order)

        def page_rank(action: Dict[str, Any]) -> int:
            target = str(action.get("payload", {}).get("targetContextType") or "").strip()
            return page_order.get(target, unknown_order)

        def declared_sequence(action: Dict[str, Any]) -> int:
            try:
                return int(action.get("sequence") or 0)
            except (TypeError, ValueError):
                return 0

        # Stable topological sort honoring dependsOn, with (page_rank, sequence) as the tie-breaker so
        # independent actions still read in navigation + declared order. Dependencies that reference an
        # operation id not present in this action set are treated as already satisfied (external).
        action_by_id: Dict[str, Dict[str, Any]] = {}
        for action in actions:
            op_id = str(action.get("operation_id") or "").strip()
            if op_id:
                action_by_id[op_id] = action

        remaining = list(actions)
        ordered: List[Dict[str, Any]] = []
        placed: set[str] = set()
        # Bound the loop by the action count; a cycle leaves the unplaceable tail in declared order.
        for _ in range(len(actions) + 1):
            if not remaining:
                break
            ready: List[Dict[str, Any]] = []
            deferred: List[Dict[str, Any]] = []
            for action in remaining:
                dependencies = action.get("payload", {}).get("dependsOn") or []
                op_id = str(action.get("operation_id") or "").strip()
                if (
                    isinstance(dependencies, list)
                    and all(
                        (dep not in action_by_id) or (str(dep) in placed)
                        for dep in dependencies
                        if isinstance(dep, str)
                    )
                ) or not isinstance(dependencies, list):
                    ready.append(action)
                else:
                    deferred.append(action)
            if not ready:
                # The audit guarantees dependencies reference earlier operations or existing
                # observations, so a cycle is unreachable for audited plans. If one ever appears
                # it is a harness bug — fail loudly instead of emitting an unordered tail.
                raise ValueError("confirmation action dependencies cannot be resolved (cycle)")
            ready.sort(key=lambda a: (page_rank(a), declared_sequence(a)))
            for action in ready:
                ordered.append(action)
                op_id = str(action.get("operation_id") or "").strip()
                if op_id:
                    placed.add(op_id)
            remaining = deferred

        return ordered

    def execute(self, calls: List[PreparedSkillCall]) -> Dict[str, Dict[str, Any]]:
        grouped: Dict[str, Dict[str, Any]] = {}
        if not calls:
            return grouped
        worker_count = min(self.max_workers, len(calls))
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="copilot-skill") as executor:
            future_map = {
                executor.submit(self.skills.execute, call.skill, call.arguments): call
                for call in calls
            }
            completed: List[Tuple[PreparedSkillCall, Dict[str, Any] | None, str]] = []
            for future in as_completed(future_map):
                call = future_map[future]
                try:
                    result = future.result()
                    completed.append((call, result, ""))
                except Exception as exc:
                    completed.append((call, None, str(exc)))

        for call, result, error in sorted(completed, key=lambda row: (row[0].observation_id, row[0].index)):
            observation = grouped.setdefault(
                call.observation_id,
                {"skill": call.skill, "items": [], "values": [], "errors": []},
            )
            item = {
                "index": call.index,
                "arguments": call.arguments,
                "metadata": call.metadata,
                "ok": not error,
                "result": result,
                "error": error,
            }
            observation["items"].append(item)
            if error:
                observation["errors"].append({"index": call.index, "error": error})
            elif result is not None:
                observation["values"].append({**result, **call.metadata})
        for observation in grouped.values():
            observation["ok"] = not observation["errors"]
            observation["count"] = len(observation["items"])
            observation["successCount"] = len(observation["values"])
        return grouped

    def materialize_observations(
        self,
        value: Any,
        observations: Dict[str, Dict[str, Any]],
    ) -> Any:
        if isinstance(value, list):
            return [self.materialize_observations(item, observations) for item in value]
        if not isinstance(value, dict):
            return value
        if "$fromObservation" in value:
            return self._resolve_observation_reference(value, observations)
        return {
            key: self.materialize_observations(child, observations)
            for key, child in value.items()
        }

    @staticmethod
    def _observation_records(observation: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Flatten an observation's values into its discrete entity records.

        Search skills return {results: [record, ...]}; resolve skills return the record itself.
        Flattening lets a caller index records uniformly (record 0 is the top hit).
        """
        records: List[Dict[str, Any]] = []
        for value in observation.get("values") or []:
            if not isinstance(value, dict):
                continue
            nested = value.get("results")
            if isinstance(nested, list) and nested:
                records.extend(item for item in nested if isinstance(item, dict))
            else:
                records.append(value)
        return records

    @classmethod
    def _resolve_observation_reference(cls, value: Dict[str, Any], observations: Dict[str, Dict[str, Any]]) -> Any:
        observation_id = str(value.get("$fromObservation") or "").strip()
        observation = observations.get(observation_id)
        if observation is None:
            raise ValueError(f"Unknown observation reference: {observation_id}")
        if not observation.get("ok"):
            raise ValueError(f"Observation {observation_id} contains failed skill calls")
        records = cls._observation_records(observation)
        field = value.get("field")
        if field is None:
            # No field: hand back the whole value list (legacy list-forwarding form).
            return list(observation.get("values") or [])
        if value.get("all") is True:
            # Column form: the field's value across every record (record 0 first), so a later skill
            # can consume a whole retrieved collection — e.g. compute.aggregate over a numeric column.
            column = [
                record.get(field)
                for record in records
                if isinstance(record, dict) and record.get(field) is not None
            ]
            if not column:
                raise ValueError(f"Observation {observation_id} has no records with field {field!r}")
            return column
        try:
            index = int(value.get("index", 0))
        except (TypeError, ValueError):
            # A non-numeric index is a planner contract error, not something to paper over by
            # silently reading record 0 — reject it so the audit makes the planner fix the output.
            raise ValueError(
                f"Observation {observation_id} declares an invalid record index {value.get('index')!r}"
            )
        if index < 0 or index >= len(records):
            raise ValueError(
                f"Observation {observation_id} has no record at index {index} (have {len(records)})"
            )
        record = records[index]
        extracted = record.get(field) if isinstance(record, dict) else None
        if extracted is None:
            raise ValueError(f"Observation {observation_id} record {index} has no field {field!r}")
        return extracted

    @classmethod
    def _validate_schema(cls, value: Any, schema: Mapping[str, Any], path: str = "arguments") -> List[str]:
        if not isinstance(schema, Mapping):
            return []
        errors: List[str] = []
        schema_type = schema.get("type")
        if schema_type and not cls._matches_type(value, schema_type):
            errors.append(f"{path} must be {schema_type}")
            return errors
        if "const" in schema and value != schema.get("const"):
            errors.append(f"{path} must equal the declared constant")
        enum = schema.get("enum")
        if isinstance(enum, list) and value not in enum:
            errors.append(f"{path} must be one of the declared values")
        if isinstance(value, str):
            if schema.get("minLength") is not None and len(value) < int(schema["minLength"]):
                errors.append(f"{path} is shorter than the declared minimum")
            if schema.get("maxLength") is not None and len(value) > int(schema["maxLength"]):
                errors.append(f"{path} is longer than the declared maximum")
            pattern = schema.get("pattern")
            if isinstance(pattern, str) and pattern:
                # The grammar strips `pattern` (it can't enforce regex); the harness re-validates so a
                # skill schema's pattern constraint is actually honored. JSON Schema `pattern` is
                # unanchored — a match anywhere satisfies it. An invalid pattern in the schema itself
                # is treated as no constraint rather than crashing the audit.
                try:
                    if re.search(pattern, value) is None:
                        errors.append(f"{path} does not match the declared pattern")
                except re.error:
                    pass
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            # A malformed schema (non-numeric minimum/maximum) must not crash the audit — surface it
            # as an issue instead, upholding the "audit never raises" contract.
            try:
                if schema.get("minimum") is not None and value < schema["minimum"]:
                    errors.append(f"{path} is below the declared minimum")
                if schema.get("maximum") is not None and value > schema["maximum"]:
                    errors.append(f"{path} is above the declared maximum")
            except TypeError:
                errors.append(f"{path} declares a non-numeric minimum or maximum")
        if isinstance(value, list):
            if schema.get("minItems") is not None and len(value) < int(schema["minItems"]):
                errors.append(f"{path} has fewer items than the declared minimum")
            if schema.get("maxItems") is not None and len(value) > int(schema["maxItems"]):
                errors.append(f"{path} has more items than the declared maximum")
            if schema.get("uniqueItems"):
                serialized = [repr(item) for item in value]
                if len(serialized) != len(set(serialized)):
                    errors.append(f"{path} must contain unique items")
            item_schema = schema.get("items")
            if isinstance(item_schema, Mapping):
                for index, item in enumerate(value):
                    errors.extend(cls._validate_schema(item, item_schema, f"{path}[{index}]"))
        if isinstance(value, dict):
            if schema.get("minProperties") is not None and len(value) < int(schema["minProperties"]):
                errors.append(f"{path} has fewer properties than the declared minimum")
            if schema.get("maxProperties") is not None and len(value) > int(schema["maxProperties"]):
                errors.append(f"{path} has more properties than the declared maximum")
            properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
            required = schema.get("required") if isinstance(schema.get("required"), list) else []
            for key in required:
                if key not in value:
                    errors.append(f"{path}.{key} is required")
            if schema.get("additionalProperties") is False:
                errors.extend(f"{path}.{key} is not declared" for key in value if key not in properties)
            for key, child in value.items():
                child_schema = properties.get(key)
                if isinstance(child_schema, Mapping):
                    errors.extend(cls._validate_schema(child, child_schema, f"{path}.{key}"))
        variants = schema.get("anyOf")
        if isinstance(variants, list) and variants:
            matched = any(
                not cls._validate_schema(value, variant, path)
                for variant in variants
                if isinstance(variant, Mapping)
            )
            if not matched:
                errors.append(f"{path} does not match any allowed schema")
        return errors

    @staticmethod
    def _matches_type(value: Any, schema_type: Any) -> bool:
        if schema_type is None:
            return True
        if isinstance(schema_type, list):
            return any(CopilotSkillHarness._matches_type(value, item) for item in schema_type)
        if schema_type == "object":
            return isinstance(value, dict)
        if schema_type == "array":
            return isinstance(value, list)
        if schema_type == "string":
            return isinstance(value, str)
        if schema_type == "boolean":
            return isinstance(value, bool)
        if schema_type == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if schema_type == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if schema_type == "null":
            return value is None
        return True
