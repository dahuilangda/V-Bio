from __future__ import annotations

from typing import Any, Dict, List

from management_api.copilot_skills.context_actions import (
    ACTION_SCHEMA_VERSION,
    CONTEXT_ACTION_SCHEMAS,
    build_context_actions,
    render_context_action_tool_schema,
)
from management_api.copilot_skills.workflows import infer_workflow_key, normalize_workflow_key


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


def render_context_plan_schema_prompt(context_type: str, context_payload: Dict[str, Any]) -> str:
    if context_type == "task_detail":
        workflow_key = infer_workflow_key(context_payload)
        return render_task_submission_schema_prompt(workflow_key)
    if context_type == "task_list":
        return _render_task_list_plan_schema()
    if context_type == "project_list":
        return _render_project_list_plan_schema()
    return "No planning schema available for this context. Return {\"actions\":[]}."


def _render_task_list_plan_schema() -> str:
    return (
        "Current context: task_list (a project's task list page).\n"
        "Use the page workflow to decide whether an operation is valid. The current project workflow is in context_payload.project.task_type.\n"
        "Schema: return a JSON object with an \"actions\" array.\n"
        "Each action must look like {\"id\":\"tasks:<verb>\",\"label\":\"<short label>\",\"description\":\"<detail>\",\"payload\":{...},\"needs_confirmation\":true,\"execute_now\":false}.\n"
        "Treat the following as tool definitions. Choose only one or two smallest matching actions; copy IDs exactly from context when required:\n"
        f"{render_context_action_tool_schema('task_list')}\n"
        "Rules:\n"
        "- Component extraction is semantic, not regex-only. First honor explicit labels in the user message, then infer from the value shape only when no label is present.\n"
        "- Label mapping: 蛋白/protein/peptide/多肽/氨基酸 -> protein; 小分子/配体/化合物/compound/ligand/drug/SMILES/smiles -> ligand; DNA/dna/脱氧核糖核酸 -> dna; RNA/rna/核糖核酸 -> rna.\n"
        "- A protein/peptide sequence is usually 2+ letters from the amino acid alphabet (ACDEFGHIKLMNPQRSTVWY), but an explicitly labeled ligand wins over this rule. Example: 小分子为 ATP is ligand, not protein.\n"
        "- Ligands may be lowercase, uppercase, mixed-case, symbolic SMILES such as CN1C=NC2=C1C(=O)N(C)C(=O)N2C, or CCD IDs such as ATP/NAD/HEM. Preserve ligand text exactly except surrounding whitespace.\n"
        "- Preserve every user-provided component in order. If the user provides multiple proteins/ligands/DNA/RNA items, payload.components must include all of them; do not merge, drop, or copy old components.\n"
        "- Component schema: {type:\"protein|ligand|dna|rna\", sequence:\"...\", numCopies:1, useMsa:true/false, inputMethod:\"smiles|ccd\" for ligand}. For proteins default useMsa=true unless the user says no MSA or the component is clearly a peptide binder.\n"
        "- If the user provides a sequence and wants to predict/submit, use tasks:create_with_sequence only when current workflow is prediction.\n"
        "- If the user asks only to create/fill a task, create the draft action. If they asks to run/submit/predict too, still return the same confirmed create action; the UI will ask for run confirmation after the draft is filled.\n"
        "- If the user request is ambiguous or lacks necessary information, return {\"actions\":[],\"missing_questions\":[\"...\"]}. Ask for clarification instead of forcing a confirmation button.\n"
        "- Ambiguous examples: unlabeled uppercase strings that could be peptide/protein/CCD/SMILES; 'new task' without any component; 'delete it' with multiple plausible target tasks; a workflow request that conflicts with current project type.\n"
        "- If current workflow is affinity, peptide_design, or lead_optimization and the user asks to predict a lone sequence, return no actions; the assistant message should explain they are in the wrong project function.\n"
        "- For delete or cancel, identify the target task from context_payload.rows by matching name or task_id.\n"
        "- Use tasks:cancel for stop/terminate/cancel operations on running or queued tasks.\n"
        "- Use tasks:delete for removing task records entirely.\n"
        "- All actions require user confirmation (needs_confirmation=true, execute_now=false).\n"
        "Broad examples, adapt them to the user's exact labels and values:\n"
        "- protein-only task / 只有一条蛋白 / predict this peptide:\n"
        "  {\"actions\":[{\"id\":\"tasks:create_with_sequence\",\"label\":\"新建预测任务\",\"description\":\"创建包含蛋白序列 MEEPQSDPSV 的新预测任务\",\"payload\":{\"create\":true,\"components\":[{\"type\":\"protein\",\"sequence\":\"MEEPQSDPSV\",\"numCopies\":1,\"useMsa\":true}]}}]}\n"
        "- protein plus explicitly labeled small molecule, including uppercase ligand-like text:\n"
        "  {\"actions\":[{\"id\":\"tasks:create_with_sequence\",\"label\":\"新建预测任务\",\"description\":\"创建包含蛋白序列 GSHMKWVTFISLLFLFSSAYSRGV 和小分子 ATP 的新预测任务\",\"payload\":{\"create\":true,\"components\":[{\"type\":\"protein\",\"sequence\":\"GSHMKWVTFISLLFLFSSAYSRGV\",\"numCopies\":1,\"useMsa\":true},{\"type\":\"ligand\",\"sequence\":\"ATP\",\"numCopies\":1,\"inputMethod\":\"ccd\"}]}}]}\n"
        "- protein plus drug-like SMILES / compound CN1C=NC2=C1C(=O)N(C)C(=O)N2C / ligand NAD:\n"
        "  {\"actions\":[{\"id\":\"tasks:create_with_sequence\",\"label\":\"新建预测任务\",\"description\":\"创建包含蛋白和 ligand 的新预测任务\",\"payload\":{\"create\":true,\"components\":[{\"type\":\"protein\",\"sequence\":\"MEEPQSDPSV\",\"numCopies\":1,\"useMsa\":true},{\"type\":\"ligand\",\"sequence\":\"CN1C=NC2=C1C(=O)N(C)C(=O)N2C\",\"numCopies\":1,\"inputMethod\":\"smiles\"},{\"type\":\"ligand\",\"sequence\":\"NAD\",\"numCopies\":1,\"inputMethod\":\"ccd\"}]}}]}\n"
        "- mixed biomolecules / DNA and RNA are not proteins:\n"
        "  {\"actions\":[{\"id\":\"tasks:create_with_sequence\",\"label\":\"新建预测任务\",\"description\":\"创建包含蛋白、DNA 和 RNA 的新预测任务\",\"payload\":{\"create\":true,\"components\":[{\"type\":\"protein\",\"sequence\":\"MSTNPKPQR\",\"numCopies\":1,\"useMsa\":true},{\"type\":\"dna\",\"sequence\":\"ATCGATCG\",\"numCopies\":1},{\"type\":\"rna\",\"sequence\":\"AUGCUU\",\"numCopies\":1}]}}]}\n"
        "- user asks to show failed tasks:\n"
        "  {\"actions\":[{\"id\":\"tasks:failure\",\"label\":\"显示失败任务\",\"description\":\"筛选 FAILURE 状态的任务\",\"payload\":{\"stateFilter\":\"FAILURE\"}}]}\n"
        "- user asks to sort by pLDDT / 按 pLDDT 排序 / 按 置信度 排序:\n"
        "  {\"actions\":[{\"id\":\"tasks:sort_plddt\",\"label\":\"按 pLDDT 排序\",\"description\":\"按置信度 pLDDT 从高到低排序\",\"payload\":{\"sortKey\":\"plddt\"}}]}\n"
        "- user asks to sort by ipTM / 按 ipTM 排序:\n"
        "  {\"actions\":[{\"id\":\"tasks:sort_iptm\",\"label\":\"按 ipTM 排序\",\"description\":\"按界面 ipTM 从高到低排序\",\"payload\":{\"sortKey\":\"iptm\"}}]}\n"
        "- user asks to delete a task:\n"
        "  {\"actions\":[{\"id\":\"tasks:delete\",\"label\":\"删除任务\",\"description\":\"删除任务 xxx\",\"payload\":{\"taskRowId\":\"<matched-id>\",\"taskName\":\"xxx\"}}]}\n"
        "- user asks to cancel/stop/terminate a task / 取消/停止 任务:\n"
        "  {\"actions\":[{\"id\":\"tasks:cancel\",\"label\":\"取消任务\",\"description\":\"取消正在运行的任务 xxx\",\"payload\":{\"taskRowId\":\"<matched-id>\",\"taskName\":\"xxx\"}}]}\n"
        "Return JSON only. Do not explain."
    )


