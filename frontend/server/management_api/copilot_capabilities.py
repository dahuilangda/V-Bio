from __future__ import annotations

from typing import Any, Dict, List


COPILOT_CAPABILITIES: List[Dict[str, str]] = [
    {
        "name": "collaboration_message",
        "description": "Respond to shared project/task discussion, summarize context, mention collaborators, and keep a durable conversation trail.",
        "trigger": "General discussion, status questions, result interpretation, or collaborator notes.",
        "inputs": "User message, conversation history, current project/task/list context.",
        "confirmation": "No confirmation needed for read-only discussion.",
        "execution_boundary": "May not modify data or submit tasks.",
    },
    {
        "name": "project_list_analysis",
        "description": "Analyze projects by workflow, backend, task counts, activity, failures, and recency.",
        "trigger": "Questions about project portfolio, statistics, failures, active work, stale projects, or summaries.",
        "inputs": "Visible projects, filtered counts, current filters, sort order.",
        "confirmation": "No confirmation needed for pure analysis.",
        "execution_boundary": "Return findings and suggested next filters; do not change filters unless user confirms a plan action.",
    },
    {
        "name": "project_list_filter_sort",
        "description": "Plan and apply project list search, workflow/state/backend/activity filters, recency filters, min task count, and sorting.",
        "trigger": "Commands such as show failed projects, active projects, newest updated projects, Boltz projects, or projects with at least N tasks.",
        "inputs": "Current list controls and visible project statistics.",
        "confirmation": "Must present a plan. The UI applies it only after the user clicks the confirmation action.",
        "execution_boundary": "Only change list UI controls; never delete or create projects.",
    },
    {
        "name": "task_list_analysis",
        "description": "Analyze a project's task list by state, backend, workflow, metric columns, failures, runtime status, and result quality.",
        "trigger": "Questions about task trends, best/worst results, failed runs, queued/running tasks, or quality metrics.",
        "inputs": "Visible task rows, filters, sort order, metrics, current page.",
        "confirmation": "No confirmation needed for pure analysis.",
        "execution_boundary": "Return findings and suggested next filters; do not alter tasks.",
    },
    {
        "name": "task_list_filter_sort",
        "description": "Plan and apply task list search, state/workflow/backend filters, metric visibility, advanced filters, and sorting.",
        "trigger": "Commands such as show failures, show running tasks, sort by pLDDT, show recent tasks, or filter by backend.",
        "inputs": "Current task controls and visible task rows.",
        "confirmation": "Must present a plan. The UI applies it only after explicit confirmation.",
        "execution_boundary": "Only change list UI controls; never submit, cancel, or delete tasks.",
    },
    {
        "name": "task_result_analysis",
        "description": "Explain a specific task's state, errors, confidence, affinity metrics, ligand/protein setup, and likely next steps.",
        "trigger": "Questions about a selected task result, failure reason, reliability, or what to do next.",
        "inputs": "Task row, runtime state, properties, confidence, affinity, current project workflow.",
        "confirmation": "No confirmation needed for explanation.",
        "execution_boundary": "Do not claim experiments were rerun or files changed.",
    },
    {
        "name": "task_submission_planning",
        "description": "Draft a parameter-change and submission plan for the current task/project using existing UI form state.",
        "trigger": "Commands to rerun, change seed/backend/mode/parameters, submit variants, or batch submit candidates.",
        "inputs": "Current draft/task parameters, workflow, editable status, run disabled reason, requested changes.",
        "confirmation": "Always require a plan and explicit user confirmation before execution. If required parameters are missing, ask concise follow-up questions.",
        "execution_boundary": "Never submit directly from the model response. The host app must execute through the existing validated submit path after confirmation.",
    },
    {
        "name": "prediction.submit_plan",
        "description": "Plan Prediction workflow parameter updates and reruns.",
        "trigger": "Prediction rerun, seed change, or request to submit the current prediction draft.",
        "inputs": "Current Prediction draft, seed, components, constraints, run disabled reason.",
        "confirmation": "Always require explicit user confirmation before applying seed or running.",
        "execution_boundary": "Allowed patch keys: seed. Execution uses the existing Prediction Run path.",
    },
    {
        "name": "affinity.submit_plan",
        "description": "Plan Affinity workflow mode/seed updates and submission.",
        "trigger": "Affinity score/pose/refine/interface mode change, seed change, or submit request.",
        "inputs": "Current Affinity draft, target/ligand upload state, affinityMode, seed, run disabled reason.",
        "confirmation": "Always require explicit user confirmation before applying mode/seed or running.",
        "execution_boundary": "Allowed patch keys: seed, affinityMode. Execution uses the existing Affinity Run path.",
    },
    {
        "name": "peptide_design.submit_plan",
        "description": "Plan Peptide Design runtime option updates and submission.",
        "trigger": "Peptide binder length, design mode, iterations, population, elite size, mutation rate, seed, or submit request.",
        "inputs": "Current Peptide Design draft, peptide runtime options, components, run disabled reason.",
        "confirmation": "Always require explicit user confirmation before applying options or running.",
        "execution_boundary": "Allowed patch keys: seed and peptide* runtime options. Execution uses the existing Peptide Design Run path.",
    },
    {
        "name": "lead_optimization.submit_plan",
        "description": "Explain that Lead Optimization needs dedicated candidate/MMP tools before automated submission.",
        "trigger": "Lead Optimization candidate scoring, MMP query, batch candidate submission, or fragment selection requests.",
        "inputs": "Current Lead Optimization workspace state, selected fragments/candidates, backend, query state.",
        "confirmation": "Do not expose generic parameter patch actions. Ask clarifying questions or propose a dedicated lead-opt workflow plan.",
        "execution_boundary": "No generic patch keys are allowed yet. Dedicated candidate/MMP tools must be implemented separately.",
    },
]


