from __future__ import annotations

from typing import Any, Dict, List

from management_api.copilot_skill_harness import CopilotSkillDefinition
from management_api.copilot_skills.context_actions import build_context_skill_definitions
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
        "trigger": "Requests to filter, sort, or summarize the project portfolio.",
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
        "trigger": "Requests to filter, sort, or summarize a project's task list.",
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
        "name": "task_creation_planning",
        "description": "Plan creating a new task from the task list with prefilled inputs, matched to the project workflow's accepted input types.",
        "trigger": "User asks to start a new task or to fill a task input while on a task list page.",
        "inputs": "Visible task rows, project workflow, retrieved inputs.",
        "confirmation": "Always confirm the create action; resolve the new-vs-existing-task choice before it.",
        "execution_boundary": "Prefill only through the workflow's create action; inputs must match the workflow's accepted input types.",
    },
    {
        "name": "task_submission_planning",
        "description": "Draft a parameter-change and submission plan for the current task/project using existing UI form state, or copy a visible task-list row into a new draft and adjust it on the task-detail page.",
        "trigger": "Commands to rerun, change seed/backend/mode/parameters, submit variants, copy a visible task row, or submit confirmed tasks.",
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
        "execution_boundary": "Allowed patch keys: backend, seed, affinityBinding, componentsReplacement. Execution uses the existing Prediction Run path.",
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
        "description": "Plan Docking workflow mode/seed updates and submission.",
        "trigger": "Docking (dock mode) request, mode change, seed change, or submit request. Dock is the default mode: protein structure plus ligand SMILES, docking and affinity computed together.",
        "inputs": "Current Docking draft, target structure / ligand SMILES state, affinityMode, seed, run disabled reason.",
        "confirmation": "Always require explicit user confirmation before applying mode/seed or running.",
        "execution_boundary": "Allowed patch keys: seed, affinityMode (dock is default). Execution uses the existing Docking Run path.",
    },
    {
        "name": "peptide_design.submit_plan",
        "description": "Plan Peptide Design runtime option updates and submission.",
        "trigger": "Peptide chirality (L/D), design mode, length window, NCAA pool, pocket residues, iterations, population, elite size, seed, or submit request.",
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
    "backend": {"type": "enum", "values": ["boltz", "alphafold3", "protenix", "protenix2dock", "boltz2dock"]},
    "seed": {"type": "int", "min": 0, "max": 2147483647},
    "affinityMode": {
        "type": "enum",
        "values": ["dock", "score", "pose", "refine", "interface"],
        "description": (
            "dock is the DEFAULT mode and what a docking request means: it takes a protein structure "
            "plus a ligand SMILES, places the ligand in the binding site, and computes the binding "
            "pose and affinity together — no ligand structure file is uploaded, but a pocket search "
            "box must be set before the run. The pose / refine / interface modes consume separately "
            "uploaded target and ligand structure files. The score mode requires the uploaded target "
            "structure only — no ligand file and no ligand SMILES."
        ),
    },
    "affinityBinding": {"type": "affinity_binding"},
    "lowVram": {"type": "bool"},
    "peptideDesignMode": {"type": "enum", "values": ["linear", "cyclic", "bicyclic"]},
    "peptideChirality": {
        "type": "enum", "values": ["l", "d"],
        "description": (
            "d designs D-peptides via the mirror workflow: the target is "
            "mirrored x->-x into a D-target, the candidate L-peptide is "
            "placed at the pocket and refined against the pinned receptor, "
            "then the product is flipped back to L-target + D-peptide. "
            "Works with linear, cyclic and bicyclic modes; requires a "
            "docking backend (protenix2dock or boltz2dock)."
        ),
    },
    "peptideBinderLength": {"type": "int", "min": 5, "max": 120},
    "peptideLengthMin": {"type": "int", "min": 5, "max": 120},
    "peptideLengthMax": {"type": "int", "min": 5, "max": 120},
    "peptideIterations": {"type": "int", "min": 1, "max": 200},
    "peptidePopulationSize": {"type": "int", "min": 1, "max": 200},
    "peptideEliteSize": {"type": "int", "min": 1, "max": 200},
    "peptideUseInitialSequence": {"type": "bool"},
    "peptideInitialSequence": {"type": "string", "max_length": 200},
    "peptideSequenceMask": {"type": "string", "max_length": 200},
    "peptideNonNaturalMin": {"type": "int", "min": 0, "max": 120},
    "peptideNonNaturalMax": {"type": "int", "min": 0, "max": 120},
    "peptideNcaaDecodeBias": {"type": "float", "min": 0.0, "max": 1.0},
    "peptidePocketResidues": {
        "type": "string", "max_length": 500,
        "description": (
            "Receptor pocket residues in author numbering of the uploaded "
            "target structure, e.g. 'A:54,A:61,A:67'. Requires an uploaded "
            "target structure; empty means whole-surface design."
        ),
    },
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
    "prediction": ["backend", "seed", "affinityBinding", "lowVram", "componentsReplacement"],
    "virtual_screening": ["backend", "seed"],
    "affinity": ["backend", "seed", "affinityMode"],
    "peptide_design": [
        "backend",
        "seed",
        "peptideDesignMode",
        "peptideChirality",
        "peptideBinderLength",
        "peptideLengthMin",
        "peptideLengthMax",
        "peptideIterations",
        "peptidePopulationSize",
        "peptideEliteSize",
        "peptideNonNaturalMin",
        "peptideNonNaturalMax",
        "peptideNcaaDecodeBias",
        "peptidePocketResidues",
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

# The input TYPES each workflow's task accepts — environment facts the planner reads alongside
# the context, so a field's accepted type is known at plan time, not discovered from a skill
# description mid-round. Type declarations only; sourcing and ordering stay the model's to derive.
WORKFLOW_INPUT_CONTRACT: Dict[str, Dict[str, str]] = {
    "prediction": {
        "components": "amino-acid or nucleic-acid sequences; ligands as CCD codes or SMILES",
    },
    "virtual_screening": {
        "target": "protein sequence (protein and ligand components only — DNA/RNA are rejected; standard 20 residues only)",
        "library": "small-molecule SMILES list (at most 200 compounds per run)",
    },
    "affinity": {
        "target": "3D structure file (.pdb / .cif / .mmcif)",
        "ligand": "mode-dependent: dock takes a ligand SMILES and requires a pocket search box; pose/refine/interface take an uploaded ligand structure file; score requires no ligand input",
    },
    "peptide_design": {
        "target": "protein sequence",
        "binder": "designed peptide (sequence generated by the workflow)",
    },
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


def build_capability_orientation(
    capabilities: List[Dict[str, str]] = COPILOT_CAPABILITIES,
    *,
    max_items: int = 14,
    per_item_chars: int = 95,
) -> str:
    """Compact, general orientation to V-Bio's capability surface, derived from the registered
    catalog (single source of truth).

    Used by the inline auto-completer so its prompt never hardcodes or drifts from the planner's
    catalog, and never bakes in specific compounds/proteins/examples (which overfit). Domain-
    general by construction: it only reshapes the catalog's own descriptions.
    """
    lines: List[str] = []
    for capability in capabilities[:max_items]:
        name = str(capability.get("name") or "").strip()
        description = str(capability.get("description") or capability.get("trigger") or "").strip()
        description = " ".join(description.split())
        if not name and not description:
            continue
        if len(description) > per_item_chars:
            description = description[:per_item_chars].rstrip() + "…"
        lines.append(f"- {name}: {description}" if name else f"- {description}")
    return "\n".join(lines)


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
            "inputMethod": {"type": "string", "enum": ["smiles", "ccd", "jsme"]},
        },
        "required": ["type", "sequence"],
        "additionalProperties": False,
    }
    for key in WORKFLOW_PARAMETER_KEYS.get(normalized_workflow, []):
        spec = TASK_PARAMETER_SCHEMA[key]
        # A declared description rides along into the generated property schema — the planner
        # reads parameter semantics (e.g. which mode is the default and what it requires) from
        # the schema itself, not from guesses.
        schema_description = str(spec.get("description") or "").strip()
        if spec["type"] == "enum":
            values = _backend_values_for_workflow(normalized_workflow) if key == "backend" else spec["values"]
            properties[key] = {"type": "string", "enum": values}
            if schema_description:
                properties[key]["description"] = schema_description
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
        elif spec["type"] == "affinity_binding":
            properties[key] = {
                "type": "object",
                "description": (
                    "Enable or disable binding/affinity computation on this task. Set enabled=true to turn on "
                    "affinity scoring; assign chain IDs to target and ligand/binder to declare which components "
                    "the affinity is computed between. The component chain IDs come from the task's component "
                    "list in context_payload."
                ),
                "properties": {
                    "enabled": {"type": "boolean"},
                    "target": {"type": "string", "description": "Chain ID of the receptor/target component."},
                    "ligand": {"type": "string", "description": "Chain ID of the ligand component."},
                    "binder": {"type": "string", "description": "Chain ID of the binder component (alias for ligand)."},
                },
                "required": ["enabled"],
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
            name="task_detail:create_new_task",
            label="New task",
            description=(
                "Create a NEW task in the current project, prefilled with the target components and "
                "an optional name/summary. Use when the user asks to start a new task or create "
                "another one from the current task's target. The host navigates to the new-task "
                "page and applies the provided components (and metadata) to the draft. When the "
                "components' data must be fetched first, emit this operation in the SAME round as "
                "the lookups, consuming their results via $fromObservation plus depends_on — the "
                "harness executes the reads and completes the action from them automatically."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "taskName": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "description": "Optional name for the new task.",
                    },
                    "taskSummary": {
                        "type": "string",
                        "maxLength": 512,
                        "description": "Optional summary for the new task.",
                    },
                    "components": {
                        "type": "array",
                        "description": "Every structural component for the new task. Copy the target protein component from the current task when the user says the target is the current one.",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["protein", "ligand", "dna", "rna"],
                                    "description": "Component type. Use ligand for small molecules, CCD IDs, and SMILES strings.",
                                },
                                "sequence": {
                                    "type": "string",
                                    "description": "Protein/DNA/RNA sequence, ligand SMILES, or ligand CCD ID.",
                                },
                                "numCopies": {"type": "integer", "minimum": 1},
                                "useMsa": {"type": "boolean"},
                                "inputMethod": {"type": "string", "enum": ["smiles", "ccd"]},
                            },
                            "required": ["type", "sequence"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["components"],
                "additionalProperties": False,
            },
            effect="create",
            context_type="task_detail",
            target_context="task_detail",
        ),
        CopilotSkillDefinition(
            name="task_detail:submit_current",
            label="Start run",
            description="Submit the current task for execution.",
            input_schema=empty_input,
            effect="execute",
            context_type="task_detail",
        ),
        CopilotSkillDefinition(
            name="task_detail:save_draft",
            label="Save draft",
            description="Save the current task configuration as a draft.",
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
            description="Update the current task's name or description.",
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
    ]
    # The structure-template skill is prediction-only (the handler enforces this). Registering it only
    # for prediction avoids the planner proposing it on affinity/peptide/lead_opt where it would error.
    if normalized_workflow == "prediction":
        definitions.append(
                CopilotSkillDefinition(
                    name="task_detail:apply_structure_template",
                    label="Apply structure as template",
                    description=(
                        "Fetch a structure file from a URL and attach it as the template for the protein component of the "
                        "current prediction task. Pass the chosen RCSB entry's pdbId as structurePdbId — the host "
                        "downloads its mmCIF file itself. For an alphafold.resolve model pass its cifUrl as "
                        "structureUrl; never paste the record's sourceUrl (an entry page, not a file). The URL "
                        "must be a cifUrl or pdbUrl explicitly returned by a skill observation, pointing to a "
                        ".pdb, .cif, or .mmcif file. When the structure search returned several entries, apply "
                        "only the entry the user chose. Prediction tasks only."
                    ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "structurePdbId": {
                            "type": "string",
                            "pattern": "^[0-9A-Za-z]{4}$",
                            "description": "The chosen RCSB entry's pdbId — the host downloads its mmCIF file. Preferred over a URL.",
                        },
                        "structureUrl": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 512,
                            "description": "Only for a non-RCSB source: a cifUrl explicitly returned by a skill observation.",
                        },
                        "fileName": {"type": "string", "minLength": 1, "maxLength": 128},
                    },
                    "required": [],
                    "anyOf": [
                        {"required": ["structurePdbId"]},
                        {"required": ["structureUrl"]},
                    ],
                    "additionalProperties": False,
                },
                effect="update",
                context_type="task_detail",
            )
        )
    # The docking-specific skills (target structure / ligand SMILES) belong only to the docking
    # workflow. Prediction tasks enable affinity via the affinityBinding parameter on
    # apply_parameter_patch instead, so these skills are withheld for other workflows to avoid
    # the model proposing a path the host blocks.
    if normalized_workflow == "affinity":
        definitions.extend(
            [
                CopilotSkillDefinition(
                    name="task_detail:apply_docking_target_structure",
                    label="Apply structure as docking target",
                    description=(
                        "Set the protein target/receptor of the current docking task by fetching a structure "
                        "file from a URL or RCSB entry id. The target of this workflow is a STRUCTURE FILE, never a bare "
                        "amino-acid sequence: when the user names the target by protein and/or organism, "
                        "source it with rcsb.search first (experimental structures — when several entries "
                        "match, ask the user to choose) or alphafold.resolve when a predicted model is "
                        "acceptable, then pass the chosen entry's pdbId as structurePdbId (preferred — the host "
                        "downloads its mmCIF itself) or its cifUrl as structureUrl. The URL must point "
                        "to a .pdb, .cif, or .mmcif file. Docking tasks only."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "structurePdbId": {
                                "type": "string",
                                "pattern": "^[0-9A-Za-z]{4}$",
                                "description": "The chosen RCSB entry's pdbId — the host downloads its mmCIF file. Preferred over a URL.",
                            },
                            "structureUrl": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 512,
                                "description": "Only for a non-RCSB source: a cifUrl explicitly returned by a skill observation.",
                            },
                            "fileName": {"type": "string", "minLength": 1, "maxLength": 128},
                        },
                        "required": [],
                        "anyOf": [
                            {"required": ["structurePdbId"]},
                            {"required": ["structureUrl"]},
                        ],
                        "additionalProperties": False,
                    },
                    effect="update",
                    context_type="task_detail",
                ),
                CopilotSkillDefinition(
                    name="task_detail:set_docking_pocket_box",
                    label="Set docking pocket box",
                    description=(
                        "Set the dock-mode search box (pocket) on the uploaded target structure. "
                        "mode 'auto' (preferred): the host detects co-crystallized ligands and "
                        "boxes the largest one's site — that IS the binding pocket; when the "
                        "structure has no ligand it boxes the whole protein (large box, blind "
                        "docking). mode 'protein': always the whole-protein box. Dock mode cannot "
                        "run without a pocket (runBlockedReason names it) — chain this before "
                        "task_detail:submit_current via depends_on when no pocket exists yet. "
                        "Docking tasks only."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "mode": {
                                "type": "string",
                                "enum": ["auto", "protein"],
                                "description": "'auto': ligand pocket if present else whole protein. 'protein': whole protein.",
                            },
                        },
                        "required": [],
                        "additionalProperties": False,
                    },
                    effect="update",
                    context_type="task_detail",
                ),
                CopilotSkillDefinition(
                    name="task_detail:apply_docking_ligand_smiles",
                    label="Set docking ligand (SMILES)",
                    description=(
                        "Set the ligand of the current docking task from a SMILES string. In dock mode "
                        "(the default) the ligand is defined by SMILES alone — no ligand structure file "
                        "is uploaded; the docking run places the ligand and computes the binding pose and "
                        "affinity together. Pass the smiles from a prior pubchem.search observation, or "
                        "the SMILES the user provided directly — no lookup is needed when the user "
                        "already gave a SMILES. Docking tasks only."
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
        )
    parameter_schema = _task_parameter_json_schema(normalized_workflow)
    if parameter_schema["properties"]:
        # The description must name EXACTLY the keys this workflow's schema declares — a generic
        # "(backend, seed, …, components, etc.)" list lets the planner send a key the schema (and
        # the host) rejects for this workflow, burning the whole round budget on rejections.
        parameter_keys = ", ".join(sorted(parameter_schema["properties"].keys()))
        definitions.append(
            CopilotSkillDefinition(
                name="task_detail:apply_parameter_patch",
                label="Update task parameters",
                description=(
                    "Update the current task's runtime parameters via a parameterPatch object. For THIS "
                    f"workflow the patch supports exactly these keys: {parameter_keys}. Any other key is "
                    "rejected by the schema audit."
                ),
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


def build_cross_context_skill_definitions(
    *,
    current_context: str,
    context_payload: Dict[str, Any],
    workflow_key: str,
) -> List[CopilotSkillDefinition]:
    """Return skills for the current host page only (progressive disclosure).

    The catalog is kept small so a weaker model can reliably pick the right operation: only the
    action skills for the page the user is on are exposed, plus all read-only lookup skills (those
    are universal). When the user confirms an action that navigates to another page, the next page's
    Copilot turn sees that page's action skills — so a multi-step task advances page by page, each
    step planned with only the relevant tools visible.
    """
    if current_context == "task_detail":
        return build_task_detail_skill_definitions(workflow_key)
    return build_context_skill_definitions(current_context, context_payload, workflow_key=workflow_key)
