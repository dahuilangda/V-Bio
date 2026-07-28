from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from management_api.copilot_skills.online_databases import OnlineDatabaseSkills, OnlineSkillDefinition


READ_EFFECTS = {"read", "observe", "resolve", "inspect"}


@dataclass(frozen=True)
class CopilotSkillDefinition:
    """The contract for one atomic operation known by the planner."""

    name: str
    description: str
    input_schema: Dict[str, Any]
    effect: str = "write"
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
        return result

    def render_protocol_prompt(
        self,
        definitions: Mapping[str, CopilotSkillDefinition] | None = None,
    ) -> str:
        import json

        available = definitions if definitions is not None else self._definitions
        output_schema = {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "state": {"type": "string", "enum": ["continue", "await_confirmation", "needs_input", "complete"]},
                "questions": {"type": "array", "items": {"type": "string"}},
                "operations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "minLength": 1},
                            "skill": {"type": "string", "minLength": 1},
                            "arguments": {"type": "object"},
                            "label": {"type": "string", "minLength": 1},
                            "description": {"type": "string", "minLength": 1},
                            "depends_on": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["id", "skill", "arguments"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["message", "state", "operations"],
            "additionalProperties": False,
        }
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
            "Plan one turn as a sequence of atomic operations. The planner composes operations; "
            "the harness executes read-only operations and presents every write operation for confirmation. "
            "Use only registered skill contracts, keep operation ids unique, preserve operation order, and "
            "declare dependencies on earlier observations. Do not execute a write operation and do not emit "
            "undocumented fields. Return one JSON object matching this contract:\n"
            f"{json.dumps(output_schema, ensure_ascii=False, sort_keys=True)}\n"
            "Registered skills:\n"
            f"{json.dumps(catalog, ensure_ascii=False, sort_keys=True)}"
        )

    def prepare(self, candidate: Dict[str, Any]) -> Tuple[List[PreparedSkillCall], List[str]]:
        calls: List[PreparedSkillCall] = []
        errors: List[str] = []
        raw_calls = candidate.get("skill_calls") if isinstance(candidate.get("skill_calls"), list) else []
        raw_loops = candidate.get("skill_loops") if isinstance(candidate.get("skill_loops"), list) else []

        for position, raw in enumerate(raw_calls):
            if not isinstance(raw, dict):
                errors.append(f"skill_calls[{position}] must be an object")
                continue
            calls.extend(self._prepare_call(raw, position, errors))

        for loop_position, raw_loop in enumerate(raw_loops):
            if not isinstance(raw_loop, dict):
                errors.append(f"skill_loops[{loop_position}] must be an object")
                continue
            loop_id = str(raw_loop.get("id") or "").strip()
            skill = str(raw_loop.get("skill") or "").strip()
            if str(raw_loop.get("op") or "").strip() != "foreach":
                errors.append(f"skill_loops[{loop_position}].op must be foreach")
                continue
            items = raw_loop.get("items") if isinstance(raw_loop.get("items"), list) else []
            if not loop_id or not items:
                errors.append(f"skill_loops[{loop_position}] requires a non-empty id and items")
                continue
            for item_position, raw_item in enumerate(items):
                item = raw_item if isinstance(raw_item, dict) else {}
                prepared = {
                    "id": loop_id,
                    "skill": skill,
                    "arguments": item.get("arguments"),
                    "metadata": item.get("metadata"),
                }
                calls.extend(self._prepare_call(prepared, item_position, errors))

        if len(calls) > self.max_calls_per_round:
            errors.append(
                f"planner requested {len(calls)} skill calls; the per-round limit is {self.max_calls_per_round}"
            )
            return [], errors
        return calls, errors

    def _prepare_call(
        self,
        raw: Dict[str, Any],
        index: int,
        errors: List[str],
    ) -> List[PreparedSkillCall]:
        observation_id = str(raw.get("id") or "").strip()
        skill = str(raw.get("skill") or "").strip()
        arguments = raw.get("arguments") if isinstance(raw.get("arguments"), dict) else {}
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        definition = self._definitions.get(skill)
        if not observation_id or definition is None:
            errors.append(f"skill call at index {index} has an invalid id or skill")
            return []
        argument_errors = self._validate_arguments(arguments, definition)
        if argument_errors:
            errors.extend(f"{observation_id}[{index}]: {error}" for error in argument_errors)
            return []
        return [PreparedSkillCall(observation_id, skill, dict(arguments), dict(metadata), index)]

    @classmethod
    def _validate_arguments(
        cls,
        arguments: Dict[str, Any],
        definition: OnlineSkillDefinition | CopilotSkillDefinition,
    ) -> List[str]:
        return cls._validate_schema(arguments, definition.input_schema)

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
            return PlanAudit((), ("planner output must be an object",), {})

        allowed_fields = {"message", "state", "questions", "operations"}
        issues.extend(f"planner output field is not declared: {key}" for key in candidate if key not in allowed_fields)
        state = str(candidate.get("state") or "").strip().lower()
        if state not in {"continue", "await_confirmation", "needs_input", "complete"}:
            issues.append("state must be one of continue, await_confirmation, needs_input, complete")
        if not isinstance(candidate.get("message"), str):
            issues.append("message must be a string")
        questions = candidate.get("questions", [])
        if not isinstance(questions, list) or any(not isinstance(item, str) for item in questions):
            issues.append("questions must be an array of strings")
            questions = []
        raw_operations = candidate.get("operations")
        if not isinstance(raw_operations, list):
            issues.append("operations must be an array")
            return PlanAudit((), tuple(issues), dict(candidate))
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
            unknown_fields = set(raw) - {"id", "skill", "arguments", "label", "description", "depends_on"}
            issues.extend(f"{path}.{key} is not declared" for key in sorted(unknown_fields))
            operation_id = str(raw.get("id") or "").strip()
            skill_name = str(raw.get("skill") or "").strip()
            if not operation_id:
                issues.append(f"{path}.id is required")
                continue
            if operation_id in seen_ids:
                issues.append(f"{path}.id must be unique")
                continue
            seen_ids.add(operation_id)
            if operation_id in observation_map:
                issues.append(f"{path}.id was already used by an earlier operation")
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
            dependencies = raw.get("depends_on", [])
            if dependencies is None:
                dependencies = []
            if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
                issues.append(f"{path}.depends_on must be an array of strings")
                dependencies = []
            for dependency in dependencies:
                if dependency in observation_map and not observation_map[dependency].get("ok"):
                    issues.append(f"{path}.depends_on references a failed observation: {dependency}")
                elif dependency not in prior_ids and dependency not in observation_map:
                    issues.append(f"{path}.depends_on references an unknown prior operation: {dependency}")
            label = str(raw.get("label") or "").strip()
            description = str(raw.get("description") or "").strip()
            if definition.requires_confirmation and not label:
                issues.append(f"{path}.label is required for a confirmation operation")
            if definition.requires_confirmation and not description:
                issues.append(f"{path}.description is required for a confirmation operation")
            prepared.append(
                PreparedOperation(
                    operation_id=operation_id,
                    skill=skill_name,
                    arguments=arguments,
                    label=label,
                    description=description,
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
        if state == "continue" and not read_operations:
            issues.append("state continue requires at least one read-only operation")
        if state == "await_confirmation" and not write_operations:
            issues.append("state await_confirmation requires at least one write operation")
        if state == "needs_input" and (raw_operations or not questions):
            issues.append("state needs_input requires questions and no operations")
        if state == "complete" and raw_operations:
            issues.append("state complete requires no operations")
        return PlanAudit(tuple(prepared), tuple(issues), dict(candidate))

    audit = audit_plan

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
        if set(value) == {"$fromObservation"}:
            observation_id = str(value.get("$fromObservation") or "").strip()
            observation = observations.get(observation_id)
            if observation is None:
                raise ValueError(f"Unknown observation reference: {observation_id}")
            if not observation.get("ok"):
                raise ValueError(f"Observation {observation_id} contains failed skill calls")
            return list(observation.get("values") or [])
        return {
            key: self.materialize_observations(child, observations)
            for key, child in value.items()
        }

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
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if schema.get("minimum") is not None and value < schema["minimum"]:
                errors.append(f"{path} is below the declared minimum")
            if schema.get("maximum") is not None and value > schema["maximum"]:
                errors.append(f"{path} is above the declared maximum")
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
