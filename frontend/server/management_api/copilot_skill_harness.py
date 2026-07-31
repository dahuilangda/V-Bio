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
# source of truth for "which fields identify a record" — used both to summarize observations for the
# model (copilot._summarize_observations) and to derive anti-hallucination grounding anchors
# (_grounding_issue). General by construction: a new skill's normalized record fields surface
# automatically once named here, instead of being hardcoded per consumer.
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
)
RECORD_LONG_FIELDS: Tuple[str, ...] = ("smiles", "sequence")
RECORD_NUMERIC_FIELDS: Tuple[str, ...] = ("avgPlddt", "resolution", "value", "length")


@dataclass(frozen=True)
class CopilotSkillDefinition:
    """The contract for one atomic operation known by the planner."""

    name: str
    description: str
    input_schema: Dict[str, Any]
    effect: str
    label: str = ""
    context_type: str | None = None
    payload_defaults: Dict[str, Any] = field(default_factory=dict)
    destructive: bool = False

    @property
    def read_only(self) -> bool:
        return self.effect.strip().lower() in READ_EFFECTS

    @property
    def requires_confirmation(self) -> bool:
        return not self.read_only


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
                    "items": {"type": "string", "minLength": 1},
                    "maxItems": 3,
                },
                "operations": {
                    "type": "array",
                    "items": {"oneOf": operation_variants},
                    "maxItems": self.max_calls_per_round,
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
                    "items": {"type": "string", "minLength": 1},
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
        catalog = [
            {
                "name": definition.name,
                "description": definition.description,
                "effect": definition.effect,
                "read_only": definition.read_only,
                "requires_confirmation": definition.requires_confirmation,
                "input_schema": definition.input_schema,
            }
            for definition in available.values()
        ]
        return (
            "Plan one turn with registered atomic skills. Return a user-visible message, unresolved questions, "
            "and ordered operations. Use questions only when required input is missing, and do not combine questions "
            "with operations. Use only skill names and argument fields declared by the registry. Operation ids must be "
            "unique across the planning loop, and dependencies may reference only earlier operations or observations. "
            "The harness derives the turn state, executes only read-only skills, and converts every non-read-only skill "
            "into a pending confirmation. The planner must never claim that a non-read-only operation already ran. "
            "The harness executes your read-only operations and returns their observations as a system message for the "
            "next round. Read what you need straight from those observations: most lookups finish in a single search "
            "operation whose observation already holds the answer (a SMILES or a protein sequence), so then return the "
            "final message with no further operations. Each round, emit only NEW operations with NEW ids; never repeat "
            "an operation id whose observation you have already received, and never invent template placeholders such "
            "as {{...}}. When a later operation needs a value a prior observation retrieved, you MUST reference it "
            "with {\"$fromObservation\": \"<id>\", \"field\": \"<field>\", \"index\": <n>} (record 0 is the top hit; "
            "common fields are sequence, smiles, accession, cid, geneNames) and set depends_on to that id — never "
            "paste a retrieved sequence, SMILES, or other long value into an argument. Run read-only lookups first; "
            "once their observations return, emit the non-read-only operation that consumes them in a later round — "
            "a single round may contain read-only operations OR non-read-only operations, never both. "
            "The output structure is enforced separately by the model server and must contain data only, never schema "
            "keywords or protocol metadata. Registered skill contracts:\n"
            f"{json.dumps(catalog, ensure_ascii=False, sort_keys=True)}"
        )

    def audit_plan(
        self,
        candidate: Any,
        definitions: Mapping[str, CopilotSkillDefinition],
        *,
        observations: Mapping[str, Dict[str, Any]] | None = None,
        context_type: str = "",
    ) -> PlanAudit:
        issues: List[str] = []
        if not isinstance(candidate, dict):
            return PlanAudit((), ("planner output must be an object",), {}, "")

        allowed_fields = {"message", "questions", "operations"}
        issues.extend(f"planner output field is not declared: {key}" for key in candidate if key not in allowed_fields)
        if not isinstance(candidate.get("message"), str) or not str(candidate.get("message") or "").strip():
            issues.append("message must be a non-empty string")
        if "questions" not in candidate:
            issues.append("questions is required")
        questions = candidate.get("questions")
        if not isinstance(questions, list) or any(not isinstance(item, str) for item in questions):
            issues.append("questions must be an array of strings")
            questions = []
        elif any(not item.strip() for item in questions):
            issues.append("questions must not contain empty strings")
        elif len(questions) > 3:
            issues.append("questions must contain at most 3 items")
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
                # The planner re-emitted an operation that already produced an observation.
                # Weaker planners routinely repeat a completed read instead of moving on;
                # treat it as already satisfied and carry the existing observation forward
                # rather than rejecting the whole turn (which would deadlock the loop).
                continue
            definition = definitions.get(skill_name)
            if definition is None:
                issues.append(f"{path}.skill is not registered: {skill_name}")
                continue
            if definition.context_type and context_type and definition.context_type != context_type:
                issues.append(f"{path}.skill is not available in context {context_type}: {skill_name}")
                continue
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
            issues.append("a turn must contain either read-only operations or confirmation operations, not both")
        if questions and raw_operations:
            issues.append("questions cannot accompany operations")
        if questions:
            state = "needs_input"
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
        return PlanAudit(tuple(prepared), tuple(issues), dict(candidate), state)

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
                    "contextType": context_type,
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
            index = 0
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
