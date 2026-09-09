from __future__ import annotations

from typing import Any, Dict, List

# Canonical workflow vocabulary (single source of truth, mirrors the frontend's
# WORKFLOWS registry). Skill schemas and capability prose DERIVE from this tuple —
# never re-list workflow keys where the model can see them.
WORKFLOW_KEYS: tuple[str, ...] = (
    "prediction",
    "virtual_screening",
    "affinity",
    "peptide_design",
    "lead_optimization",
)

# Backend availability per workflow (platform rule, single source). A workflow whose
# list has one entry has a FIXED backend — schemas expose that one value and prose
# describes the constraint by rule, never by hardcoding the backend's brand name.
_WORKFLOW_BACKENDS: Dict[str, List[str]] = {
    "virtual_screening": ["nesso"],
}


def workflow_backend_values(workflow_key: str) -> List[str]:
    # Backend values legal for a workflow (falls back to the general backend list).
    normalized = normalize_workflow_key(workflow_key)
    if normalized in _WORKFLOW_BACKENDS:
        return list(_WORKFLOW_BACKENDS[normalized])
    from management_api.copilot_capabilities import TASK_PARAMETER_SCHEMA  # local: avoid import cycle

    return list(TASK_PARAMETER_SCHEMA["backend"]["values"])


def normalize_workflow_key(value: Any, *, default: str = "prediction") -> str:
    token = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if token in {"prediction", "boltz_2_prediction", "boltz_prediction"}:
        return "prediction"
    if token in {"virtual_screening", "virtualscreening", "screening", "vs"}:
        return "virtual_screening"
    if token in {"affinity", "affinity_scoring", "boltz_2_affinity", "docking", "dock", "molecular_docking"}:
        return "affinity"
    if token in {"peptide", "peptide_design", "designer", "bicyclic_designer", "bicyclicdesigner"}:
        return "peptide_design"
    if token in {"leadopt", "lead_opt", "lead_optimization"}:
        return "lead_optimization"
    if "peptide" in token:
        return "peptide_design"
    if "screening" in token:
        return "virtual_screening"
    if "affinity" in token or "dock" in token:
        return "affinity"
    if "lead" in token and "opt" in token:
        return "lead_optimization"
    return default


def infer_workflow_key(context_payload: Dict[str, Any], *, default: str = "prediction") -> str:
    if not isinstance(context_payload, dict):
        return default
    direct = context_payload.get("workflow") or context_payload.get("workflow_key")
    if direct:
        return normalize_workflow_key(direct, default=default)
    page = context_payload.get("page")
    if isinstance(page, dict):
        page_workflow = page.get("workflowKey") or page.get("workflow_key") or page.get("workflow") or page.get("workflowTitle")
        if page_workflow:
            return normalize_workflow_key(page_workflow, default=default)
    project = context_payload.get("project")
    if isinstance(project, dict):
        value = project.get("task_type") or project.get("workflow") or project.get("workflow_key")
        if value:
            return normalize_workflow_key(value, default=default)
    return default