def render_capability_prompt() -> str:
    lines = [
        "Available Copilot capabilities are modular and skill-like. Select the smallest matching capability.",
        "Never expose capability names, tool names, or internal skill identifiers to the user.",
        "Do not write labels such as capability used, 能力使用, 能力调用, or internal action ids.",
        "Before planning any task submission, validate whether the user's requested input matches the current workflow and identify missing required inputs.",
        "Workflow input requirements:",
        "- workflow: prediction",
        "  purpose: Structure prediction for protein, peptide, nucleic-acid, ligand, and complex inputs.",
        "  required: At least one valid structural component. Treat peptide sequences as protein components unless the user explicitly asks for peptide-design workflow options.",
        "  component edits: If the user says only/single/只有 one peptide/protein sequence, plan a component replacement with one protein component containing that sequence; do not copy old components.",
        "  reject: Do not use it for binding affinity-only scoring without a target/ligand affinity question.",
        "- workflow: affinity",
        "  purpose: Binding affinity estimation/scoring for a prepared target-ligand setup.",
        "  required: A target structure/sequence context and a ligand or separate affinity inputs, plus affinity mode when relevant.",
        "  reject: A lone peptide/protein sequence is not enough for Affinity Scoring and must not be submitted as an affinity task.",
        "- workflow: peptide_design",
        "  purpose: Design new peptide binders against a target.",
        "  required: Target context plus design options such as binder length/mode. A user-provided sequence alone is incomplete unless they ask to seed/constrain an existing design workflow.",
        "  reject: Do not treat a single sequence as a complete peptide-design submission unless target/design intent is clear.",
        "- workflow: lead_optimization",
        "  purpose: Optimize ligand candidates using dedicated MMP/candidate tools.",
        "  required: Lead compound/candidate context and the dedicated lead optimization operation.",
        "  reject: Do not expose generic submission actions.",
    ]
    for item in COPILOT_CAPABILITIES:
        lines.extend(
            [
                f"- name: {item['name']}",
                f"  description: {item['description']}",
                f"  trigger: {item['trigger']}",
                f"  inputs: {item['inputs']}",
                f"  confirmation: {item['confirmation']}",
                f"  execution_boundary: {item['execution_boundary']}",
            ]
        )
    return "\n".join(lines)


TASK_PARAMETER_SCHEMA: Dict[str, Dict[str, Any]] = {
    "seed": {"type": "int", "min": 0, "max": 2147483647},
    "affinityMode": {"type": "enum", "values": ["score", "pose", "refine", "interface"]},
    "peptideDesignMode": {"type": "enum", "values": ["linear", "cyclic", "bicyclic"]},
    "peptideBinderLength": {"type": "int", "min": 1, "max": 200},
    "peptideIterations": {"type": "int", "min": 1, "max": 10000},
    "peptidePopulationSize": {"type": "int", "min": 1, "max": 10000},
    "peptideEliteSize": {"type": "int", "min": 1, "max": 10000},
    "peptideMutationRate": {"type": "float", "min": 0.0, "max": 1.0},
    "componentsReplacement": {"type": "component_replacement"},
}