def _render_project_list_plan_schema() -> str:
    return (
        "Current context: project_list (all projects overview page).\n"
        "The user can create new projects, delete projects, and analyze/project statistics. "
        "The user CANNOT submit specific tasks from this page.\n"
        "Schema: return a JSON object with an \"actions\" array.\n"
        "Each action must look like {\"id\":\"projects:<verb>\",\"label\":\"<short label>\",\"description\":\"<detail>\",\"payload\":{...},\"needs_confirmation\":true,\"execute_now\":false}.\n"
        "Treat the following as tool definitions. Choose only one or two smallest matching actions; copy IDs exactly from context when required:\n"
        f"{render_context_action_tool_schema('project_list')}\n"
        "Rules:\n"
        "- For open/delete/cancel_active, identify the target project from context_payload.projects by matching name or id.\n"
        "- If the user asks for affinity-related projects, use projects:workflow_affinity, not projects:backend_boltz.\n"
        "- Use projects:backend_boltz only when the user explicitly asks for Boltz backend.\n"
        "- If the user asks to submit/predict/run a task, return no actions from project_list; they must open the right project/task function first.\n"
        "- All create and delete actions require user confirmation (needs_confirmation=true, execute_now=false).\n"
        "- Filter/sort actions also require user confirmation.\n"
        "Examples:\n"
        "- user asks to create a project:\n"
        "  {\"actions\":[{\"id\":\"projects:create\",\"label\":\"新建项目\",\"description\":\"创建一个新项目\",\"payload\":{\"create\":true}}]}\n"
        "- user asks to delete a project:\n"
        "  {\"actions\":[{\"id\":\"projects:delete\",\"label\":\"删除项目\",\"description\":\"删除项目 xxx\",\"payload\":{\"projectId\":\"<matched-id>\",\"projectName\":\"xxx\"}}]}\n"
        "Return JSON only. Do not explain."
    )


