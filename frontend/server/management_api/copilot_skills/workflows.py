from __future__ import annotations

from typing import Any, Dict


def normalize_workflow_key(value: Any) -> str:
    token = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if token in {"prediction", "boltz_2_prediction", "boltz_prediction"}:
        return "prediction"
    if token in {"affinity", "affinity_scoring", "boltz_2_affinity"}:
        return "affinity"
    if token in {"peptide", "peptide_design"}:
        return "peptide_design"
    if token in {"leadopt", "lead_opt", "lead_optimization"}:
        return "lead_optimization"
    if "peptide" in token:
        return "peptide_design"
    if "affinity" in token:
        return "affinity"
    if "lead" in token and "opt" in token:
        return "lead_optimization"
    return "prediction"


def infer_workflow_key(context_payload: Dict[str, Any]) -> str:
    if not isinstance(context_payload, dict):
        return "prediction"
    direct = context_payload.get("workflow") or context_payload.get("workflow_key")
    if direct:
        return normalize_workflow_key(direct)
    page = context_payload.get("page")
    if isinstance(page, dict):
        page_workflow = page.get("workflowKey") or page.get("workflow_key") or page.get("workflow") or page.get("workflowTitle")
        if page_workflow:
            return normalize_workflow_key(page_workflow)
    project = context_payload.get("project")
    if isinstance(project, dict):
        return normalize_workflow_key(project.get("task_type") or project.get("workflow") or project.get("workflow_key"))
    return "prediction"