WORKFLOW_PARAMETER_KEYS: Dict[str, List[str]] = {
    "prediction": ["seed", "componentsReplacement"],
    "affinity": ["seed", "affinityMode"],
    "peptide_design": [
        "seed",
        "peptideDesignMode",
        "peptideBinderLength",
        "peptideIterations",
        "peptidePopulationSize",
        "peptideEliteSize",
        "peptideMutationRate",
    ],
    "lead_optimization": [],
}


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
    project = context_payload.get("project")
    if isinstance(project, dict):
        return normalize_workflow_key(project.get("task_type") or project.get("workflow") or project.get("workflow_key"))
    return "prediction"


def render_task_submission_schema_prompt(workflow_key: str = "prediction") -> str:
    normalized_workflow = normalize_workflow_key(workflow_key)
    allowed_keys = WORKFLOW_PARAMETER_KEYS.get(normalized_workflow, WORKFLOW_PARAMETER_KEYS["prediction"])
    lines = [
        "Task submission planning must output strict JSON only.",
        f"Current workflow: {normalized_workflow}",
        "Schema:",
        '{"capability":"task_submission_planning","intent":"...",'
        '"parameter_patch":{},"missing_questions":[],"risks":[],"needs_confirmation":true,"execute_now":false}',
        "Interpret user-requested input changes into parameter_patch schema. Do not copy existing components when the user asks for only/single one component.",
        "For structure prediction, peptide and protein sequence inputs are both protein components.",
        "Examples:",
        "- one protein sequence:",
        '{"parameter_patch":{"componentsReplacement":{"mode":"replace","components":[{"type":"protein","sequence":"<PROTEIN_SEQUENCE>","numCopies":1,"useMsa":true}],"clearConstraints":true}},"needs_confirmation":true,"execute_now":false}',
        "- protein plus small molecule ligand:",
        '{"parameter_patch":{"componentsReplacement":{"mode":"replace","components":[{"type":"protein","sequence":"<PROTEIN_SEQUENCE>","numCopies":1,"useMsa":true},{"type":"ligand","sequence":"<SMILES_OR_CCD>","numCopies":1,"inputMethod":"smiles"}],"clearConstraints":true}},"needs_confirmation":true,"execute_now":false}',
        "- protein plus peptide binder; peptide is represented as another protein component:",
        '{"parameter_patch":{"componentsReplacement":{"mode":"replace","components":[{"type":"protein","sequence":"<TARGET_PROTEIN_SEQUENCE>","numCopies":1,"useMsa":true},{"type":"protein","sequence":"<PEPTIDE_SEQUENCE>","numCopies":1,"useMsa":false}],"clearConstraints":true}},"needs_confirmation":true,"execute_now":false}',
        "Allowed parameter_patch keys for this workflow:",
    ]
    for key in allowed_keys:
        spec = TASK_PARAMETER_SCHEMA[key]
        if spec["type"] == "component_replacement":
            detail = (
                'object {"mode":"replace","components":[{"type":"protein","sequence":"<SEQUENCE>","numCopies":1,'
                '"useMsa":false}],"clearConstraints":true}; use only for explicit component input changes.'
            )
        elif spec["type"] == "enum":
            detail = ", ".join(spec["values"])
        else:
            detail = f"{spec['type']} range {spec['min']}..{spec['max']}"
        lines.append(f"- {key}: {detail}")
    if not allowed_keys:
        lines.append("- none. Ask follow-up questions or explain that this workflow needs a dedicated tool.")
    lines.append("All modifications and submissions must set needs_confirmation=true and execute_now=false.")
    return "\n".join(lines)


def _finite_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return parsed