def render_task_submission_schema_prompt(workflow_key: str = "prediction") -> str:
    normalized_workflow = normalize_workflow_key(workflow_key)
    allowed_keys = WORKFLOW_PARAMETER_KEYS.get(normalized_workflow, WORKFLOW_PARAMETER_KEYS["prediction"])
    lines = [
        "Task detail context: the user is viewing/editing a specific task within a project.",
        "They can create new tasks, delete the current task, or analyze the current task.",
        "Schema: return a JSON object.",
        "If the user wants to modify parameters or submit:",
        '{"capability":"task_submission_planning","intent":"...",'
        '"parameter_patch":{},"missing_questions":[],"risks":[],"needs_confirmation":true,"execute_now":false}',
        "If the user wants to delete the current task:",
        '{"capability":"task_deletion","intent":"delete","needs_confirmation":true,"execute_now":false}',
        "If the user wants to rename or update description for the current task:",
        '{"capability":"task_metadata_patch","intent":"rename","metadata_patch":{"taskName":"<NEW_NAME>","taskSummary":"<NEW_DESCRIPTION>"},"needs_confirmation":true,"execute_now":false}',
        "If the request is ambiguous or missing necessary information:",
        '{"capability":"clarification_needed","intent":"ask_clarifying_question","missing_questions":["<QUESTION_TO_ASK_USER>"],"needs_confirmation":false,"execute_now":false}',
        "Interpret user-requested input changes into parameter_patch schema. Component extraction is semantic: first honor explicit labels, then infer from value shape only when no label is present.",
        "Label mapping: 蛋白/protein/peptide/多肽/氨基酸 -> protein; 小分子/配体/化合物/compound/ligand/drug/SMILES/smiles -> ligand; DNA/dna -> dna; RNA/rna -> rna.",
        "For structure prediction, peptide and protein sequence inputs are both protein components, unless the user explicitly labels the value as ligand/small molecule/SMILES.",
        "When the user asks for only/single/只有/rewrite/replace components, replace the component list and clear constraints unless they explicitly ask to keep constraints. Do not copy old components.",
        "If the user gives multiple components, preserve all components in order. Uppercase ligand text such as ATP/NAD/HEM remains ligand when labeled 小分子/ligand/CCD/SMILES.",
        "If the type or value of a component is unclear, or the user asks to run but required inputs are missing, set capability=clarification_needed with missing_questions instead of producing a confirmation action.",
        "Broad examples, adapt them to the exact user labels and values:",
        "- replace with one protein sequence:",
        '{"parameter_patch":{"componentsReplacement":{"mode":"replace","components":[{"type":"protein","sequence":"<PROTEIN_SEQUENCE>","numCopies":1,"useMsa":true}],"clearConstraints":true}},"needs_confirmation":true,"execute_now":false}',
        "- replace with protein plus explicitly labeled small molecule ligand, preserving uppercase ligand text:",
        '{"parameter_patch":{"componentsReplacement":{"mode":"replace","components":[{"type":"protein","sequence":"GSHMKWVTFISLLFLFSSAYSRGV","numCopies":1,"useMsa":true},{"type":"ligand","sequence":"ATP","numCopies":1,"inputMethod":"ccd"}],"clearConstraints":true}},"needs_confirmation":true,"execute_now":false}',
        "- replace with multiple ligands or SMILES strings:",
        '{"parameter_patch":{"componentsReplacement":{"mode":"replace","components":[{"type":"protein","sequence":"<PROTEIN_SEQUENCE>","numCopies":1,"useMsa":true},{"type":"ligand","sequence":"CN1C=NC2=C1C(=O)N(C)C(=O)N2C","numCopies":1,"inputMethod":"smiles"},{"type":"ligand","sequence":"NAD","numCopies":1,"inputMethod":"ccd"}],"clearConstraints":true}},"needs_confirmation":true,"execute_now":false}',
        "- replace with protein plus DNA/RNA components:",
        '{"parameter_patch":{"componentsReplacement":{"mode":"replace","components":[{"type":"protein","sequence":"<PROTEIN_SEQUENCE>","numCopies":1,"useMsa":true},{"type":"dna","sequence":"ATCGATCG","numCopies":1},{"type":"rna","sequence":"AUGCUU","numCopies":1}],"clearConstraints":true}},"needs_confirmation":true,"execute_now":false}',
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
    capability = str(candidate.get("capability") or "").strip().lower()
    missing_questions = candidate.get("missing_questions") if isinstance(candidate, dict) else []
    if capability == "clarification_needed" or (
        isinstance(missing_questions, list) and any(str(item or "").strip() for item in missing_questions)
    ):
        return []
    if capability == "task_deletion":
        return [
            {
                "id": "task_detail:delete_current",
                "label": "删除当前任务",
                "description": "确认删除当前正在查看的任务",
                "payload": {
                    "schemaVersion": ACTION_SCHEMA_VERSION,
                    "contextType": "task_detail",
                    "workflowKey": normalized_workflow,
                    "destructive": True,
                },
                "needs_confirmation": True,
                "execute_now": False,
            }
        ]
    if capability == "task_metadata_patch":
        raw_patch = candidate.get("metadata_patch") if isinstance(candidate.get("metadata_patch"), dict) else {}
        task_name = str(raw_patch.get("taskName") or raw_patch.get("name") or "").strip()
        task_summary = str(raw_patch.get("taskSummary") or raw_patch.get("summary") or "").strip()
        metadata_patch = {
            key: value
            for key, value in {
                "taskName": task_name,
                "taskSummary": task_summary,
            }.items()
            if value
        }
        if not metadata_patch:
            return []
        parts = []
        if task_name:
            parts.append(f"name: {task_name}")
        if task_summary:
            parts.append(f"description: {task_summary}")
        return [
            {
                "id": "task_detail:apply_metadata_patch",
                "label": "更新任务信息",
                "description": ", ".join(parts),
                "payload": {
                    "metadataPatch": metadata_patch,
                    "workflowKey": normalized_workflow,
                    "schemaVersion": ACTION_SCHEMA_VERSION,
                    "contextType": "task_detail",
                    "destructive": False,
                },
                "needs_confirmation": True,
                "execute_now": False,
            }
        ]
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
    content = str(user_content or "").lower()
    wants_submit = any(token in content for token in ["submit", "run", "rerun", "提交", "运行", "重跑"])
    intent = str(candidate.get("intent") or "").lower()
    wants_submit = wants_submit or any(token in intent for token in ["submit", "run", "rerun"])
    if wants_submit:
        return [
            {
                "id": "task_detail:apply_patch_and_submit",
                "label": "Confirm changes and run",
                "description": f"Apply {description}, then use the existing validated run action.",
                "payload": {
                    "parameterPatch": patch,
                    "workflowKey": normalized_workflow,
                    "schemaVersion": ACTION_SCHEMA_VERSION,
                    "contextType": "task_detail",
                    "destructive": False,
                },
                "needs_confirmation": True,
                "execute_now": False,
            }
        ]
    return [
        {
            "id": "task_detail:apply_parameter_patch",
            "label": "Confirm parameter changes",
            "description": description,
            "payload": {
                "parameterPatch": patch,
                "workflowKey": normalized_workflow,
                "schemaVersion": ACTION_SCHEMA_VERSION,
                "contextType": "task_detail",
                "destructive": False,
            },
            "needs_confirmation": True,
            "execute_now": False,
        }
    ]
