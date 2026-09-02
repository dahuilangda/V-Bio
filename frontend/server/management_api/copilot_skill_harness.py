from __future__ import annotations

import copy
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from management_api.copilot_skills.online_databases import OnlineDatabaseSkills


READ_EFFECTS = frozenset({"read", "observe", "resolve", "inspect"})
CONFIRMATION_EFFECTS = frozenset({"create", "update", "delete", "execute", "navigate"})
KNOWN_EFFECTS = READ_EFFECTS | CONFIRMATION_EFFECTS

# Native tool-call markup a model may leak into the message field instead of using the
# operations array (e.g. "<tool_call>{...}</tool_call>", "<|tool_calls|>", "[TOOL_CALLS]").
# Matching is on the markup delimiters only — the JSON body inside is irrelevant.
_INLINE_TOOL_CALL_PATTERN = re.compile(
    r"<\/?\|?tool_calls?\|?>|\[/?tool_calls?\]|<function_call>|===tool_call===",
    re.IGNORECASE,
)


# The argument keys that carry free-text entity names into a lookup. The input-language audit
# checks exactly these: schema-constrained tokens (unit enums, numeric fields, sequences) are
# validated by the schema itself and may legitimately contain non-ASCII (the micro sign in µM).
_QUERY_TEXT_ARGUMENT_KEYS = frozenset({"query", "text", "identifier", "name", "organism"})


