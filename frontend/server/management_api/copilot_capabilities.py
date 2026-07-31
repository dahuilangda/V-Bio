from __future__ import annotations

from typing import Any, Dict, List

from management_api.copilot_skill_harness import CopilotSkillDefinition
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
        "description": "Draft a parameter-change and submission plan for the current task/project using existing UI form state, or copy a visible task-list row into a new draft with a requested backend.",
        "trigger": "Commands to rerun, change seed/backend/mode/parameters, submit variants, copy a best-scoring visible task, or batch submit candidates.",
        "inputs": "Current draft/task parameters, visible task rows when in task_list, workflow, editable status, run disabled reason, requested changes.",
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
        "name": "virtual_screening.submit_plan",
        "description": "Plan Virtual Screening seed updates and reruns with the fixed Nesso-1 backend.",
        "trigger": "Virtual Screening rerun, seed change, or request to submit the current screening batch.",
        "inputs": "Current target sequence, compound library, seed, and run disabled reason.",
        "confirmation": "Always require explicit user confirmation before applying a seed or running.",
        "execution_boundary": "Nesso-1 is the only backend. Do not expose structure-prediction backend choices, templates, or component replacement patches.",
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


TASK_PARAMETER_SCHEMA: Dict[str, Dict[str, Any]] = {
    "backend": {"type": "enum", "values": ["boltz", "alphafold3", "protenix"]},
    "seed": {"type": "int", "min": 0, "max": 2147483647},
    "affinityMode": {"type": "enum", "values": ["score", "pose", "refine", "interface"]},
    "peptideDesignMode": {"type": "enum", "values": ["linear", "cyclic", "bicyclic"]},
    "peptideBinderLength": {"type": "int", "min": 1, "max": 200},
    "peptideIterations": {"type": "int", "min": 1, "max": 10000},
    "peptidePopulationSize": {"type": "int", "min": 1, "max": 10000},
    "peptideEliteSize": {"type": "int", "min": 1, "max": 10000},
    "peptideMutationRate": {"type": "float", "min": 0.0, "max": 1.0},
    "peptideUseInitialSequence": {"type": "bool"},
    "peptideInitialSequence": {"type": "string", "max_length": 200},
    "peptideSequenceMask": {"type": "string", "max_length": 200},
    "peptideBicyclicLinkerCcd": {"type": "enum", "values": ["SEZ", "29N", "BS3"]},
    "peptideBicyclicCysPositionMode": {"type": "enum", "values": ["auto", "manual"]},
    "peptideBicyclicFixTerminalCys": {"type": "bool"},
    "peptideBicyclicIncludeExtraCys": {"type": "bool"},
    "peptideBicyclicCys1Pos": {"type": "int", "min": 1, "max": 200},
    "peptideBicyclicCys2Pos": {"type": "int", "min": 1, "max": 200},
    "peptideBicyclicCys3Pos": {"type": "int", "min": 1, "max": 200},
    "componentsReplacement": {"type": "component_replacement"},
}

WORKFLOW_PARAMETER_KEYS: Dict[str, List[str]] = {
    "prediction": ["backend", "seed", "componentsReplacement"],
    "virtual_screening": ["backend", "seed"],
    "affinity": ["seed", "affinityMode"],
    "peptide_design": [
        "backend",
        "seed",
        "peptideDesignMode",
        "peptideBinderLength",
        "peptideIterations",
        "peptidePopulationSize",
        "peptideEliteSize",
        "peptideMutationRate",
        "peptideUseInitialSequence",
        "peptideInitialSequence",
        "peptideSequenceMask",
        "peptideBicyclicLinkerCcd",
        "peptideBicyclicCysPositionMode",
        "peptideBicyclicFixTerminalCys",
        "peptideBicyclicIncludeExtraCys",
        "peptideBicyclicCys1Pos",
        "peptideBicyclicCys2Pos",
        "peptideBicyclicCys3Pos",
    ],
    "lead_optimization": [],
}


