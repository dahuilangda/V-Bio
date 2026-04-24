from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from management_api.copilot_skills.project_list import PROJECT_LIST_ACTION_SCHEMAS
from management_api.copilot_skills.task_list import TASK_LIST_ACTION_SCHEMAS
from management_api.copilot_skills.workflows import infer_workflow_key


ACTION_SCHEMA_VERSION = "vbio-copilot-action-v2"

CONTEXT_ACTION_SCHEMAS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "project_list": PROJECT_LIST_ACTION_SCHEMAS,
    "task_list": TASK_LIST_ACTION_SCHEMAS,
}


def _context_rows(context_payload: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    rows = context_payload.get(key) if isinstance(context_payload, dict) else []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _known_ids(context_payload: Dict[str, Any], context_type: str) -> set[str]:
    if context_type == "project_list":
        return {str(row.get("id") or "").strip() for row in _context_rows(context_payload, "projects") if str(row.get("id") or "").strip()}
    if context_type == "task_list":
        return {str(row.get("id") or "").strip() for row in _context_rows(context_payload, "rows") if str(row.get("id") or "").strip()}
    return set()


def _context_row_by_id(context_payload: Dict[str, Any], context_type: str, row_id: str) -> Dict[str, Any] | None:
    normalized_id = str(row_id or "").strip()
    if not normalized_id:
        return None
    key = "projects" if context_type == "project_list" else "rows" if context_type == "task_list" else ""
    if not key:
        return None
    for row in _context_rows(context_payload, key):
        if str(row.get("id") or "").strip() == normalized_id:
            return row
    return None


def _has_active_project_work(row: Dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return True
    counts = row.get("task_counts") if isinstance(row.get("task_counts"), dict) else {}
    queued = _safe_int(counts.get("queued"))
    running = _safe_int(counts.get("running"))
    state = str(row.get("task_state") or row.get("state") or "").strip().upper()
    if not counts and not state:
        return True
    return queued > 0 or running > 0 or state in {"QUEUED", "RUNNING"}


def _has_active_task_work(row: Dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return True
    state = str(row.get("state") or row.get("task_state") or "").strip().upper()
    return state in {"", "QUEUED", "RUNNING"}


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _contains_intent_keyword(user_content: str, keywords: Any) -> bool:
    if not keywords:
        return True
    content = str(user_content or "").strip().lower()
    if not content:
        return True
    return any(str(keyword or "").strip().lower() in content for keyword in keywords)


def _sanitize_context_payload(action_id: str, payload: Any, schema: Dict[str, Any]) -> Dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    allowed = set(schema.get("payload_keys") or [])
    sanitized = {key: raw.get(key) for key in allowed if key in raw}
    sanitized = {**(schema.get("payload_defaults") or {}), **sanitized}

    for key in ("projectId", "projectName", "taskRowId", "taskName", "taskSummary"):
        if key in sanitized:
            sanitized[key] = str(sanitized[key] or "").strip()
    if "components" in sanitized:
        sanitized["components"] = _sanitize_prediction_components(sanitized["components"])
    if "workflowFilter" in sanitized:
        sanitized["workflowFilter"] = str(sanitized["workflowFilter"] or "").strip()
    if action_id in {"projects:create", "tasks:create", "tasks:create_with_sequence"}:
        sanitized["create"] = True
    if action_id.startswith("projects:workflow_"):
        sanitized["workflowFilter"] = action_id.replace("projects:workflow_", "", 1)
    if action_id.startswith("projects:") and action_id != "projects:create":
        sanitized = {key: value for key, value in sanitized.items() if value not in (None, "")}
    if action_id.startswith("tasks:") and action_id not in {"tasks:create"}:
        sanitized = {key: value for key, value in sanitized.items() if value not in (None, "")}
    return sanitized


def _normalize_component_type(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"peptide", "polypeptide", "aa", "amino_acid", "amino_acids"}:
        return "protein"
    if normalized in {"small_molecule", "smallmolecule", "molecule", "compound", "drug", "smiles", "ccd"}:
        return "ligand"
    if normalized in {"dna_sequence"}:
        return "dna"
    if normalized in {"rna_sequence"}:
        return "rna"
    return normalized


def _read_component_sequence(item: Dict[str, Any], component_type: str) -> str:
    for key in ("sequence", "value", "input", "content"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    if component_type == "ligand":
        for key in ("smiles", "ccd", "ligand", "molecule", "compound"):
            value = str(item.get(key) or "").strip()
            if value:
                return value
    return ""


def _sanitize_prediction_components(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    components: List[Dict[str, Any]] = []
    for item in value[:12]:
        if not isinstance(item, dict):
            continue
        component_type = _normalize_component_type(item.get("type"))
        if not component_type and any(str(item.get(key) or "").strip() for key in ("smiles", "ccd", "ligand")):
            component_type = "ligand"
        if component_type not in {"protein", "ligand", "dna", "rna"}:
            continue
        sequence = _read_component_sequence(item, component_type)
        if component_type in {"protein", "dna", "rna"}:
            sequence = "".join(sequence.split()).upper()
        if not sequence:
            continue
        component: Dict[str, Any] = {
            "type": component_type,
            "sequence": sequence,
            "numCopies": max(1, _safe_int(item.get("numCopies")) or 1),
        }
        if component_type == "protein":
            component["useMsa"] = bool(item.get("useMsa", True))
        if component_type == "ligand":
            input_method = str(item.get("inputMethod") or "smiles").strip().lower()
            component["inputMethod"] = input_method if input_method in {"smiles", "ccd"} else "smiles"
        components.append(component)
    return components


def _declared_ligand_tokens(user_content: str) -> List[str]:
    content = str(user_content or "")
    if not content.strip():
        return []
    token_pattern = r"([A-Za-z0-9@+\-\[\]\(\)=#$\\/%.]+)"
    patterns = [
        rf"(?:小分子|配体|化合物)\s*(?:序列|SMILES|smiles|为|是|:|：|=)?\s*{token_pattern}",
        rf"(?:small\s+molecule|ligand|compound|SMILES|smiles)\s*(?:sequence|is|as|:|=)?\s*{token_pattern}",
    ]
    tokens: List[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, content, flags=re.IGNORECASE):
            token = str(match.group(1) or "").strip().strip("，,。.;；")
            if not token:
                continue
            key = token.lower()
            if key in seen:
                continue
            seen.add(key)
            tokens.append(token)
    return tokens


def _component_contains_sequence(components: List[Dict[str, Any]], component_type: str, sequence: str) -> bool:
    expected = str(sequence or "").strip().lower()
    if not expected:
        return True
    for component in components:
        if not isinstance(component, dict):
            continue
        if str(component.get("type") or "").strip().lower() != component_type:
            continue
        if str(component.get("sequence") or "").strip().lower() == expected:
            return True
    return False


def _context_action_is_allowed(
    *,
    context_type: str,
    context_payload: Dict[str, Any],
    action_id: str,
    payload: Dict[str, Any],
    workflow_key: str,
    user_content: str = "",
) -> bool:
    schema = CONTEXT_ACTION_SCHEMAS.get(context_type, {}).get(action_id)
    if not schema:
        return False
    if not _contains_intent_keyword(user_content, schema.get("intent_keywords")):
        return False
    required_workflows = schema.get("requires_workflow")
    if isinstance(required_workflows, list) and workflow_key not in required_workflows:
        return False
    for key in schema.get("requires_payload") or []:
        if not str(payload.get(key) or "").strip():
            return False
    any_payload_keys = schema.get("requires_any_payload") or []
    if any_payload_keys and not any(payload.get(key) for key in any_payload_keys):
        return False
    known = _known_ids(context_payload, context_type)
    if known:
        if context_type == "project_list" and "projectId" in payload and str(payload["projectId"]) not in known:
            return False
        if context_type == "task_list" and "taskRowId" in payload and str(payload["taskRowId"]) not in known:
            return False
    if schema.get("requires_active_project"):
        row = _context_row_by_id(context_payload, context_type, str(payload.get("projectId") or ""))
        if not _has_active_project_work(row):
            return False
    if schema.get("requires_active_task"):
        row = _context_row_by_id(context_payload, context_type, str(payload.get("taskRowId") or ""))
        if not _has_active_task_work(row):
            return False
    if action_id == "tasks:create_with_sequence":
        components = payload.get("components") if isinstance(payload.get("components"), list) else []
        if not components:
            return False
        for ligand_token in _declared_ligand_tokens(user_content):
            if not _component_contains_sequence(components, "ligand", ligand_token):
                return False
        has_valid_component = False
        for component in components:
            if not isinstance(component, dict):
                continue
            component_type = str(component.get("type") or "")
            value = str(component.get("sequence") or "")
            if component_type == "protein" and len(value) >= 2 and all(char in "ACDEFGHIKLMNPQRSTVWY" for char in value):
                has_valid_component = True
            if component_type in {"ligand", "dna", "rna"} and value:
                has_valid_component = True
        if not has_valid_component:
            return False
    if action_id == "tasks:rename" and not (
        str(payload.get("taskName") or "").strip() or str(payload.get("taskSummary") or "").strip()
    ):
        return False
    if context_type == "project_list":
        if action_id.startswith("projects:workflow_"):
            return str(payload.get("workflowFilter") or "") in {"prediction", "affinity", "peptide_design", "lead_optimization"}
        if action_id == "projects:backend_boltz" and str(payload.get("backendFilter") or "").strip().lower() != "boltz":
            return False
    return True


def render_context_action_tool_schema(context_type: str) -> str:
    schemas = CONTEXT_ACTION_SCHEMAS.get(str(context_type or "").strip(), {})
    if not schemas:
        return "[]"
    rendered: List[Dict[str, Any]] = []
    for action_id, schema in schemas.items():
        input_schema = schema.get("input_schema")
        if not isinstance(input_schema, dict):
            properties: Dict[str, Any] = {}
            for key in schema.get("payload_keys") or []:
                default = (schema.get("payload_defaults") or {}).get(key)
                prop: Dict[str, Any] = {"description": f"Payload field {key}."}
                if isinstance(default, bool):
                    prop.update({"type": "boolean", "const": default})
                elif default is not None:
                    prop.update({"type": "string", "enum": [str(default)]})
                else:
                    prop.update({"type": "string"})
                properties[key] = prop
            input_schema = {
                "type": "object",
                "properties": properties,
                "required": list(schema.get("requires_payload") or []),
                "additionalProperties": False,
            }
        rendered.append(
            {
                "name": action_id,
                "description": schema.get("description") or "",
                "destructive": bool(schema.get("destructive", False)),
                "requires_confirmation": True,
                "input_schema": input_schema,
            }
        )
    return json.dumps(rendered, ensure_ascii=False, indent=2, sort_keys=True)


def _project_list_action_matches_user_intent(action_id: str, payload: Dict[str, Any], user_content: str) -> bool:
    content = str(user_content or "").strip().lower()
    if not content:
        return True
    if "affinity" in content or "亲和" in content:
        return action_id == "projects:workflow_affinity" or action_id in {"projects:open", "projects:delete", "projects:cancel_active"}
    if "boltz" in content:
        return action_id == "projects:backend_boltz" or action_id in {"projects:open", "projects:delete", "projects:cancel_active"}
    return True


def build_context_actions(
    context_type: str,
    candidate: Dict[str, Any],
    context_payload: Dict[str, Any],
    user_content: str = "",
) -> List[Dict[str, Any]]:
    normalized_context = str(context_type or "").strip()
    missing_questions = candidate.get("missing_questions") if isinstance(candidate, dict) else []
    if isinstance(missing_questions, list) and any(str(item or "").strip() for item in missing_questions):
        return []
    workflow_key = infer_workflow_key(context_payload)
    planned = candidate.get("actions") or candidate.get("plan_actions") or []
    if not isinstance(planned, list):
        planned = [planned] if isinstance(planned, dict) else []

    actions: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in planned:
        if not isinstance(item, dict):
            continue
        if item.get("execute_now") is True or item.get("needs_confirmation") is False:
            continue
        action_id = str(item.get("id") or "").strip()
        schema = CONTEXT_ACTION_SCHEMAS.get(normalized_context, {}).get(action_id)
        if not schema:
            continue
        payload = _sanitize_context_payload(action_id, item.get("payload") or {}, schema)
        if not _context_action_is_allowed(
            context_type=normalized_context,
            context_payload=context_payload,
            action_id=action_id,
            payload=payload,
            workflow_key=workflow_key,
            user_content=user_content,
        ):
            continue
        if normalized_context == "project_list" and not _project_list_action_matches_user_intent(action_id, payload, user_content):
            continue
        dedupe_key = f"{action_id}:{payload}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        actions.append(
            {
                "id": action_id,
                "label": str(item.get("label") or schema["label"]).strip() or schema["label"],
                "description": str(item.get("description") or schema["description"]).strip() or schema["description"],
                "payload": {
                    **payload,
                    "schemaVersion": ACTION_SCHEMA_VERSION,
                    "contextType": normalized_context,
                    "workflowKey": workflow_key,
                    "destructive": bool(schema.get("destructive", False)),
                },
                "needs_confirmation": True,
                "execute_now": False,
            }
        )
    return actions