def _first_non_english_query_text(arguments: Mapping[str, Any]) -> str | None:
    """Return the first free-text query argument containing non-English characters, or None.

    "English" for query purposes means ASCII: every database this platform queries indexes ASCII
    terms (English names, gene symbols, registry numbers, SMILES, InChI). Typographic punctuation
    (em dash, curly quotes, NBSP) is stripped before the check — an English query that merely
    carries typographic marks is still English.
    """
    import unicodedata

    for key in sorted(_QUERY_TEXT_ARGUMENT_KEYS):
        value = arguments.get(key)
        if not isinstance(value, str):
            continue
        meaningful = "".join(
            char
            for char in value
            if unicodedata.category(char)[0] not in {"P", "Z"}  # drop punctuation/whitespace classes
        )
        if any(ord(char) > 127 for char in meaningful):
            return value
    return None

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
    questions WITHOUT operations and waits for the user's answer in the next turn. ``allowOther``
    (default true) marks whether the UI also offers a free-text "Other ___" answer next to the
    options — set it false only when the option set is exhaustive and exclusive.
    """
    return {
        "type": "object",
        "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": 400},
            "kind": {"type": "string", "enum": ["choice", "confirm", "freeform"]},
            "allowOther": {"type": "boolean"},
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
    # Input-language boundary for read skills: False (default) means the skill's data source is
    # English-indexed, so the audit rejects non-English query text before execution; True marks a
    # skill whose purpose is handling non-English input. Copied from OnlineSkillDefinition.
    accepts_non_english_input: bool = False

    @property
    def read_only(self) -> bool:
        return self.effect.strip().lower() in READ_EFFECTS

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
    # Observation ids this confirmation operation still references as $fromObservation forms —
    # reads from the SAME round that have not executed yet. Non-empty means `arguments` holds the
    # RAW (unmaterialized) values and schema validation is deferred until materialization.
    pending_refs: Tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanAudit:
    operations: Tuple[PreparedOperation, ...]
    issues: Tuple[str, ...]
    candidate: Dict[str, Any]
    state: str
    goal_steps: Tuple[Dict[str, Any], ...] = ()
    # Questions held back because their round ended in confirmation actions: they did not
    # reach the user this turn (the actions end it) and are neither discarded silently nor
    # shown — the post-confirmation continuation is their next window.
    held_questions: Tuple[Dict[str, Any], ...] = ()


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

    def _read_definitions(self) -> Dict[str, CopilotSkillDefinition]:
        """Derive the read-skill catalog LIVE from the registry (pi principle: the registry is
        the single source of truth — no construction-time snapshot to go stale). Registering a
        skill on the registry immediately makes it auditable and executable. A plain
        OnlineSkillDefinition is wrapped as a read; a full CopilotSkillDefinition is adopted
        verbatim so its declared effect survives."""
        catalog: Dict[str, CopilotSkillDefinition] = {}
        for definition in self.skills.definitions:
            if isinstance(definition, CopilotSkillDefinition):
                catalog[definition.name] = definition
            else:
                catalog[definition.name] = CopilotSkillDefinition(
                    name=definition.name,
                    description=definition.description,
                    input_schema=definition.input_schema,
                    effect="read",
                    label=definition.name,
                    accepts_non_english_input=getattr(definition, "accepts_non_english_input", False),
                )
        return catalog

    def definitions(
        self,
        additional: Iterable[CopilotSkillDefinition] = (),
    ) -> Dict[str, CopilotSkillDefinition]:
        result = self._read_definitions()
        for definition in additional:
            if not definition.name.strip():
                raise ValueError("Skill name is required.")
            existing = result.get(definition.name)
            if existing is not None and not (
                existing.effect == definition.effect
                and existing.input_schema == definition.input_schema
            ):
                raise ValueError(f"Duplicate skill definition: {definition.name}")
            # An identical re-declaration (registry + host page both list the same skill) is
            # idempotent: the host page's version wins.
            result[definition.name] = definition
        for definition in result.values():
            self._validate_definition(definition)
        return result

    _REF_SHORTHAND = re.compile(
        r"^\$fromObservation:([A-Za-z0-9_-]+)(?:\.([A-Za-z0-9_.\-]+))?(?:\[(\d+)\])?$"
    )

    @classmethod
    def _normalize_reference_shorthand(cls, value: Any) -> Any:
        """Rewrite the compact string reference form to the canonical object form.

        Planners NATURALLY emit `"$fromObservation:<id>.<field>"` (a common convention) — the
        intent is unambiguous, so the harness normalizes it instead of letting it pass as a
        literal string and fail schema validation (a deterministic repeat loop for mid-tier
        models). One normalization point feeding materialization, deferred refs, and schema.
        """
        if isinstance(value, str):
            match = cls._REF_SHORTHAND.match(value.strip())
            if match:
                ref_id, field, index = match.group(1), match.group(2), match.group(3)
                resolved: Dict[str, Any] = {"$fromObservation": ref_id}
                if field:
                    resolved["field"] = field
                if index is not None:
                    resolved["index"] = int(index)
                return resolved
            return value
        if isinstance(value, list):
            return [cls._normalize_reference_shorthand(item) for item in value]
        if isinstance(value, dict):
            return {key: cls._normalize_reference_shorthand(child) for key, child in value.items()}
        return value

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
        allow_other = item.get("allowOther", True)
        if not isinstance(allow_other, bool):
            return f"{path}.allowOther must be a boolean when present"
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

    @staticmethod
    def _normalize_question(question: Dict[str, Any]) -> Dict[str, Any]:
        """Drop later options whose value duplicates an earlier one (keep the first).

        A repeated option value is the SAME choice stated twice — semantically unambiguous,
        so the harness normalizes it instead of rejecting the round (pi principle: normalize
        the unambiguous, reject only the ambiguous). Mid-tier planners emit duplicate values
        deterministically, and a hard rejection sent them into a repeat loop that burned the
        round budget; dedup lets the question reach the user as intended.
        """
        options = question.get("options")
        if not isinstance(options, list):
            return question
        seen: set = set()
        deduped = []
        for option in options:
            if not isinstance(option, dict):
                continue
            value = str(option.get("value") or "").strip()
            if value in seen:
                continue
            seen.add(value)
            deduped.append(option)
        if len(deduped) != len(options):
            return {**question, "options": deduped}
        return question

    def planner_output_schema_simple(
        self,
        definitions: Mapping[str, CopilotSkillDefinition],
    ) -> Dict[str, Any]:
        """The planner envelope: one operation shape with skill:enum and permissive arguments.

        Always used: a oneOf over per-skill argument schemas makes the grammar decoder constrain
        every token against all variants, truncating the model's natural-language answer. The
        harness audit is the authoritative argument check; the grammar is a loose guide only.
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

        available = definitions if definitions is not None else self._read_definitions()
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
            "CONTRACT — three roles, one loop:\n"
            "- PLANNER (you): own the DIRECTION. When the goal needs more than one unit operation, "
            "emit goal_steps first; once the harness accepts the outline it is LOCKED. Then "
            "concretize ONE step per round from execution truth, and recover from failures instead "
            "of dropping them.\n"
            "- HARNESS: own the INVARIANT. It locks the outline, executes read skills, audits every "
            "operation BEFORE execution (declared fields, argument schema, grounding, references, "
            "repeats), reports every outcome class (SUCCESS / NO_MATCH / REJECTED / UNREACHABLE) "
            "with its correction policy, and refuses to end a turn while declared confirmation "
            "operations remain unresolved.\n"
            "- SKILLS: atomic unit operations, exposed for the current host page plus the universal "
            "read catalog. Read skills execute immediately; action skills become user confirmations "
            "applied by the HOST — application receipts (applied / failed / cancelled) return in "
            "the next turn's context, and a failed receipt is an open blocker the plan must recover "
            "from.\n\n"
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
            "  {text, kind, options?, allowOther?} where kind=choice/confirm/freeform\n"
            "  choice: options=[{label, value}] — 2 to 8 options, each something that actually exists;\n"
            "  allowOther (boolean, default true): the UI shows a free-text \"Other ___\" answer next to\n"
            "  the options — set false only when the options are exhaustive and exclusive. However you\n"
            "  set it, the user may always reply in free text: treat any answer that matches no option\n"
            "  as the user's own resolution and plan from it\n"
            "  confirm: yes/no; freeform: open text\n\n"
            "GOAL_STEPS — for complex tasks, emit an outline first:\n"
            "  [{description}] — the harness drives step-by-step concretization\n"
            "  Each step is a single description. To fan out over a retrieved collection (one action "
            "per element), emit one operation per element when the harness asks for that step.\n\n"
            "REFERENCE — use a retrieved value in an argument instead of pasting it\n"
            '  (canonical) {"$fromObservation": "<id>", "field": "<field>", "index": 0} — also\n'
            '  accepted as the shorthand string "$fromObservation:<id>.<field>[<index>]"\n\n'
            "GUIDELINES:\n"
            "- Emit goal_steps at most once per turn: the outline is fixed once the harness accepts it, "
            "and re-emitting it is rejected\n"
            "- Operation ids, depends_on, and $fromObservation references are scoped to the CURRENT "
            "TURN only: an id from an earlier turn is not addressable, even if that turn's results "
            "appear in the conversation. Earlier turns survive only as copilot_memory identity "
            "records\n"
            "- Operations emitted together with goal_steps are HELD (not executed): the outline is "
            "registered and the harness drives each step — prefer emitting the outline alone\n"
            "- Confirmation (action) operations may be emitted TOGETHER with the reads they "
            "consume: declare the dataflow with $fromObservation on the read's id plus depends_on, "
            "and the harness executes the reads first, materializes your references from their "
            "results, and surfaces the actions — one round, no re-emission. A confirmation "
            "operation that consumes NO read result is HELD: the reads run first and you re-emit "
            "it next round, informed by the observations\n"
            "- Questions may accompany READ operations: the harness runs the reads first and HOLDS "
            "your questions — re-emit them alone in the next round, informed by the observations. "
            "Questions cannot accompany confirmation (action) operations\n"
            "- Retry a failed or empty lookup with a NEW operation id; never reuse an operation id that "
            "already produced an observation — consume it via $fromObservation and depends_on instead\n"
            "- Never repeat an exact call that already SUCCEEDED — the audit rejects it; consume the "
            "existing observation via $fromObservation and depends_on\n"
            "- Failed or empty lookups must be retried, asked about, or reported plainly to the user — "
            "never proceed as if the data had been retrieved\n"
            "- Reference retrieved values, never paste long values into arguments\n"
            "- Never fabricate data, identifiers, or observations: an identifier may appear in your "
            "message only when it was retrieved this turn or was already present in the context or "
            "the user's messages"
        )

    def audit_plan(
        self,
        candidate: Any,
        definitions: Mapping[str, CopilotSkillDefinition],
        *,
        observations: Mapping[str, Dict[str, Any]] | None = None,
        context_type: str = "",
        active_outline: Sequence[Dict[str, Any]] | None = None,
        context_row_ids: Sequence[str] | None = None,
    ) -> PlanAudit:
        issues: List[str] = []
        if not isinstance(candidate, dict):
            return PlanAudit((), ("planner output must be an object",), {}, "")

        allowed_fields = {"message", "questions", "operations", "goal_steps"}
        issues.extend(f"planner output field is not declared: {key}" for key in candidate if key not in allowed_fields)
        if not isinstance(candidate.get("message"), str) or not str(candidate.get("message") or "").strip():
            issues.append("message must be a non-empty string")
        else:
            # A message that embeds native tool-call syntax is a protocol violation, not prose:
            # the model tried to invoke a tool INSIDE the text field instead of the operations
            # field. The harness would show the raw markup to the user and no lookup would ever
            # run, so the round is rejected and the planner told to re-emit the call as an
            # operation. This is structural (protocol channel misuse), not content auditing.
            message_text = str(candidate.get("message") or "")
            if _INLINE_TOOL_CALL_PATTERN.search(message_text):
                issues.append(
                    "the message embeds tool-call syntax; a tool invocation must be emitted as an "
                    "entry of the operations array (with id, skill, arguments, depends_on), never "
                    "written inside the message — re-emit with the invocation as an operation and a "
                    "message written for the user"
                )
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
                # Normalize BEFORE validating: duplicate option values are unambiguous (the
                # same choice twice) and are deduped; validation then judges the clean list.
                normalized = self._normalize_question(item) if isinstance(item, dict) else item
                issue = self._validate_question(normalized, path)
                if issue:
                    issues.append(issue)
                elif isinstance(normalized, dict):
                    questions.append(normalized)
        raw_message = str(candidate.get("message") or "").strip()
        # audit_plan is an INSTANCE method — the historical `cls.` reference here raised
        # NameError the moment a planner message started with "```" (the exact input this
        # check exists for), crashing the whole turn with a 502 instead of an audit issue.
        if raw_message.startswith('{"message"') or (
            raw_message.startswith("```")
            and CopilotSkillHarness._fenced_block_is_double_encoded(raw_message)
        ):
            # Double-encoded output: the planner put its JSON round INTO the message field.
            # The user would see raw JSON — reject with the exact correction. A fenced block
            # that is NOT a serialized planner round is a legitimate formatted answer.
            issues.append(
                "message must be the user-facing reply text, not a JSON/code block — put the "
                "reply prose in message and the operations in the operations array"
            )
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
        observation_map = observations or {}
        # Deferred materialization: a confirmation operation may reference a READ operation from
        # the SAME round via $fromObservation — the planner declares the dataflow (fetch, then
        # apply the fetched value) and the harness executes it: reads run first, then the write's
        # references are materialized from the fresh observations. Without this, the natural
        # "search → apply the result" pattern is rejected as an unknown reference and weaker
        # planners burn their round budget re-emitting it. Prescan the round to know which
        # declared ids belong to reads (references to anything else stay invalid).
        same_round_ids: set[str] = set()
        same_round_read_ids: set[str] = set()
        for raw in raw_operations:
            if not isinstance(raw, dict):
                continue
            rid = str(raw.get("id") or "").strip()
            rskill = str(raw.get("skill") or "").strip()
            if not rid:
                continue
            same_round_ids.add(rid)
            rdef = definitions.get(rskill)
            if rdef is not None and rdef.read_only:
                same_round_read_ids.add(rid)
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
                prior = observation_map.get(operation_id)
                prior_definition = definitions.get(skill_name)
                if (
                    prior_definition is not None
                    and prior_definition.read_only
                    and isinstance(prior, dict)
                    and prior.get("ok")
                    and self._same_call_as_observation(prior, skill_name, raw.get("arguments"))
                ):
                    # Idempotent re-read of a SUCCESSFUL observation under the IDENTICAL call:
                    # the result is already addressable, so drop it instead of rejecting — a
                    # repeat is the planner re-anchoring, not an error. A DIFFERENT call under
                    # the same id is NOT idempotent (it would silently serve the old answer to
                    # the new question) and falls through to the contract rejection.
                    continue
                if not (
                    prior_definition is not None
                    and prior_definition.read_only
                    and isinstance(prior, dict)
                    and not prior.get("ok")
                ):
                    # A FAILED read retried under the SAME id is a legitimate retry (the
                    # execution contract allows re-running failed calls) — it falls through
                    # and re-executes, overwriting the failed observation. Everything else
                    # (writes, unknown shapes) keeps the contract rejection.
                    issues.append(
                        f"{path}.id already produced an observation; consume it with $fromObservation "
                        "and depends_on, or retry under a NEW operation id that differs from every id "
                        "emitted this turn"
                    )
                    continue
            definition = definitions.get(skill_name)
            if definition is None:
                issues.append(f"{path}.skill is not registered: {skill_name}")
                continue
            # Cross-page action payloads: contextType names the host page the operation belongs
            # to (the frontend only renders an action on the page whose applier can execute it);
            # targetContextType names the page confirming it navigates to. Availability itself is
            # decided by the turn's skill catalog (the current page's action skills only), and
            # depends_on/$fromObservation enforce order — the audit does not re-check pages.
            raw_arguments = raw.get("arguments")
            if not isinstance(raw_arguments, dict):
                issues.append(f"{path}.arguments must be an object")
                continue
            if "depends_on" in raw_arguments:
                # Mid-tier models nest depends_on INSIDE arguments (JSON-grammar degradation).
                # The intent is unambiguous — lift it to the operation level and merge with
                # any top-level declaration (real-stack chaos: repeated verbatim to budget
                # death otherwise).
                nested = raw_arguments.pop("depends_on")
                top = raw.get("depends_on")
                merged = (list(top) if isinstance(top, list) else []) + (
                    [nested] if isinstance(nested, str)
                    else list(nested) if isinstance(nested, list)
                    else []
                )
                raw["depends_on"] = merged
            arguments_with_defaults = dict(definition.payload_defaults)
            arguments_with_defaults.update(raw_arguments)
            arguments_with_defaults = self._normalize_reference_shorthand(arguments_with_defaults)
            # Unambiguous-reference rebind (pi: normalize the unambiguous, reject only the
            # ambiguous): weaker planners invent a semantic id for the lookup they declare in
            # the same round and then reference THAT name — the intent is exactly the round's
            # read, so rebind instead of burning the budget. Rebind only when the target is
            # unique: an exact case-insensitive match to a declared read id, or the round's
            # single read. Multiple reads and no match stays rejected.
            arguments_with_defaults = self._rebind_unambiguous_refs(
                arguments_with_defaults, observation_map, same_round_read_ids
            )
            pending_refs: Tuple[str, ...] = ()
            try:
                arguments = self.materialize_observations(arguments_with_defaults, observation_map)
            except ValueError as exc:
                # An operation referencing same-round reads is a DECLARED DATAFLOW, not an
                # error: defer the reference (writes surface once materialized; reads execute
                # in dependency order with per-wave materialization in execute_operations).
                # Real-stack evidence: the natural "lookup -> compute over the result" chain
                # is ONE round — rejecting it sent weaker planners into repeat-to-death.
                referenced = tuple(sorted(self._iter_observation_refs(arguments_with_defaults)))
                unresolved = tuple(ref for ref in referenced if ref not in observation_map)
                if unresolved and all(ref in same_round_read_ids for ref in unresolved):
                    pending_refs = unresolved
                    arguments = arguments_with_defaults
                else:
                    detail = str(exc)
                    if same_round_read_ids:
                        # Make the fix mechanical: name the ids this round actually declared
                        # (a model referencing an invented id in a multi-read round cannot
                        # comply with "reference a valid id" without seeing them).
                        detail += " — reads declared this round: " + ", ".join(sorted(same_round_read_ids))
                    issues.append(f"{path}.arguments: {detail}")
                    continue
            if not isinstance(arguments, dict):
                issues.append(f"{path}.arguments must resolve to an object")
                continue
            if not pending_refs:
                issues.extend(
                    f"{path}.arguments: {error}"
                    for error in self._validate_schema(arguments, definition.input_schema)
                )
            # Task-row grounding: an action that references a task by id may only reference rows
            # the host actually shows. The frontend resolves taskRowId against its visible task
            # rows and fails the confirmation with "Could not find the task referenced by
            # Copilot" on a miss — catching the fabricated/stale id here at plan time lets the
            # planner correct itself (copy a real row id, or use the workflow's create action)
            # instead of surfacing a failed confirmation to the user.
            if context_row_ids is not None and not pending_refs:
                task_row_id = str(arguments.get("taskRowId") or "").strip()
                if task_row_id and task_row_id not in context_row_ids:
                    # Teach the boundary, not just the violation: the planner repeatedly failing
                    # here invented a row id because it did not know what the environment
                    # actually holds. Say how many rows exist, name legal ids (or that there
                    # are none — making the existing-task action impossible and the create
                    # action the only legal path).
                    if context_row_ids:
                        boundary = (
                            f"the context holds {len(context_row_ids)} task row(s); valid ids include "
                            + ", ".join(sorted(context_row_ids)[:3])
                        )
                    else:
                        boundary = (
                            "the context holds NO task rows at all — no existing-task action is legal "
                            "here; to act on a task that does not exist yet, use this workflow's "
                            "create action instead"
                        )
                    issues.append(
                        f"{path}.arguments.taskRowId ({task_row_id}) is not a task row in the "
                        "current context — an existing-task action may only reference rows from "
                        f"context_payload.rows or summary.currentTask; {boundary}"
                    )
                    continue
            # Input-language boundary (declared by the skill, enforced structurally): a read skill
            # backed by an English-indexed source must not receive non-English query text — the
            # source returns no match, and silently translating inside a query argument is a
            # hidden unrecorded conversion. The one legal path is the atomic conversion: emit
            # translate.to_english on the name first, then query with the recorded English form.
            # Checked AFTER materialization, so an argument that already consumes a translated
            # observation is English and passes. Write operations are exempt: user-facing text
            # (task names, summaries) is legitimately in any language.
            if definition.read_only and not definition.accepts_non_english_input:
                non_english = _first_non_english_query_text(arguments)
                if non_english is not None:
                    issues.append(
                        f"{path}.arguments contain non-English text ({non_english[:40]}…) but this skill's "
                        "source is English-indexed and a non-English query returns no match. Emit "
                        "translate.to_english on that name first (a read operation, so it can share this "
                        "read-only round), then query with the recorded English form consumed via "
                        '{"$fromObservation": "<translate-op-id>", "field": "english"} and depends_on it.'
                    )
                    continue
            if "depends_on" not in raw:
                issues.append(f"{path}.depends_on is required")
            dependencies = raw.get("depends_on")
            if isinstance(dependencies, list) and all(isinstance(item, str) for item in dependencies):
                # Same unambiguous rebind as $fromObservation: a depends_on entry naming the
                # invented id is rebound to the round's unambiguous read.
                dependencies = [
                    self._rebind_unambiguous_ref_id(item, observation_map, same_round_read_ids)
                    for item in dependencies
                ]
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
                elif dependency not in observation_map and dependency not in same_round_ids:
                    issues.append(
                        f"{path}.depends_on references an unknown operation: {dependency} — "
                        "operation ids are scoped to the current turn, so an id from an earlier "
                        "turn is not addressable here"
                    )
            # Redundant-work audit: an operation whose (skill, arguments) exactly repeats a call
            # that ALREADY SUCCEEDED would burn a network round (or hit the cache) to relearn what
            # the harness already holds. The planner must consume the existing observation instead.
            # A retry that only repeats a FAILED call is legitimate (the source may have recovered),
            # and an operation that declares depends_on the earlier observation is consuming it, so
            # both are exempt.
            if not self._differs_from_successful_calls(arguments, skill_name, observation_map, set(dependencies)):
                issues.append(
                    f"{path} repeats the exact call that already succeeded as an earlier observation; "
                    "consume that observation with $fromObservation and depends_on instead of calling again"
                )
                continue
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
                    pending_refs=pending_refs,
                )
            )

        # Dependency-cycle audit: a cyclic depends_on graph passes reference checks but makes
        # execute_operations raise mid-turn (misrouted as a client 400). One three-color DFS
        # over the declared graph (diamonds are legal — only a true back-edge is a cycle).
        id_to_index = {op.operation_id: op.index for op in prepared}
        graph = {op.operation_id: [d for d in op.depends_on if d in id_to_index] for op in prepared}
        color: Dict[str, int] = {node: 0 for node in graph}  # 0 unvisited, 2 done
        reported: set[str] = set()
        for start in graph:
            if color[start] != 0:
                continue
            path_stack: List[Tuple[str, int]] = [(start, 0)]
            on_path: set[str] = set()
            while path_stack:
                node, cursor = path_stack.pop()
                if cursor == 0:
                    if color[node] == 2:
                        continue
                    if node in on_path:
                        if node not in reported:
                            reported.add(node)
                            issues.append(
                                f"operations[{id_to_index[node]}].depends_on forms a dependency "
                                f"cycle through '{node}' — order the operations so each depends "
                                "only on earlier ones"
                            )
                        continue
                    on_path.add(node)
                    path_stack.append((node, 1))
                    path_stack.extend((child, 0) for child in graph[node])
                else:
                    color[node] = 2
                    on_path.discard(node)

        read_operations = [item for item in prepared if item.definition.read_only]
        write_operations = [item for item in prepared if not item.definition.read_only]
        # Read and confirmation operations emitted TOGETHER are coherent: the reads run first
        # (state=continue). A confirmation whose arguments consume a read via $fromObservation
        # (deferred, pending_refs) is materialized from the fresh observations and surfaced in
        # the SAME round by the loop; one that consumes nothing is HELD and the planner re-emits
        # it next round, informed by the observations. Nothing executes without its data or
        # confirmation path.
        if questions and write_operations and not read_operations:
            # A confirmation-only round carrying questions: the actions surface for the user's
            # confirmation and END the turn, so the questions cannot ride along — they are HELD
            # (not shown, not discarded silently): the post-confirmation continuation turn is
            # their next window, and the harness records the hold in the audit so the loop can
            # log it. A hard rejection here sent mid-tier planners into repeat-to-death in
            # real-stack runs; the hold keeps the round's real outcome (the actions).
            held_questions = tuple(questions)
            questions = []
        else:
            held_questions = ()
        # Questions alongside READ operations are coherent and accepted: the harness runs the
        # reads first (the turn has not ended), and the questions are HELD — the planner must
        # re-emit them alone in the next round, now informed by the observations. This converts
        # what used to be a hard rejection into progress; see state derivation below.
        # Validate goal_steps (the abstract outline). When present with no operations and no questions,
        # the state is "outline" — the harness will drive step-by-step concretization.
        goal_steps_raw = candidate.get("goal_steps")
        goal_steps: List[Dict[str, Any]] = []
        if active_outline is not None and goal_steps_raw:
            # The outline is the plan's declared direction and is locked once accepted. A
            # re-emission that REPEATS the locked outline is idempotent — the planner echoing its
            # own plan alongside a step's operations changes nothing, so it is ignored and the
            # operations proceed. A re-emission that DIFFERS is drift from the approved direction
            # and is rejected.
            locked = [str(item.get("description") or "").strip() for item in active_outline if isinstance(item, dict)]
            repeated_raw = goal_steps_raw if isinstance(goal_steps_raw, list) else []
            emitted = [str(item.get("description") or "").strip() for item in repeated_raw if isinstance(item, dict)]
            # Drift is judged on SEMANTIC content, not byte equality (production: the model
            # re-emitted the same four steps with whitespace/punctuation tweaks and a final
            # "。" and looped to budget death on a purely textual mismatch). Compare on
            # normalized alphanumeric skeletons (case-insensitive), which no legitimate
            # paraphrase of the SAME steps escapes while cosmetic edits pass.
            def _norm_step(text: str) -> str:
                return "".join(ch for ch in text.lower() if ch.isalnum())

            if [_norm_step(step) for step in emitted] != [_norm_step(step) for step in locked]:
                issues.append(
                    "the re-emitted goal_steps differ from the locked outline — the outline is "
                    "fixed; emit only the current step's operations"
                )
            # identical re-emission: fall through with goal_steps empty so the round's operations
            # are processed as the current step's concretization
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
        # An outline emitted TOGETHER with operations is coherent: the outline is registered as
        # the plan's direction, and the accompanying operations are NOT executed — the harness
        # drives each step and asks for its operations, starting from step 0 (so the model's
        # head-start operations are re-solicited, not lost or silently run). This converts what
        # used to be a hard rejection into a registered outline. Questions still cannot accompany
        # an outline: the answer may change the direction itself.
        if goal_steps and questions:
            # A question resolves an ambiguity the outline must incorporate first. The
            # QUESTION wins and the outline is dropped — outlines cannot persist across turns
            # anyway, so the next turn re-outlines with the answer in hand. Normalized, not
            # rejected: a hard rejection here looped weaker planners to budget death in
            # real-stack runs.
            goal_steps = []
        if questions and not read_operations:
            state = "needs_input"
        elif goal_steps:
            # The outline wins: it is registered as the plan's direction and any operations
            # emitted alongside it are held (never executed) — the harness drives each step.
            state = "outline"
        elif read_operations:
            # Reads win over held questions: the turn continues with the lookups, and the
            # questions re-emerge next round informed by the observations.
            state = "continue"
        elif write_operations:
            state = "await_confirmation"
        else:
            state = "complete"
        # Guard against an ungrounded final answer: when the turn ends with a plain message and
        # the skills retrieved a small set of discrete records, the message must reference at least
        # one of them (identity or SMILES/sequence). This refuses a confident-but-ungrounded answer
        # (e.g. a hallucination that ignores the retrieved compound) instead of showing it to the
        # user. Skipped while an outline is being concretized: an intermediate round's message is
        # harness-facing transition text ("data retrieved"), not the user-facing final answer — the
        # final answer is verified separately when the outline completes.
        if state == "complete" and observations and active_outline is None:
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
        return PlanAudit(tuple(prepared), tuple(issues), {**candidate, "questions": questions}, state, tuple(goal_steps), held_questions)

    @staticmethod
    def _call_signature(skill: str, arguments: Mapping[str, Any]) -> str:
        import json

        return json.dumps(
            {"skill": str(skill or ""), "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def _differs_from_successful_calls(
        cls,
        arguments: Mapping[str, Any],
        skill: str,
        observation_map: Mapping[str, Dict[str, Any]],
        consumed: set[str],
    ) -> bool:
        """True unless (skill, arguments) exactly repeats an already-successful call.

        (Named for what the return value MEANS — the historical ``_is_repeat_of_success``
        returned True for "not a repeat", a call-site trap for any future reader.)

        ``consumed`` holds the operation ids this operation declares in depends_on — repeating a
        call while consuming its observation via $fromObservation is the sanctioned pattern, and a
        repeat of a FAILED call is a legitimate retry, so only exact repeats of successful calls
        that are NOT declared as dependencies count as redundant.
        """
        try:
            signature = cls._call_signature(skill, arguments)
        except (TypeError, ValueError):
            return True
        for obs_id, observation in (observation_map or {}).items():
            if not isinstance(observation, dict) or not observation.get("ok") or obs_id in consumed:
                continue
            prior_skill = str(observation.get("skill") or "")
            for item in observation.get("items") or []:
                if not isinstance(item, dict) or not item.get("ok"):
                    continue
                prior_arguments = item.get("arguments")
                if not isinstance(prior_arguments, dict):
                    continue
                try:
                    if cls._call_signature(prior_skill, prior_arguments) == signature:
                        return False
                except (TypeError, ValueError):
                    continue
        return True

    @classmethod
    def final_message_issue(
        cls,
        message: Any,
        observations: Mapping[str, Dict[str, Any]],
    ) -> str | None:
        """Public grounding check for a turn's final message (used outside ``audit_plan``).

        The audit_plan grounding check only sees planner rounds; a hierarchical (outline) turn's
        user-facing message is finalized after the last step, so the planner loop calls this to
        verify the message it is about to return against everything the turn retrieved.
        """
        return cls._grounding_issue(message, observations)

    # Structured identifier patterns the audit can recognize mechanically. Each entry is
    # (label, regex). PDB ids are matched as a 4-char token starting with a digit and then
    # filtered in code to require at least one letter in the last three characters — a pure-digit
    # token (a year, a count) must never be mistaken for a structure id. SMILES are matched as a
    # long organic-character token and filtered to require a bond/ring marker (digit, paren,
    # =/#/@/[) so ordinary long words never match. Amino-acid sequences are matched as a long
    # run of standard residue letters and filtered to require sufficient length and alphabet
    # diversity so no natural word or acronym is mistaken for a sequence.
    _IDENTIFIER_PATTERNS: Tuple[Tuple[str, str], ...] = (
        ("UniProt accession", r"\b[OPQ][0-9][A-Z0-9]{3}[0-9]\b"),
        ("PDB id", r"\b[0-9][A-Z0-9]{3}\b"),
        ("PubChem CID", r"\bCID ?[0-9]{3,}\b"),
        ("ChEMBL id", r"\bCHEMBL[0-9]+\b"),
        ("ClinicalTrials id", r"\bNCT[0-9]{8}\b"),
        ("SMILES string", r"\b[CNOScnop][A-Za-z0-9\[\]\(\)@+\-/\\=#]{19,}\b"),
        ("amino-acid sequence", r"\b[ACDEFGHIKLMNPQRSTVWY]{25,}\b"),
    )

    @classmethod
    def fabricated_identifier_issue(
        cls,
        message: Any,
        allowed_text: str,
    ) -> str | None:
        """Return an audit issue when the message cites an identifier no source provided.

        The grounding check only runs when the turn RETRIEVED records, so a turn that ran no tools
        could cite a made-up accession or PDB id and pass. This check closes that hole structurally:
        every recognizable database identifier in the message must occur verbatim in the allowed
        text — everything the turn observed, the conversation so far (including the user's own
        messages), and the live context payload. An identifier that appears nowhere was fabricated
        by the model, and the message is rejected with the offending tokens named.

        ``allowed_text`` is lowercased internally — callers pass it in any case.
        """
        fabricated = list(cls.iter_fabricated_tokens(message, allowed_text))
        if not fabricated:
            return None
        quoted = ", ".join(sorted(set(fabricated))[:5])
        return (
            f"the message cites {quoted}, which was not retrieved this turn and does not appear in "
            "the context or the user's messages — never present an identifier that no source "
            "provided; re-answer without it, or retrieve the record first"
        )

    @classmethod
    def iter_fabricated_tokens(cls, message: Any, allowed_text: Any) -> List[str]:
        """Every identifier token in ``message`` that no allowed source provides, in order.

        THE single matcher for the fabrication audit — the redaction path in copilot.py consumes
        this same iterator, so the "detect" and "redact" logic can never drift apart again.

        CID matching is boundary-anchored (a fabricated ``CID 22`` must NOT pass because the
        allowed text contains ``cid 2244``), and covers the compact-JSON and ledger serialization
        forms of the allowed text in addition to prose.
        """
        text = str(message or "").strip()
        if not text:
            return []
        allowed = str(allowed_text or "").lower()
        fabricated: List[str] = []
        for label, pattern in cls._IDENTIFIER_PATTERNS:
            for match in re.finditer(pattern, text):
                token = match.group(0)
                if label == "PDB id" and not any(char.isalpha() for char in token[1:]):
                    continue  # pure digits (year, count) are not structure ids
                if label == "SMILES string":
                    # Prose like "concentration(s)-dependent" carries one parenthesis; a real
                    # SMILES token of this length carries ring digits or bond markers alongside
                    # its brackets — require BOTH a bracket/paren AND a digit or bond symbol.
                    if not (re.search(r"[\[\]\(\)]", token) and re.search(r"[0-9=#@\\]", token)):
                        continue
                if label == "amino-acid sequence":
                    # A 25+ letter run over the residue alphabet that is really a sequence uses
                    # most of the alphabet; a natural all-caps run (an acronym pileup, a
                    # keyboard mash) rarely exceeds a handful of distinct letters.
                    if len(set(token)) < 8:
                        continue
                if label == "PubChem CID":
                    cid_body = re.sub(r"[^0-9]", "", token)
                    # Boundary-anchored on the digits alone: prose ("cid 2244"), JSON
                    # ('"cid":"2244"' compact or spaced), and ledger ("cid=2244") forms all put a
                    # non-digit around the body, while a fabricated shorter CID ("CID 22") cannot
                    # match inside a longer allowed one ("2244") because the next char is a digit.
                    if re.search(rf"(?<![0-9]){re.escape(cid_body)}(?![0-9])", allowed):
                        continue
                    fabricated.append(token)
                    continue
                if token.lower() in allowed:
                    continue
                fabricated.append(token)
        return fabricated

    # ── Fabricated HOST-STATE audit ─────────────────────────────────────────────────────────
    # Closes the hole the identifier audit cannot see: a turn that invents machine state — a
    # quoted runBlockedReason that exists nowhere in the context, or a past-tense "task
    # created/submitted" claim with no applied receipt to license it. Both are the exact
    # failure where the user checks the page, sees nothing happened, and stops trusting the
    # assistant. High-precision patterns only: a miss costs nothing, a false positive burns a
    # correction round.

    # Past-tense lifecycle claims. Chinese markers bind tightly to the task/project/draft noun;
    # English uses passive/present-perfect after the noun. Future/proposal phrasing
    # (将创建 / will create / 请确认) intentionally does NOT match.
    # 已生成|已进入|已启动 are OBSERVED-state verbs (true from context on running/completed
    # tasks with no receipt needed) — absent from the pattern so a truthful context-grounded
    # answer is never replaced. The remaining verbs describe Copilot-caused transitions and
    # require an applied lifecycle receipt.
    _COMPLETION_CLAIM_PATTERNS: Tuple[re.Pattern, ...] = (
        re.compile(r"(对接任务|任务|项目|草稿)[^。！？!?\n]{0,32}?(已创建|已提交|已打开|已保存|已完成|已成功)"),
        re.compile(r"\b(task|project|draft)\b[^.!?\n]{0,48}?\b(?:has been|was|is now|have been|is)\s+(?:created|submitted|opened|saved|completed|succeeded|finished)\b", re.IGNORECASE),
    )
    # Quoted machine task-state claims: a message asserting a task's runtime state must
    # quote a state the context actually shows (production regression: "已成功完成（状态
    # SUCCESS）" shipped with zero receipts and zero tasks in the context).
    _TASK_STATE_CLAIM_PATTERN = re.compile(
        r"(?:状态|state)\s*[为是:：=]*\s*[\"'「『(]?(SUCCESS|FAILURE|QUEUED|RUNNING|REVOKED|DRAFT)",
        re.IGNORECASE,
    )
    # Claims about what the user did in an EARLIER turn are licensed by history, not by this
    # turn's receipts — the pre-noun window exemption keeps them out of the audit. "刚才" is
    # the most common Chinese past-reference word in recovery turns ("刚才的操作失败了") and
    # was missing from the original set, flagging honest history narration as fabrication.
    _PRIOR_TURN_MARKERS = ("之前", "先前", "上次", "刚才", "earlier", "already")
    # An applied receipt from one of these skills is what licenses a lifecycle completion claim.
    # A pending confirmation is NOT done — only status=applied receipts count.
    # tasks:open navigates (the host confirms the open); task_detail:save_draft persists the
    # draft — both are receipt-licensed lifecycle transitions.
    _LIFECYCLE_SKILL_PREFIXES: Tuple[str, ...] = (
        "tasks:create", "task_detail:create", "task_detail:submit", "projects:create",
        "tasks:open", "task_detail:save",
    )
    _STATE_FIELD_MARKERS = ("runBlockedReason", "runDisabledReason")

    @staticmethod
    def _normalized_state_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").lower()).strip()

    @staticmethod
    def _verbatim_fragments(value: str, *, size: int = 16) -> set:
        """Contiguous chunks of a normalized state value.

        Machine values are long error strings ('Failed to submit affinity scoring (403):
        {"error":"project_id is required"}'); a reply that quotes only the decisive tail
        ('project_id is required') is QUOTING, not paraphrasing, yet used to fail the
        full-value window check. Any ≥16-char contiguous run of the actual value is
        evidence of verbatim quotation — a paraphrase or an invented value shares no
        such run. All positions, not a sampled grid: a sampled grid can straddle the
        quoted span and miss it.
        """
        text = str(value or "")
        if len(text) < size:
            return {text} if text else set()
        return {text[i : i + size] for i in range(len(text) - size + 1)}

    @classmethod
    def _context_state_values(cls, value: Any, key: str) -> List[str]:
        """Every string stored under ``key`` anywhere in the context payload, normalized."""
        found: List[str] = []
        if isinstance(value, Mapping):
            for item_key, item in value.items():
                if str(item_key) == key and isinstance(item, str) and item.strip():
                    found.append(cls._normalized_state_text(item))
                found.extend(cls._context_state_values(item, key))
        elif isinstance(value, (list, tuple)):
            for item in value:
                found.extend(cls._context_state_values(item, key))
        return found

    @staticmethod
    def _fenced_block_is_double_encoded(raw_message: str) -> bool:
        body = raw_message.strip().strip("`")
        first_line, _, rest = body.partition("\n")
        candidate = rest if first_line.strip().startswith(("json", "JSON")) else body
        candidate = candidate.strip()
        if not candidate.startswith("{"):
            return False
        try:
            parsed = json.loads(candidate)
        except ValueError:
            return False
        return isinstance(parsed, dict) and ("message" in parsed or "operations" in parsed)

    @staticmethod
    def _same_call_as_observation(observation: Mapping[str, Any], skill: str, raw_arguments: Any) -> bool:
        """True when re-emitting ``skill``+``raw_arguments`` repeats EXACTLY the call that
        produced ``observation`` (skill match + argument-signature match). Anything else is a
        different query masquerading under a used id and must not be dropped."""
        if str(observation.get("skill") or "") != str(skill or ""):
            return False
        items = observation.get("items")
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            return False
        prior_args = items[0].get("arguments")
        if not isinstance(prior_args, dict) or not isinstance(raw_arguments, dict):
            return False
        return dict(prior_args) == dict(raw_arguments)

    @classmethod
    def _rebind_unambiguous_refs(
        cls,
        value: Any,
        observation_map: Mapping[str, Any],
        read_ids: set,
    ) -> Any:
        """Rewrite $fromObservation ids that point at nothing to the round's unambiguous read.

        Only UNKNOWN ids are candidates (a live observation id or a correctly-declared read id
        is never touched). Resolution order: exact case-insensitive match against a declared
        read id; else, when the round declared exactly one read, that read. Anything else is
        left for the audit to reject with its precise feedback.
        """
        if not read_ids:
            return value
        lowered = {str(rid).lower(): str(rid) for rid in read_ids}
        single = next(iter(read_ids)) if len(read_ids) == 1 else None

        def resolve(raw_id: str) -> Optional[str]:
            if raw_id in observation_map or raw_id in read_ids:
                return None
            target = lowered.get(raw_id.lower())
            if target is None:
                target = single
            return target

        def walk(node: Any) -> Any:
            if isinstance(node, dict):
                raw_id = node.get("$fromObservation")
                if isinstance(raw_id, str) and raw_id:
                    target = resolve(raw_id)
                    if target is not None and target != raw_id:
                        return {**node, "$fromObservation": target}
                return {key: walk(child) for key, child in node.items()}
            if isinstance(node, list):
                return [walk(item) for item in node]
            return node

        return walk(value)

    @classmethod
    def _rebind_unambiguous_ref_id(
        cls,
        raw_id: str,
        observation_map: Mapping[str, Any],
        read_ids: set,
    ) -> str:
        """Single-id form of the rebind (depends_on entries): returns the rebound id or the
        original when the round does not identify a unique target."""
        if not read_ids or raw_id in observation_map or raw_id in read_ids:
            return raw_id
        lowered = {str(rid).lower(): str(rid) for rid in read_ids}
        target = lowered.get(raw_id.lower())
        if target is None and len(read_ids) == 1:
            target = next(iter(read_ids))
        return target if target is not None else raw_id

    @classmethod
    def question_fabrication_issue(
        cls,
        *,
        questions_text: Any,
        allowed_text: str,
    ) -> Optional[str]:
        """Return an audit issue when a QUESTION cites a machine value no source provided.

        The message fabrication audit closes the message channel; without this, the question
        channel is the bypass: a planner that skipped its lookups asks the user to CONFIRM a
        value it wrote from memory (a plausible-but-wrong SMILES, a made-up accession) — a
        yes/no answer then launders the fabrication into an applied operation. A question may
        only present values the registered sources actually returned this conversation.
        """
        tokens = list(cls.iter_fabricated_tokens(questions_text, allowed_text))
        if not tokens:
            return None
        quoted = ", ".join(sorted(set(tokens))[:5])
        return (
            f"the questions cite {quoted}, which was not retrieved this turn and appears in "
            "neither the context nor the user's messages — a question may only present values "
            "the registered sources returned; never ask the user to confirm a value from "
            "memory. Retrieve it with the matching lookup skill first, then ask a choice "
            "question over the retrieved candidates (or apply it directly when unique)."
        )

    # A choice over ENTITIES is only meaningful over retrieved candidates. Firing needs ALL
    # of: an object-class noun (record / entry / candidate), a selector phrase, and at least
    # one option that LOOKS like an entity identity (identifier pattern or a long slug) —
    # preference questions ("experimental or predicted model?") never satisfy all three.
    _ENTITY_CHOICE_MARKER = re.compile(r"(条目|记录|候选|entries?|records?|candidates?)", re.IGNORECASE)
    _ENTITY_CHOICE_SELECTOR = re.compile(r"(选择|挑选|哪[一条个]|pick|choose|which one)", re.IGNORECASE)

    @classmethod
    def vacuous_entity_choice_issue(
        cls,
        *,
        questions: Any,
        observation_count: int,
    ) -> Optional[str]:
        """Return an audit issue when an ENTITY choice question runs with zero retrievals.

        Production shape: the planner asks "please choose the structure entry to use" before
        running any lookup — the user is asked to pick among candidates that were never
        retrieved (options cite nothing, so the value-grounding audit cannot see them). Run
        the lookup first, then ask over the real candidates; preference questions are
        unaffected (they never reference entries/records/candidates).
        """
        if observation_count > 0:
            return None
        for question in questions if isinstance(questions, list) else []:
            if not isinstance(question, Mapping):
                continue
            if str(question.get("kind") or "") != "choice":
                continue
            text = str(question.get("text") or "")
            options = question.get("options")
            entity_like_option = any(
                isinstance(opt, dict)
                and (
                    cls._IDENTIFIER_PATTERNS and any(
                        re.search(pattern, str(opt.get("value") or "") + " " + str(opt.get("label") or ""))
                        for _, pattern in cls._IDENTIFIER_PATTERNS
                    )
                    or len(str(opt.get("value") or "")) >= 8
                )
                for opt in (options if isinstance(options, list) else [])
            )
            if (
                cls._ENTITY_CHOICE_MARKER.search(text)
                and cls._ENTITY_CHOICE_SELECTOR.search(text)
                and entity_like_option
            ):
                return (
                    "the choice question asks the user to pick among entries/records, but this "
                    "turn retrieved none — run the matching lookup first and ask the choice "
                    "over the retrieved candidates; a preference question needs no retrieval, "
                    "an entity choice needs real candidates"
                )
        return None

    # --- numeric-claim grounding -------------------------------------------------
    # Counts the message may assert about context data, mapped to the noun families the
    # context declares counts for. A claim that contradicts a declared count is fabrication
    # ("3 candidates" when the context declares 365); a claim about a family the context
    # declares nothing for stays allowed — an unverifiable number is not a contradicted one.
    # Chinese count claims REQUIRE the 个/条 counter — a bare "候选分子 1 预测值" is an
    # enumeration index, not a quantity, and must not be audited as a count claim.
    _COUNT_QUALIFIERS = ("active", "failed", "failures", "running", "success", "succeeded", "draft", "pending", "queued", "visible", "matched", "all", "total")
    _COUNT_QUALIFIER_ZH_EN = {
        "活跃": "active", "失败": "failed", "运行中": "running", "运行": "running",
        "成功": "success", "草稿": "draft", "排队": "queued", "已完成": "success",
    }
    _COUNT_CLAIM_PATTERN = re.compile(
        r"(?P<num>\d+(?:\.\d+)?)\s*[个条]\s*(?P<qualifier>活跃|失败|运行中|运行|成功|草稿|排队)?\s*(?P<noun>候选分子|候选|分子|化合物|预测|变换|任务|项目)\s*(?:处于)?\s*(?P<post_qualifier>活跃|失败|运行中|成功|草稿|排队)?"
        r"|(?P<qualifier2>active|failed|running|success|draft|pending|queued|visible|matched)\s+"
        r"(?P<num2>\d+(?:\.\d+)?)\s+(?P<noun2>candidates?|molecules?|compounds?|predictions?|transforms?|tasks?|projects?)"
        r"|(?P<num3>\d+(?:\.\d+)?)\s+(?P<noun3>candidates?|molecules?|compounds?|predictions?|transforms?|tasks?|projects?)",
        re.IGNORECASE,
    )
    _COMPLETED_COUNT_PATTERN = re.compile(
        r"(?:已完成|已成功|completed|succeeded)\s*(?P<num>\d+(?:\.\d+)?)\s*(?:个|条|of|/)?",
        re.IGNORECASE,
    )
    _METRIC_UNITS = r"(?:(?:kcal|kj)\s*/\s*mol|kcal/mol|[µμ]m|\bnm\b|\bmm\b|plddt|iptm|ipsae|\bpae\b|å|\bda\b|g/mol)"
    _METRIC_CLAIM_PATTERN = re.compile(
        rf"(?P<num>[−-]?\d+(?:\.\d+)?)\s*{_METRIC_UNITS}"
        rf"|{_METRIC_UNITS}\s*[为:：=是]?\s*(?P<num2>[−-]?\d+(?:\.\d+)?)",
        re.IGNORECASE,
    )
    _COUNT_NOUN_FAMILIES: Dict[str, str] = {
        "候选": "candidates", "候选分子": "candidates", "分子": "candidates", "化合物": "candidates",
        "candidate": "candidates", "candidates": "candidates",
        "molecule": "candidates", "molecules": "candidates",
        "compound": "candidates", "compounds": "candidates",
        "预测": "predictions", "prediction": "predictions", "predictions": "predictions",
        "变换": "transforms", "transform": "transforms", "transforms": "transforms",
        "任务": "tasks", "task": "tasks", "tasks": "tasks",
        "项目": "projects", "project": "projects", "projects": "projects",
    }
    _COUNTABLE_KEY_TOKENS = ("candidate", "transform", "prediction", "task", "project")

    @classmethod
    def _declared_context_counts(
        cls, context_payload: Mapping[str, Any]
    ) -> Tuple[Dict[str, List[float]], Dict[Tuple[str, str], List[float]]]:
        """Numeric values the context explicitly declares as counts.

        Returns two indexes:

        - family → declared count values (count-shaped keys whose path names the family:
          ``candidate_count``, ``enumerated_candidates.count``, ``prediction_summary.success``,
          ``summary.totalTasks``). An UNQUALIFIED count claim must equal one of these.
        - (family, qualifier) → values (any numeric leaf whose path names both, e.g.
          ``summary.activeProjects``). "N 个活跃项目" is verified against the
          active-scoped value only, never against the project total — a qualified claim
          compared against the wrong granularity is how honest answers got redacted.
        """
        declared: Dict[str, List[float]] = {}
        qualified: Dict[Tuple[str, str], List[float]] = {}

        def family_of(path: str) -> Optional[str]:
            lowered = path.lower()
            for family_token in cls._COUNTABLE_KEY_TOKENS:
                if family_token in lowered:
                    return f"{family_token}s"
            return None

        def visit(node: Any, path: str) -> None:
            if isinstance(node, Mapping):
                for key, value in node.items():
                    visit(value, f"{path}.{key}")
            elif isinstance(node, (int, float)) and not isinstance(node, bool):
                family = family_of(path)
                if family is None:
                    return
                value = float(node)
                token = path.rsplit(".", 1)[-1].lower()
                # Count-shaped keys in both snake_case and camelCase (candidate_count, count,
                # total, totalTasks, global_count) and per-state counters the platform declares
                # (taskStateCounts.SUCCESS/DRAFT, prediction_summary.success, …).
                is_count_key = (
                    token in {"count", "total", "queued", "running", "success", "failure", "failed", "draft", "pending"}
                    or token.endswith("_count")
                    or token.endswith("count")
                    or token.startswith("total")
                )
                if is_count_key:
                    declared.setdefault(family, []).append(value)
                lowered_path = path.lower()
                for qualifier in cls._COUNT_QUALIFIERS:
                    if qualifier in lowered_path:
                        qualified.setdefault((family, qualifier), []).append(value)

        visit(context_payload, "")
        return declared, qualified

    # Numbers with word-boundary guards: "IC50"/"CD73"/"p53" are labels, and their embedded
    # digits must never enter a value pool (a fabricated "50 nM" would otherwise pass via IC50).
    _STANDALONE_NUMBER = r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])"

    @classmethod
    def _source_numbers(cls, *sources: Any) -> List[float]:
        numbers: List[float] = []
        for source in sources:
            text = str(source or "")
            for match in re.finditer(cls._STANDALONE_NUMBER, text):
                try:
                    numbers.append(float(match.group(0).replace("−", "-")))
                except ValueError:
                    continue
        return numbers

    @classmethod
    def _source_numbers_by_unit(cls, sources_text: str) -> Dict[str, List[float]]:
        """Numbers from the sources, grouped by the metric unit they appear next to.

        Prose legitimately quotes min/max/mean over ONE coherent value set (the nM IC50s of a
        retrieval); the global pool is polluted by ids, dates and counts, so mean-verification
        is only meaningful per unit group.
        """
        text = str(sources_text or "")
        groups: Dict[str, List[float]] = {}
        boundaries = "{}\n"
        for unit_match in re.finditer(cls._METRIC_UNITS, text, re.IGNORECASE):
            unit = unit_match.group(0).lower().replace(" ", "")
            # Scope the window to the unit's own record: JSON object braces / newlines delimit
            # records, and an unbounded window drags neighbouring records' values (a kcal/mol
            # affinity) into an unrelated unit's group, skewing the mean verification.
            left = max((text.rfind(ch, 0, unit_match.start()) for ch in boundaries), default=-1)
            right_candidates = [text.find(ch, unit_match.end()) for ch in boundaries]
            right = min((p for p in right_candidates if p != -1), default=len(text))
            window = text[left + 1: right]
            bucket = groups.setdefault(unit, [])
            for number in re.finditer(cls._STANDALONE_NUMBER, window):
                try:
                    bucket.append(float(number.group(0).replace("−", "-")))
                except ValueError:
                    continue
        # Adjacent same-unit records put one number inside several units' windows; duplicates
        # would skew the mean verification, so each distinct value counts once per unit group.
        for unit, bucket in groups.items():
            groups[unit] = list(dict.fromkeys(bucket))
        return groups

    @classmethod
    def numeric_claim_issue(
        cls,
        *,
        message: Any,
        context_payload: Mapping[str, Any],
        numeric_sources: Any = "",
    ) -> Optional[str]:
        """Return an audit issue when the message asserts numbers the context contradicts.

        Two deterministic checks, both scoped to numbers the platform can actually ground:

        1. Count claims — "N candidates/predictions/tasks" (and "已完成 N 个" completion
           counts) must equal a count the context declares for that family. A zero-read turn
           answering "3 个候选分子，已完成 2 个" against candidate_count=365 /
           prediction_summary.success=0 is fabricated, and this is the structural gate for it.
        2. Metric claims — a value quoted with a platform metric unit (kcal/mol, pLDDT, ipTM,
           PAE, Å, µM/nM/mM, Da, g/mol) must match a number from the allowed sources (context,
           observations, user/system text) at the precision the message uses. Derived values
           (compute.aggregate means, unit conversions) appear in observation results, so honest
           arithmetic still passes; a "−9.2 kcal/mol" with no source number rounding to 9.2 is
           rejected with the offending values named.
        """
        text = str(message or "").strip()
        if not text:
            return None
        declared, qualified = cls._declared_context_counts(
            context_payload if isinstance(context_payload, Mapping) else {}
        )
        problems: List[str] = []

        for match in cls._COUNT_CLAIM_PATTERN.finditer(text):
            noun = (match.group("noun") or match.group("noun2") or match.group("noun3") or "").lower()
            number = match.group("num") or match.group("num2") or match.group("num3")
            raw_qualifier = (
                match.group("qualifier") or match.group("post_qualifier") or match.group("qualifier2") or ""
            )
            family = cls._COUNT_NOUN_FAMILIES.get(noun)
            if family is None:
                continue
            qualifier = cls._COUNT_QUALIFIER_ZH_EN.get(str(raw_qualifier)) or str(raw_qualifier).lower()
            if qualifier:
                # Qualified claim: verify against the SAME qualifier's declared value only.
                allowed = qualified.get((family, qualifier), [])
                if not allowed:
                    continue
                claimed = float(number)
                if claimed not in allowed:
                    problems.append(
                        f"claims {number} {raw_qualifier}{noun} but the context declares "
                        + " / ".join(f"{int(v) if v.is_integer() else v}" for v in sorted(set(allowed))[:4])
                    )
                continue
            if family not in declared:
                continue
            claimed = float(number)
            if claimed not in declared[family]:
                problems.append(
                    f"claims {number} {noun} but the context declares "
                    + " / ".join(f"{int(v) if v.is_integer() else v}" for v in sorted(set(declared[family]))[:4])
                )

        for match in cls._COMPLETED_COUNT_PATTERN.finditer(text):
            if "predictions" in declared:
                claimed = float(match.group("num"))
                success_values = declared.get("predictions", [])
                if success_values and claimed not in success_values:
                    problems.append(
                        f"claims 已完成 {match.group('num')} but the context's prediction success counts are "
                        + " / ".join(f"{int(v) if v.is_integer() else v}" for v in sorted(set(success_values))[:4])
                    )

        source_numbers = cls._source_numbers(numeric_sources)
        if source_numbers:
            unit_groups = cls._source_numbers_by_unit(str(numeric_sources or ""))
            for match in cls._METRIC_CLAIM_PATTERN.finditer(text):
                raw = (match.group("num") or match.group("num2") or "").replace("−", "-")
                if not raw:
                    continue
                try:
                    claimed = float(raw)
                except ValueError:
                    continue
                decimals = len(raw.split(".")[1]) if "." in raw else 0
                # A claim passes when a source value supports it at the message's precision, or
                # when it is the (deterministically recomputed) MEAN of the SAME unit's source
                # values — prose legitimately quotes means over one coherent value set (the nM
                # IC50s of a retrieval), and the audit verifies the arithmetic instead of
                # trusting it. A unit with no source values at all supports nothing.
                unit_key = re.sub(r"[\d.\s−-]", "", match.group(0)).lower()
                unit_values = unit_groups.get(unit_key, [])
                unit_mean = sum(unit_values) / len(unit_values) if unit_values else None
                supported = any(round(source, decimals) == claimed for source in source_numbers) or (
                    unit_mean is not None and round(unit_mean, decimals) == claimed
                )
                if not supported:
                    problems.append(f"quotes {raw} with a metric unit that no source value supports")

        if not problems:
            return None
        detail = "; ".join(problems[:4])
        return (
            f"the message asserts numbers no source supports ({detail}) — quote only counts and "
            "metric values the context/observations declare, at their declared precision; if a "
            "value is unknown, say so instead of inventing it"
        )

    @classmethod
    def fabricated_state_issue(
        cls,
        *,
        message: Any,
        questions: Any,
        context_payload: Mapping[str, Any],
        recent_action_resolutions: Any,
    ) -> Optional[str]:
        """Return an audit issue when the turn asserts host state no source supports.

        Two checks, both deterministic:

        1. Blocker fidelity — any mention of a machine state field (runBlockedReason, …)
           requires the ACTUAL value from the context to appear verbatim nearby. Paraphrase-
           only mentions are how invented blockers (e.g. a "missing seed structure" that no
           workflow declares) reach the user; when the context does not provide the field at
           all, mentioning it is fabrication outright.
        2. Completion claims — a past-tense "task/project created|submitted|opened" claim is
           licensed only by an APPLIED receipt of a lifecycle skill in recent_action_resolutions.
           Operations awaiting the user's confirmation are proposals, not done work.

        ``questions`` is the joined question/option text: fabricated state must not reach the
        user through the question chips either.
        """
        joined = "\n".join(str(part or "") for part in (message, questions) if str(part or "").strip())
        if not joined.strip():
            return None

        for marker in cls._STATE_FIELD_MARKERS:
            for match in re.finditer(marker, joined):
                actual_values = cls._context_state_values(context_payload, marker)
                if not actual_values:
                    return (
                        f"the reply cites {marker}, but the current context does not provide that "
                        "field at all — never invent machine state; describe only what "
                        "context_payload actually says"
                    )
                window = cls._normalized_state_text(joined[max(0, match.start() - 40): match.end() + 240])
                if not any(value in window for value in actual_values) and not any(
                    fragment in window
                    for value in actual_values
                    for fragment in cls._verbatim_fragments(value)
                ):
                    return (
                        f"the reply cites {marker} with a value the context does not contain — "
                        "quote machine state VERBATIM from the context or do not cite it; a "
                        "paraphrase is how invented blockers reach the user"
                    )

        # Quoted task-state claims run AFTER blocker fidelity: a state the context does not
        # show anywhere (no runtime displayTaskState, no row state, no receipt detail) is a
        # fabricated machine value (production: "已成功完成（状态 SUCCESS）" with zero tasks).
        context_state_blob = json.dumps(context_payload or {}, ensure_ascii=False, default=str).upper()
        for state_match in cls._TASK_STATE_CLAIM_PATTERN.finditer(joined):
            claimed = state_match.group(1).upper()
            if claimed not in context_state_blob:
                return (
                    f"the reply claims a task state of {claimed} — the live context does not "
                    "show that state for any task; a task's state is a machine value, quote it "
                    "only when the context or an applied receipt actually reports it"
                )
        resolutions = recent_action_resolutions if isinstance(recent_action_resolutions, list) else []
        # Context-observed terminal completion: 已完成/已成功/completed/succeeded are OBSERVED-state
        # verbs when the live context actually shows a finished task (SUCCESS/COMPLETED state on
        # any task or runtime) — true statements about what the page shows, licensed like any
        # other context value. With no finished task anywhere they stay unlicensed claims.
        context_state_blob = json.dumps(context_payload or {}, ensure_ascii=False, default=str).upper()
        context_shows_completion = ("SUCCESS" in context_state_blob) or ("COMPLETED" in context_state_blob)
        licensed = any(
            isinstance(row, Mapping)
            and str(row.get("status") or "") == "applied"
            and str(row.get("skill") or "").startswith(cls._LIFECYCLE_SKILL_PREFIXES)
            for row in resolutions
        )
        if not licensed and not context_shows_completion:
            for pattern in cls._COMPLETION_CLAIM_PATTERNS:
                for match in pattern.finditer(joined):
                    prefix = joined[max(0, match.start() - 12): match.start()]
                    if any(marker in prefix for marker in cls._PRIOR_TURN_MARKERS):
                        continue
                    return (
                        f"the reply claims {match.group(0)!r} (host state transition), but no "
                        "applied receipt for a lifecycle operation exists in this conversation — "
                        "operations awaiting the user's confirmation are proposals, never done "
                        "work; rephrase as the proposed next step"
                    )
        return None

    _SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?\n])")

    @classmethod
    def redact_unlicensed_state_sentences(
        cls,
        message: str,
        *,
        context_payload: Mapping[str, Any],
        recent_action_resolutions: Any,
    ) -> str:
        """Drop ONLY the sentences the state audit rejects; keep the grounded remainder.

        Replaces the wholesale honest-fallback: a correction round that still carries one
        bad sentence used to discard the ENTIRE reply (the user saw a generic "state could
        not be verified" instead of the valid diagnosis around it). Sentence-level removal
        via the SAME audit keeps a single source of truth; an empty result means nothing
        survived and the caller falls back to the deterministic honest message.
        """
        kept = [
            part
            for part in cls._SENTENCE_SPLIT_RE.split(str(message or ""))
            if not cls.fabricated_state_issue(
                message=part,
                questions="",
                context_payload=context_payload,
                recent_action_resolutions=recent_action_resolutions,
            )
        ]
        return "".join(kept)

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

    # Grounding is enforced when a turn's successful observations contain at most this many
    # discrete records. Small result sets (1–3 records) are exactly the case where the answer is
    # expected to name what was retrieved; larger lists are legitimately summarized in aggregate.
    GROUNDING_MAX_RECORDS = 3

    @staticmethod
    def _grounding_issue(message: Any, observations: Mapping[str, Dict[str, Any]]) -> str | None:
        """Return an audit issue when a final message ignores the records a skill retrieved.

        General, record-driven (no hardcoded identifiers): enforced when the successful
        observations contain at most ``GROUNDING_MAX_RECORDS`` discrete records — the answer is
        expected to name at least one of them (identity field or SMILES/sequence prefix). Larger
        result sets stay exempt because a faithful aggregate summary often quotes no single
        identifier verbatim, and rejecting those would hurt UX more than it protects.
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
        if not (1 <= len(records) <= CopilotSkillHarness.GROUNDING_MAX_RECORDS):
            return None
        anchors: List[str] = []
        for record in records:
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
            if len(records) == 1:
                return (
                    "the answer does not reference the single record the skill retrieved; re-answer using that "
                    "record's identity, SMILES, or sequence verbatim, or state plainly that you cannot resolve it"
                )
            return (
                f"the answer does not reference any of the {len(records)} retrieved records; re-answer naming "
                "at least one retrieved record's identity (accession, name, target, …) verbatim"
            )
        return None

    def execute_operations(
        self,
        operations: Sequence[PreparedOperation],
        *,
        abort: Any = None,
        deadline: float | None = None,
    ) -> Dict[str, Dict[str, Any]]:
        # abort/deadline bound an abandoned round: a disconnected client stops the wave before
        # its next submission, and a deadline past ends it — instead of running every queued
        # call (retries, rate-limiter sleeps) for a result nobody will read.
        operation_ids = {operation.operation_id for operation in operations}
        pending = list(operations)
        completed_ids: set[str] = set()
        observations: Dict[str, Dict[str, Any]] = {}

        for operation in operations:
            if not operation.definition.read_only:
                raise ValueError(f"write operation cannot execute in the harness: {operation.skill}")

        while pending:
            if abort is not None and abort.is_set():
                raise RuntimeError("planner aborted")
            if deadline is not None and time.monotonic() > deadline:
                raise RuntimeError("turn deadline exceeded during read execution")
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
                wave_arguments = operation.arguments
                if operation.pending_refs:
                    # Deferred read->read dataflow: materialize this wave's references against
                    # the observations the earlier waves just produced.
                    try:
                        wave_arguments = self.materialize_observations(operation.arguments, observations)
                    except ValueError as exc:
                        observations[operation.operation_id] = {
                            "skill": operation.skill,
                            "items": [
                                {
                                    "index": operation.index,
                                    "arguments": operation.arguments,
                                    "metadata": {},
                                    "ok": False,
                                    "result": None,
                                    "error": str(exc),
                                }
                            ],
                            "values": [],
                            "errors": [{"index": operation.index, "error": str(exc)}],
                            "ok": False,
                            "count": 1,
                            "successCount": 0,
                        }
                        continue
                calls.append(
                    PreparedSkillCall(
                        observation_id=operation.operation_id,
                        skill=operation.skill,
                        arguments=wave_arguments,
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
                    # LAYER SEPARATION: the card description is USER-facing — a one-line
                    # summary mechanically derived from the skill's first sentence. The full
                    # model-facing documentation (boundaries, sourcing rules) stays in the
                    # protocol catalog where the planner reads it; rendering it on the
                    # confirmation card was the "wall of text next to the button" bug.
                    "description": CopilotSkillHarness.user_facing_summary(operation.description, operation.label),
                    "arguments": dict(operation.arguments),
                    "payload": payload,
                    "effect": operation.definition.effect,
                    "needs_confirmation": True,
                    "execute_now": False,
                }
            )
        return actions

    @staticmethod
    def user_facing_summary(description: str, label: str = "", *, max_chars: int = 110) -> str:
        """First sentence of a skill description, capped — the user-facing card summary."""
        text = str(description or "").strip()
        for cut in (text.find(". "), text.find("。")):
            if cut > 0:
                text = text[: cut + 1]
                break
        text = " ".join(text.split())
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        if len(text) < 12 and label:
            return label
        return text or label

    # --- pi error philosophy: a tool's ANY failure mode is a model-visible result -----------
    # Every path a skill can fail by (raising, returning a non-record, returning values no
    # serializer can carry) becomes an ok=False observation naming the cause. The loop never
    # breaks on a tool error; the planner reads the error and self-corrects (retry, re-route,
    # or answer honestly). Only control-flow exceptions (abort/deadline) propagate.

    @classmethod
    def _json_safe(cls, value: Any) -> Any:
        """Recursively coerce a skill result into JSON-serializable data.

        External sources return arbitrary payloads (bytes bodies, datetimes, sets). An
        unserializable value inside an observation would blow up every later json.dumps —
        feedback rendering, ledger, memory, the turn's own response — killing the whole turn
        over one tool's payload. Coerce structurally: bytes decode/preview, datetimes
        isoformat, sets listify, anything else str().
        """
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8")[:400]
            except UnicodeDecodeError:
                return f"[{len(value)} bytes binary data]"
        if isinstance(value, (set, frozenset, tuple)):
            return [cls._json_safe(item) for item in list(value)[:40]]
        if isinstance(value, dict):
            return {str(key): cls._json_safe(child) for key, child in list(value.items())[:200]}
        if isinstance(value, list):
            return [cls._json_safe(item) for item in value[:200]]
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)[:400]

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
            if not error and result is not None and not isinstance(result, Mapping):
                # A skill contract violation is the skill's error, not the turn's: report it
                # as a failed result the planner can see (and route around) instead of
                # crashing the wave when the grouping tries to merge it as a record.
                error = (
                    f"skill returned a non-record result ({type(result).__name__}); "
                    "results must be JSON records — re-issue the call or use a different skill"
                )
            item = {
                "index": call.index,
                "arguments": self._json_safe(call.arguments),
                "metadata": call.metadata,
                "ok": not error,
                "result": self._json_safe(result),
                "error": error,
            }
            observation["items"].append(item)
            if error:
                observation["errors"].append({"index": call.index, "error": error})
            elif result is not None:
                observation["values"].append({**self._json_safe(result), **call.metadata})
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

    @classmethod
    def _iter_observation_refs(cls, value: Any):
        """Yield every $fromObservation id referenced anywhere inside ``value``."""
        if isinstance(value, list):
            for item in value:
                yield from cls._iter_observation_refs(item)
        elif isinstance(value, dict):
            if "$fromObservation" in value:
                ref = str(value.get("$fromObservation") or "").strip()
                if ref:
                    yield ref
            else:
                for child in value.values():
                    yield from cls._iter_observation_refs(child)

    def materialize_pending(
        self,
        operation: PreparedOperation,
        observations: Mapping[str, Dict[str, Any]],
        *,
        context_row_ids: Sequence[str] | None = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Materialize a deferred confirmation operation against freshly executed reads.

        Returns ``(arguments, None)`` when every pending reference resolved and the materialized
        arguments satisfy the skill's schema, or ``(None, reason)`` explaining what failed — the
        caller keeps the operation held and forwards the reason to the planner.
        """
        if not operation.pending_refs:
            return dict(operation.arguments), None
        try:
            materialized = self.materialize_observations(operation.arguments, dict(observations))
        except ValueError as exc:
            return None, str(exc)
        if not isinstance(materialized, dict):
            return None, "arguments must resolve to an object"
        errors = self._validate_schema(materialized, operation.definition.input_schema)
        if errors:
            return None, "; ".join(errors)
        if context_row_ids is not None:
            task_row_id = str(materialized.get("taskRowId") or "").strip()
            if task_row_id and task_row_id not in set(context_row_ids):
                return None, (
                    f"taskRowId ({task_row_id}) is not a task row in the current context — "
                    "an existing-task action may only reference rows from context_payload"
                )
        return materialized, None

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
    def _iter_string_values(cls, value: Any):
        """Yield every scalar string value inside ``value`` (skips keys)."""
        if isinstance(value, str):
            yield value
        elif isinstance(value, list):
            for item in value:
                yield from cls._iter_string_values(item)
        elif isinstance(value, dict):
            for child in value.values():
                yield from cls._iter_string_values(child)

    def silent_candidate_issues(
        self,
        operations: Sequence[PreparedOperation],
        observations: Mapping[str, Dict[str, Any]],
        *,
        allowed_text: str = "",
    ) -> Dict[str, str]:
        """Detect writes that apply ONE candidate of a multi-record search without a user choice.

        The deferred-reference guard catches $fromObservation consumption; this catches the PASTED
        form — a write whose argument value equals a field of one record of a search observation
        that returned several records. Values the USER's own message names are pre-choices, not
        silent picks, so they are exempt. Full-coverage consumption across the round's writes
        (fan-out) is legal. Returns {operation_id: issue}.
        """
        value_map: Dict[str, Tuple[str, int]] = {}
        totals: Dict[str, int] = {}
        for obs_id, obs in observations.items():
            if not isinstance(obs, dict):
                continue
            records = self._observation_records(obs)
            if len(records) <= 1:
                continue
            totals[obs_id] = len(records)
            for record_index, record in enumerate(records):
                for field_value in record.values():
                    if isinstance(field_value, str) and len(field_value) >= 4:
                        value_map.setdefault(field_value, (obs_id, record_index))
        if not value_map:
            return {}
        writes = [op for op in operations if not op.definition.read_only]
        consumed: Dict[str, set] = {}
        for op in writes:
            for text_value in self._iter_string_values(op.arguments):
                hit = value_map.get(text_value)
                if hit:
                    consumed.setdefault(hit[0], set()).add(hit[1])
        issues: Dict[str, str] = {}
        for op in writes:
            for text_value in self._iter_string_values(op.arguments):
                hit = value_map.get(text_value)
                if hit is None:
                    continue
                obs_id, record_index = hit
                if text_value in allowed_text:
                    continue
                # Choice by record identity: when the user's message NAMES the record this value
                # belongs to (e.g. clicked a choice chip labeled with its PDB id / CID), applying
                # that record's field is an explicit user choice — the consumed VALUE (a SMILES,
                # a URL) need not appear verbatim in the user's text.
                records = self._observation_records(observations[obs_id])
                if 0 <= record_index < len(records) and self.record_named_by_user(
                    records[record_index], allowed_text
                ):
                    continue
                if not set(consumed.get(obs_id, set())) >= set(range(totals[obs_id])):
                    issues[op.operation_id] = (
                        f"{op.operation_id} [{op.skill}] applies one entry of {obs_id}, which "
                        f"returned {totals[obs_id]} records — the user has not chosen one. Either "
                        "ask a choice question listing the candidates and apply only the entry "
                        "the user picks, fan out one operation per record if ALL are intended, "
                        "or re-emit only when the user's own message names the entry."
                    )
                    break
        return issues

    @staticmethod
    def record_named_by_user(record: Dict[str, Any], allowed_text: str) -> bool:
        """True when any of the record's identity fields appears in the user's turn text.

        This is the cross-turn resolution path for choice questions (model-asked or
        harness-synthesized): the user's answer names the chosen record's identity —
        「用 4NFF」「选 5354」 — and any later consumption of that record's fields is an
        explicit choice rather than a silent pick.
        """
        text = str(allowed_text or "").strip().lower()
        if not text:
            return False
        for field_name in RECORD_IDENTITY_FIELDS:
            value = str(record.get(field_name) or "").strip().lower()
            if len(value) >= 3 and value in text:
                return True
        return False

    # Context fields rendered in a synthesized option's hint / a records footer line, in this
    # order, skipping empties. Purely presentational complements to the identity label.
    RECORD_CONTEXT_FIELDS: Tuple[str, ...] = ("title", "name", "method", "resolution", "organism", "molecularFormula")

    @staticmethod
    def record_identity_label(record: Dict[str, Any]) -> str:
        """The record's display identity: the first populated canonical identity field."""
        for field in RECORD_IDENTITY_FIELDS:
            value = str(record.get(field) or "").strip()
            if value:
                return value[:80]
        for value in record.values():
            if isinstance(value, str) and len(value.strip()) >= 2:
                return value.strip()[:80]
        return ""

    @staticmethod
    def record_subtitle(record: Dict[str, Any]) -> str:
        """A one-line descriptive suffix for a record ('Human kallikrein… · X-ray · 1.9')."""
        parts: List[str] = []
        label = CopilotSkillHarness.record_identity_label(record)
        for field in CopilotSkillHarness.RECORD_CONTEXT_FIELDS:
            value = str(record.get(field) or "").strip()
            if value and value != label and value not in parts:
                parts.append(value)
        return " · ".join(parts)[:160]

    def synthesize_candidate_choice_question(
        self,
        write_operations: Sequence[Any],
        observations: Mapping[str, Dict[str, Any]],
    ) -> Optional[Tuple[Dict[str, Any], str]]:
        """Deterministically build the choice question the candidate guard demanded.

        When the planner has been told at least once to resolve a multi-record candidate pick
        and still re-emits a single-entry write, asking the user is a pure function of data the
        harness already holds: the referenced observation's records. The harness completes the
        action itself instead of re-sending the same corrective instruction — the model decides
        direction, the harness ensures completion.

        Returns ``(question, observation_id)`` or None when no write references a multi-record
        observation. Options are the records' identity labels (record-driven, works for any
        skill); ``allowOther`` stays true so the user is never boxed into the retrieved set.
        """
        # Deferred-reference form: the write consumes $fromObservation of a multi-record search.
        for op in write_operations:
            for ref in getattr(op, "pending_refs", ()) or ():
                observation = observations.get(ref)
                if not isinstance(observation, dict) or not observation.get("ok"):
                    continue
                records = self._observation_records(observation)
                if len(records) < 2:
                    continue
                question = self._build_choice_question(records, skill=str(getattr(op, "skill") or ""))
                if question:
                    return question, ref
        # Pasted-value form: an argument value equals a field of one record of a multi-record
        # search (mirror of silent_candidate_issues' detection, resolving back to the source).
        value_map: Dict[str, str] = {}
        for obs_id, observation in observations.items():
            if not isinstance(observation, dict) or not observation.get("ok"):
                continue
            records = self._observation_records(observation)
            if len(records) < 2:
                continue
            for record in records:
                for field_value in record.values():
                    if isinstance(field_value, str) and len(field_value) >= 4:
                        value_map.setdefault(field_value, obs_id)
        if not value_map:
            return None
        for op in write_operations:
            for text_value in self._iter_string_values(getattr(op, "arguments", None)):
                obs_id = value_map.get(text_value)
                if not obs_id:
                    continue
                records = self._observation_records(observations[obs_id])
                question = self._build_choice_question(records, skill=str(getattr(op, "skill") or ""))
                if question:
                    return question, obs_id
        return None

    def _build_choice_question(self, records: List[Dict[str, Any]], *, skill: str) -> Optional[Dict[str, Any]]:
        options: List[Dict[str, str]] = []
        seen: set = set()
        for record in records[:8]:
            label = self.record_identity_label(record)
            if not label or label in seen:
                continue
            seen.add(label)
            option: Dict[str, str] = {"label": label, "value": label}
            hint = self.record_subtitle(record)
            if hint:
                option["hint"] = hint
            options.append(option)
        if len(options) < 2:
            # A single distinguishable option is not a choice — leave the turn to the planner.
            return None
        purpose = f"（用于 {skill} / for {skill}）" if skill else ""
        text = (
            f"检索到 {len(records)} 条候选记录，请选择要使用的一条 {purpose}。\n"
            f"{len(records)} matching records were retrieved — choose one to continue."
        )
        return {
            "text": text[:400],
            "kind": "choice",
            "allowOther": True,
            "options": options,
        }


    @classmethod
    def _resolve_observation_reference(cls, value: Dict[str, Any], observations: Dict[str, Dict[str, Any]]) -> Any:
        observation_id = str(value.get("$fromObservation") or "").strip()
        observation = observations.get(observation_id)
        if observation is None:
            raise ValueError(
                f"Unknown observation reference: {observation_id} — observation ids live only "
                "inside the turn that retrieved them (ids from earlier turns or from confirmation "
                "receipts are not addressable). Re-run the lookup under a NEW operation id this "
                "turn (an identical call is served from cache), or pass the value directly when "
                "the schema permits it"
            )
        if not observation.get("ok"):
            raise ValueError(f"Observation {observation_id} contains failed skill calls")
        records = cls._observation_records(observation)
        field = value.get("field")
        if value.get("all") is True:
            # Column form: the field's value across every record (record 0 first), so a later skill
            # can consume a whole retrieved collection — e.g. compute.aggregate over a numeric column.
            column = [
                record.get(field)
                for record in records
                if isinstance(record, dict) and record.get(field) is not None
            ]
            if not column:
                raise ValueError(
                    f"Observation {observation_id} has no records with field {field!r} — the referenced "
                    "records do not carry that field. If the context_payload already declares the "
                    "number you need (a *_count field or a summary block), quote it directly "
                    "instead of aggregating; otherwise re-run the read whose records actually "
                    "carry the field"
                )
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
