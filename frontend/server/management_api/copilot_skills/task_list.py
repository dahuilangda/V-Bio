from __future__ import annotations

from typing import Any, Dict


TASK_LIST_ACTION_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "tasks:open": {
        "label": "Open task",
        "description": "Navigate into a task's detail page. Requires the task ID from the visible task list.",
        "target_context": "task_detail",
        "payload_keys": ["taskRowId", "taskName"],
        "requires_payload": ["taskRowId"],
        "input_schema": {
            "type": "object",
            "properties": {
                "taskRowId": {"type": "string", "description": "Task ID from the visible list."},
                "taskName": {"type": "string"},
            },
            "required": ["taskRowId"],
            "additionalProperties": False,
        },
    },
    "tasks:create": {
        "label": "New task",
        "description": "Create a new task in the current project.",
        "target_context": "task_detail",
        "payload_keys": ["create"],
        "payload_defaults": {"create": True},
    },
    "tasks:create_with_sequence": {
        "label": "New task (with components)",
        "description": "Create a new task prefilled with a structured component list (protein / small molecule / DNA / RNA). Applies to structure-prediction and peptide-design tasks.",
        "target_context": "task_detail",
        "payload_keys": ["create", "components"],
        "requires_workflow": ["prediction", "peptide_design"],
        "requires_payload": ["components"],
        "payload_defaults": {"create": True},
        "input_schema": {
            "type": "object",
            "properties": {
                "create": {"type": "boolean", "const": True},
                "components": {
                    "type": "array",
                    "description": "Every structural component copied from the user request. Required for both protein-only and multi-component tasks.",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["protein", "ligand", "dna", "rna"],
                                "description": "Use ligand for small molecules, compounds, CCD IDs, and SMILES strings.",
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
            "required": ["create", "components"],
            "additionalProperties": False,
        },
    },
    "tasks:create_virtual_screening": {
        "label": "New virtual-screening task",
        "description": "Create a virtual-screening task: one or more protein target components plus a library of small-molecule compounds (by SMILES) to screen against the target.",
        "target_context": "task_detail",
        "payload_keys": ["create", "components", "screeningCompounds"],
        "requires_workflow": ["virtual_screening"],
        "requires_payload": ["components", "screeningCompounds"],
        "payload_defaults": {"create": True},
        "input_schema": {
            "type": "object",
            "properties": {
                "create": {"type": "boolean", "const": True},
                "components": {
                    "type": "array",
                    "description": "Target structural component(s): at least one protein. Fixed context ligands may be added here; the screened compound library goes in screeningCompounds.",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["protein", "ligand"],
                                "description": "Use ligand for a fixed context molecule (not the screened library). Virtual screening rejects DNA/RNA components.",
                            },
                            "sequence": {"type": "string", "description": "Protein/DNA/RNA sequence or ligand SMILES/CCD ID."},
                            "numCopies": {"type": "integer", "minimum": 1},
                            "useMsa": {"type": "boolean"},
                            "inputMethod": {"type": "string", "enum": ["smiles", "ccd"]},
                        },
                        "required": ["type", "sequence"],
                        "additionalProperties": False,
                    },
                },
                "screeningCompounds": {
                    "type": "array",
                    "description": "The compound library to screen: each entry is one small molecule by SMILES (optional name). Use one entry per compound; 1 to 200 entries.",
                    "minItems": 1,
                    "maxItems": 200,
                    "items": {
                        "type": "object",
                        "properties": {
                            "smiles": {"type": "string", "minLength": 1},
                            "name": {"type": "string"},
                        },
                        "required": ["smiles"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["create", "components", "screeningCompounds"],
            "additionalProperties": False,
        },
    },
    "tasks:create_docking": {
        "label": "New docking task (with target structure)",
        "description": (
            "Create a new docking task in the current project. A docking run consumes a protein "
            "target structure and a ligand SMILES — the target is a 3D STRUCTURE FILE, never an "
            "amino-acid sequence. Pass the chosen RCSB entry's pdbId as targetPdbId — the host "
            "fetches the entry's mmCIF file itself; never paste a URL from the record (sourceUrl "
            "is the human entry page, not a file). Only use targetStructureUrl for a cifUrl a "
            "skill observation explicitly returned. Pass the ligand SMILES when it is known. "
            "Docking projects only."
        ),
        "target_context": "task_detail",
        "payload_keys": [
            "create",
            "targetPdbId",
            "targetStructureUrl",
            "targetStructureName",
            "ligandSmiles",
            "taskName",
            "taskSummary",
        ],
        "requires_workflow": ["affinity"],
        "payload_defaults": {"create": True},
        "input_schema": {
            "type": "object",
            "properties": {
                "create": {"type": "boolean", "const": True},
                "targetPdbId": {
                    "type": "string",
                    "pattern": "^[0-9A-Za-z]{4}$",
                    "description": "The chosen RCSB entry's 4-character pdbId. The host downloads its mmCIF file.",
                },
                "targetStructureUrl": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 512,
                    "description": "Only for a non-RCSB source: a cifUrl explicitly returned by a skill observation.",
                },
                "targetStructureName": {"type": "string", "maxLength": 128},
                "ligandSmiles": {"type": "string", "maxLength": 2048},
                "taskName": {"type": "string", "maxLength": 128},
                "taskSummary": {"type": "string", "maxLength": 512},
            },
            "required": ["create"],
            "anyOf": [
                {"required": ["targetPdbId"]},
                {"required": ["targetStructureUrl"]},
            ],
            "additionalProperties": False,
        },
    },
    "tasks:copy": {
        "label": "Copy task to new draft",
        "description": "Select an existing task from the task list and copy it into a new draft, opened on the task-detail page for further edits. Parameter changes are separate operations on that page.",
        "target_context": "task_detail",
        "payload_keys": ["taskRowId", "taskName"],
        "requires_workflow": ["prediction"],
        "requires_payload": ["taskRowId"],
        "input_schema": {
            "type": "object",
            "properties": {
                "taskRowId": {
                    "type": "string",
                    "description": "ID copied exactly from the selected task row in the provided context.",
                },
                "taskName": {
                    "type": "string",
                    "description": "Optional new name for the copied draft.",
                },
            },
            "required": ["taskRowId"],
            "additionalProperties": False,
        },
    },
    "tasks:delete": {
        "label": "Delete task",
        "description": "Permanently delete a task. Requires the task ID.",
        "payload_keys": ["taskRowId", "taskName"],
        "requires_payload": ["taskRowId"],
        "destructive": True,
    },
    "tasks:rename": {
        "label": "Rename task",
        "description": "Change a task's name or description. Requires the task ID.",
        "payload_keys": ["taskRowId", "taskName", "taskSummary"],
        "requires_payload": ["taskRowId"],
        "requires_any_payload": ["taskName", "taskSummary"],
    },
    "tasks:cancel": {
        "label": "Cancel task",
        "description": "Stop a running or queued task. Requires the task ID.",
        "payload_keys": ["taskRowId", "taskName"],
        "requires_payload": ["taskRowId"],
        # Cancelling stops a run; it does not delete the task record — "execute" is the honest
        # effect (the default synthesis would mislabel destructive actions as "delete").
        "effect": "execute",
        "destructive": True,
    },
    "tasks:clear_filters": {
        "label": "Show all tasks",
        "description": "Clear task-list filters and restore the default sorting.",
        "payload_keys": ["stateFilter", "workflowFilter", "backendFilter", "sortKey", "clearAdvancedFilters", "clearSearch"],
        "payload_defaults": {
            "stateFilter": "all",
            "workflowFilter": "all",
            "backendFilter": "all",
            "sortKey": "submitted",
            "clearAdvancedFilters": True,
            "clearSearch": True,
        },
        "input_schema": {
            "type": "object",
            "properties": {
                "stateFilter": {"type": "string", "enum": ["all"]},
                "workflowFilter": {"type": "string", "enum": ["all"]},
                "backendFilter": {"type": "string", "enum": ["all"]},
                "sortKey": {"type": "string", "enum": ["submitted"]},
                "clearAdvancedFilters": {"type": "boolean", "const": True},
                "clearSearch": {"type": "boolean", "const": True},
            },
            "additionalProperties": False,
        },
    },
    "tasks:update_view": {
        "label": "Filter or sort tasks",
        "description": "Filter and sort the task list: search by text, filter by state / workflow / backend, sort by submitted time or metrics (pLDDT/ipTM/IPSAE/PAE), change page size. Pass only the fields the user wants to change.",
        "payload_keys": [
            "search",
            "stateFilter",
            "workflowFilter",
            "backendFilter",
            "sortKey",
            "sortDirection",
            "pageSize",
            "submittedWithinDays",
            "seedFilter",
            "failureOnly",
            "minPlddt",
            "minIptm",
            "maxPae",
            "visibleMetricColumns",
        ],
        "requires_any_payload": [
            "search",
            "stateFilter",
            "workflowFilter",
            "backendFilter",
            "sortKey",
            "sortDirection",
            "pageSize",
            "submittedWithinDays",
            "seedFilter",
            "failureOnly",
            "minPlddt",
            "minIptm",
            "maxPae",
            "visibleMetricColumns",
        ],
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Free text for the task search box."},
                "stateFilter": {"type": "string", "enum": ["all", "DRAFT", "QUEUED", "RUNNING", "SUCCESS", "FAILURE", "REVOKED"]},
                "workflowFilter": {"type": "string", "enum": ["all", "prediction", "virtual_screening", "affinity", "peptide_design", "lead_optimization"]},
                "backendFilter": {"type": "string", "description": "Backend token from context rows, or all."},
                "sortKey": {"type": "string", "enum": ["submitted", "plddt", "ipsae", "iptm", "pae", "backend", "seed", "mode"]},
                "sortDirection": {"type": "string", "enum": ["asc", "desc"]},
                "pageSize": {"type": "integer", "enum": [8, 12, 20, 50]},
                "submittedWithinDays": {"type": "string", "enum": ["all", "1", "7", "30", "90"]},
                "seedFilter": {"type": "string", "enum": ["all", "with_seed", "without_seed"]},
                "failureOnly": {"type": "boolean"},
                "minPlddt": {"type": "number", "minimum": 0, "maximum": 100},
                "minIptm": {"type": "number", "minimum": 0, "maximum": 1},
                "maxPae": {"type": "number", "minimum": 0},
                "visibleMetricColumns": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["plddt", "ipsae", "iptm", "pae"]},
                    "minItems": 1,
                    "uniqueItems": True,
                },
            },
            "additionalProperties": False,
        },
    },
}