def build_registered_capability_catalog() -> Dict[str, Any]:
    """Build a read-only catalog directly from the registered capability schemas."""

    return {
        "operations": [dict(capability) for capability in COPILOT_CAPABILITIES],
        "workflows": [
            {
                "workflow": workflow,
                "parameters": [
                    {
                        "field": field,
                        "schema": dict(TASK_PARAMETER_SCHEMA[field]),
                    }
                    for field in fields
                    if field in TASK_PARAMETER_SCHEMA
                ],
            }
            for workflow, fields in WORKFLOW_PARAMETER_KEYS.items()
        ],
    }


def _backend_values_for_workflow(workflow_key: str) -> List[str]:
    if normalize_workflow_key(workflow_key) == "virtual_screening":
        return ["nesso"]
    return list(TASK_PARAMETER_SCHEMA["backend"]["values"])


def _task_parameter_json_schema(workflow_key: str) -> Dict[str, Any]:
    normalized_workflow = normalize_workflow_key(workflow_key) if str(workflow_key or "").strip() else ""
    properties: Dict[str, Any] = {}
    component_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "type": {"type": "string", "enum": ["protein", "dna", "rna", "ligand"]},
            "sequence": {"type": "string", "minLength": 1},
            "numCopies": {"type": "integer", "minimum": 1},
            "useMsa": {"type": "boolean"},
            "cyclic": {"type": "boolean"},
            "inputMethod": {"type": "string", "enum": ["smiles", "ccd"]},
        },
        "required": ["type", "sequence"],
        "additionalProperties": False,
    }
    for key in WORKFLOW_PARAMETER_KEYS.get(normalized_workflow, []):
        spec = TASK_PARAMETER_SCHEMA[key]
        if spec["type"] == "enum":
            values = _backend_values_for_workflow(normalized_workflow) if key == "backend" else spec["values"]
            properties[key] = {"type": "string", "enum": values}
        elif spec["type"] == "bool":
            properties[key] = {"type": "boolean"}
        elif spec["type"] == "string":
            properties[key] = {"type": "string", "minLength": 1, "maxLength": spec["max_length"]}
        elif spec["type"] == "int":
            properties[key] = {"type": "integer", "minimum": spec["min"], "maximum": spec["max"]}
        elif spec["type"] == "float":
            properties[key] = {"type": "number", "minimum": spec["min"], "maximum": spec["max"]}
        elif spec["type"] == "component_replacement":
            properties[key] = {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "const": "replace"},
                    "components": {"type": "array", "minItems": 1, "items": component_schema},
                    "clearConstraints": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["mode", "components"],
                "additionalProperties": False,
            }
    return {
        "type": "object",
        "properties": properties,
        "minProperties": 1,
        "additionalProperties": False,
    }


