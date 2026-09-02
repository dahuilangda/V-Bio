from __future__ import annotations

from typing import Any, Dict, List

from management_api.copilot_skill_harness import CONFIRMATION_EFFECTS, CopilotSkillDefinition
from management_api.copilot_skills.project_list import PROJECT_LIST_ACTION_SCHEMAS
from management_api.copilot_skills.task_list import TASK_LIST_ACTION_SCHEMAS
from management_api.copilot_skills.workflows import infer_workflow_key


# Declarative registry of host page operations, keyed by context type. Each entry is data only —
# the page-effect (create/update/delete/...), payload fields, defaults, and workflow gating — which
# build_context_skill_definitions turns into schema-backed planner skills. This is the single source
# of truth for what page operations the planner may propose; the host applies them through its own
# validated path after the user confirms.
CONTEXT_ACTION_SCHEMAS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "project_list": PROJECT_LIST_ACTION_SCHEMAS,
    "task_list": TASK_LIST_ACTION_SCHEMAS,
}


def _resolve_input_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Return a context action's JSON-schema for planner arguments.

    An action may declare ``input_schema`` explicitly; otherwise one is synthesized from the
    declarative payload_keys / payload_defaults / requires_payload fields so every action is
    schema-backed without a hand-written schema per action. Single source of truth for that
    synthesis — invoked by build_context_skill_definitions.
    """
    declared = schema.get("input_schema")
    if isinstance(declared, dict):
        return declared
    properties: Dict[str, Any] = {}
    for key in schema.get("payload_keys") or []:
        default = (schema.get("payload_defaults") or {}).get(key)
        property_schema: Dict[str, Any] = {"description": f"Payload field {key}."}
        if isinstance(default, bool):
            property_schema.update({"type": "boolean", "const": default})
        elif default is not None:
            property_schema.update({"type": "string", "enum": [str(default)]})
        else:
            property_schema.update({"type": "string"})
        properties[key] = property_schema
    return {
        "type": "object",
        "properties": properties,
        "required": list(schema.get("requires_payload") or []),
        "additionalProperties": False,
    }


def build_context_skill_definitions(
    context_type: str,
    context_payload: Dict[str, Any],
    *,
    workflow_key: str | None = None,
) -> List[CopilotSkillDefinition]:
    """Expose page operations as atomic, schema-backed planner skills."""

    normalized_context = str(context_type or "").strip()
    resolved_workflow_key = (
        infer_workflow_key(context_payload)
        if workflow_key is None
        else str(workflow_key or "").strip()
    )
    definitions: List[CopilotSkillDefinition] = []
    for action_id, schema in CONTEXT_ACTION_SCHEMAS.get(normalized_context, {}).items():
        required_workflows = schema.get("requires_workflow")
        if isinstance(required_workflows, list) and resolved_workflow_key not in required_workflows:
            continue
        input_schema = _resolve_input_schema(schema)
        if schema.get("requires_any_payload"):
            # "Pass only the fields the user wants to change" still means at least one field:
            # an empty payload is a no-op the user would be asked to confirm. Encode it in the
            # schema so both the planner contract and the harness audit enforce it.
            input_schema = {**input_schema, "minProperties": 1}
        defaults = dict(schema.get("payload_defaults") or {})
        # A page action is ALWAYS a user-confirmed operation: a data-declared effect outside the
        # confirmation taxonomy would synthesize a server-side "executable" skill with no handler.
        effect = str(schema.get("effect") or "").strip().lower()
        if effect not in CONFIRMATION_EFFECTS:
            effect = "create" if defaults.get("create") is True else "delete" if schema.get("destructive") else "update"
        target_context = str(schema.get("target_context") or "").strip() or None
        definitions.append(
            CopilotSkillDefinition(
                name=action_id,
                label=str(schema.get("label") or "").strip(),
                description=str(schema.get("description") or "").strip(),
                input_schema=input_schema,
                effect=effect,
                context_type=normalized_context,
                target_context=target_context,
                payload_defaults=defaults,
                destructive=bool(schema.get("destructive", False)),
            )
        )
    return definitions