def _sanitize_component_replacement(value: Any) -> Dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if str(value.get("mode") or "replace").strip().lower() != "replace":
        return None
    components_raw = value.get("components")
    if not isinstance(components_raw, list):
        return None
    components: List[Dict[str, Any]] = []
    for item in components_raw[:12]:
        if not isinstance(item, dict):
            continue
        component_type = str(item.get("type") or "protein").strip().lower()
        if component_type in {"peptide", "polypeptide"}:
            component_type = "protein"
        if component_type not in {"protein", "dna", "rna", "ligand"}:
            continue
        sequence = "".join(str(item.get("sequence") or "").split()).upper()
        if component_type == "ligand":
            sequence = str(item.get("sequence") or "").strip()
        if not sequence:
            continue
        component: Dict[str, Any] = {
            "id": str(item.get("id") or f"copilot-{component_type}-{len(components) + 1}"),
            "type": component_type,
            "sequence": sequence,
            "numCopies": max(1, int(_finite_number(item.get("numCopies")) or 1)),
        }
        if component_type == "protein":
            component["useMsa"] = bool(item.get("useMsa", False))
            component["cyclic"] = bool(item.get("cyclic", False))
        if component_type == "ligand":
            component["inputMethod"] = str(item.get("inputMethod") or "smiles")
        components.append(component)
    if not components:
        return None
    return {
        "mode": "replace",
        "reason": str(value.get("reason") or "User requested component input changes."),
        "components": components,
        "clearConstraints": value.get("clearConstraints", True) is not False,
    }


def sanitize_task_parameter_patch(candidate: Any, workflow_key: str = "prediction") -> Dict[str, Any]:
    if not isinstance(candidate, dict):
        return {}
    normalized_workflow = normalize_workflow_key(workflow_key)
    allowed_keys = set(WORKFLOW_PARAMETER_KEYS.get(normalized_workflow, WORKFLOW_PARAMETER_KEYS["prediction"]))
    sanitized: Dict[str, Any] = {}
    for key, value in candidate.items():
        normalized_key = str(key)
        if normalized_key not in allowed_keys:
            continue
        spec = TASK_PARAMETER_SCHEMA.get(normalized_key)
        if not spec:
            continue
        if spec["type"] == "component_replacement":
            if normalized_workflow != "prediction":
                continue
            replacement = _sanitize_component_replacement(value)
            if replacement:
                sanitized[normalized_key] = replacement
            continue
        if spec["type"] == "enum":
            token = str(value or "").strip()
            if token in spec["values"]:
                sanitized[normalized_key] = token
            continue
        number = _finite_number(value)
        if number is None:
            continue
        min_value = float(spec["min"])
        max_value = float(spec["max"])
        if number < min_value or number > max_value:
            continue
        if spec["type"] == "int":
            sanitized[normalized_key] = int(number)
        else:
            sanitized[normalized_key] = float(number)
    return sanitized


def build_task_submission_actions(candidate: Dict[str, Any], user_content: str, workflow_key: str = "prediction") -> List[Dict[str, Any]]:
    normalized_workflow = normalize_workflow_key(workflow_key)
    patch = sanitize_task_parameter_patch(candidate.get("parameter_patch"), normalized_workflow)
    if not patch:
        return []
    description_parts: List[str] = []
    for key, value in patch.items():
        if key == "componentsReplacement" and isinstance(value, dict):
            components = value.get("components") if isinstance(value.get("components"), list) else []
            first = components[0] if components and isinstance(components[0], dict) else {}
            sequence = str(first.get("sequence") or "").strip()
            description_parts.append(
                f"components: replace with one protein component {sequence}" if sequence else "components: replace"
            )
        else:
            description_parts.append(f"{key}: {value}")
    description = ", ".join(description_parts)
    actions = [
        {
            "id": "task_detail:apply_parameter_patch",
            "label": "Confirm parameter changes",
            "description": description,
            "payload": {"parameterPatch": patch, "workflowKey": normalized_workflow},
        }
    ]
    content = str(user_content or "").lower()
    wants_submit = any(token in content for token in ["submit", "run", "rerun", "提交", "运行", "重跑"])
    intent = str(candidate.get("intent") or "").lower()
    wants_submit = wants_submit or any(token in intent for token in ["submit", "run", "rerun"])
    if wants_submit:
        actions.append(
            {
                "id": "task_detail:apply_patch_and_submit",
                "label": "Confirm changes and run",
                "description": f"Apply {description}, then use the existing validated run action.",
                "payload": {"parameterPatch": patch, "workflowKey": normalized_workflow},
            }
        )
    return actions