def build_task_detail_skill_definitions(workflow_key: str) -> List[CopilotSkillDefinition]:
    """Return atomic host operations for the current task-detail workflow."""

    normalized_workflow = normalize_workflow_key(workflow_key) if str(workflow_key or "").strip() else ""
    empty_input = {"type": "object", "properties": {}, "additionalProperties": False}
    definitions = [
        CopilotSkillDefinition(
            name="task_detail:submit_current",
            label="Start run",
            description="Submit the task through the current page's validation path.",
            input_schema=empty_input,
            effect="execute",
            context_type="task_detail",
        ),
        CopilotSkillDefinition(
            name="task_detail:save_draft",
            label="Save draft",
            description="Save the current task draft.",
            input_schema=empty_input,
            effect="update",
            context_type="task_detail",
        ),
        CopilotSkillDefinition(
            name="task_detail:cancel_current",
            label="Cancel current task",
            description="Cancel the current running or queued task.",
            input_schema=empty_input,
            effect="update",
            context_type="task_detail",
            destructive=True,
        ),
        CopilotSkillDefinition(
            name="task_detail:delete_current",
            label="Delete current task",
            description="Delete the current task record.",
            input_schema=empty_input,
            effect="delete",
            context_type="task_detail",
            destructive=True,
        ),
        CopilotSkillDefinition(
            name="task_detail:apply_metadata_patch",
            label="Update task metadata",
            description="Update the current task's metadata.",
            input_schema={
                "type": "object",
                "properties": {
                    "metadataPatch": {
                        "type": "object",
                        "properties": {
                            "taskName": {"type": "string", "minLength": 1},
                            "taskSummary": {"type": "string"},
                        },
                        "minProperties": 1,
                        "additionalProperties": False,
                    }
                },
                "required": ["metadataPatch"],
                "additionalProperties": False,
            },
            effect="update",
            context_type="task_detail",
        ),
        CopilotSkillDefinition(
            name="task_detail:apply_copilot_attachments",
            label="Apply uploaded files",
            description="Apply files uploaded in the current conversation using their declared roles.",
            input_schema={
                "type": "object",
                "properties": {
                    "attachmentApplications": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "attachmentId": {"type": "string", "minLength": 1},
                                "fileName": {"type": "string", "minLength": 1},
                                "role": {"type": "string", "enum": ["target", "ligand", "template"]},
                            },
                            "required": ["attachmentId", "fileName", "role"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["attachmentApplications"],
                "additionalProperties": False,
            },
            effect="update",
            context_type="task_detail",
        ),
        CopilotSkillDefinition(
            name="task_detail:apply_structure_template",
            label="Apply structure as template",
            description=(
                "Fetch a structure file from a URL and attach it as the template for the protein component of the "
                "current prediction task. Pass the structureUrl returned by a prior rcsb.resolve / rcsb.search / "
                "alphafold.resolve observation (its pdbUrl or cifUrl); the URL must point to a .pdb, .cif, or .mmcif "
                "file. Prediction tasks only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "structureUrl": {"type": "string", "minLength": 1, "maxLength": 512},
                    "fileName": {"type": "string", "minLength": 1, "maxLength": 128},
                },
                "required": ["structureUrl"],
                "additionalProperties": False,
            },
            effect="update",
            context_type="task_detail",
        ),
        CopilotSkillDefinition(
            name="task_detail:apply_affinity_target_structure",
            label="Apply structure as affinity target",
            description=(
                "Fetch a structure file from a URL and set it as the target/receptor structure for the current "
                "affinity task. Pass the structureUrl from a prior rcsb.resolve / rcsb.search / alphafold.resolve "
                "observation (pdbUrl or cifUrl); the URL must point to a .pdb, .cif, or .mmcif file. Affinity tasks "
                "only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "structureUrl": {"type": "string", "minLength": 1, "maxLength": 512},
                    "fileName": {"type": "string", "minLength": 1, "maxLength": 128},
                },
                "required": ["structureUrl"],
                "additionalProperties": False,
            },
            effect="update",
            context_type="task_detail",
        ),
        CopilotSkillDefinition(
            name="task_detail:apply_affinity_ligand_smiles",
            label="Set affinity binder (SMILES)",
            description=(
                "Set the binder / ligand for the current affinity task from a SMILES string. Pass the smiles from a "
                "prior pubchem.search observation. Affinity tasks only."
            ),
            input_schema={
                "type": "object",
                "properties": {"smiles": {"type": "string", "minLength": 1, "maxLength": 2048}},
                "required": ["smiles"],
                "additionalProperties": False,
            },
            effect="update",
            context_type="task_detail",
        ),
    ]
    parameter_schema = _task_parameter_json_schema(normalized_workflow)
    if parameter_schema["properties"]:
        definitions.append(
            CopilotSkillDefinition(
                name="task_detail:apply_parameter_patch",
                label="Update task parameters",
                description="Update the current task's structured parameters.",
                input_schema={
                    "type": "object",
                    "properties": {"parameterPatch": parameter_schema},
                    "required": ["parameterPatch"],
                    "additionalProperties": False,
                },
                effect="update",
                context_type="task_detail",
            )
        )
    return definitions
