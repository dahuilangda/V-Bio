from __future__ import annotations

from typing import Any, Dict


TASK_LIST_ACTION_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "tasks:open": {
        "label": "打开任务",
        "description": "打开匹配的任务。",
        "payload_keys": ["taskRowId", "taskName"],
        "requires_payload": ["taskRowId"],
        "intent_keywords": ["open", "show", "view", "打开", "进入", "查看"],
        "input_schema": {
            "type": "object",
            "properties": {
                "taskRowId": {"type": "string", "description": "ID copied exactly from context_payload.rows[].id."},
                "taskName": {"type": "string"},
            },
            "required": ["taskRowId"],
            "additionalProperties": False,
        },
    },
    "tasks:create": {
        "label": "新建任务",
        "description": "在当前项目中新建任务。",
        "payload_keys": ["create"],
        "payload_defaults": {"create": True},
    },
    "tasks:create_with_sequence": {
        "label": "新建预测任务",
        "description": "创建包含结构化组件列表的新预测任务。",
        "payload_keys": ["create", "components"],
        "requires_workflow": ["prediction"],
        "requires_payload": ["components"],
        "payload_defaults": {"create": True},
        "intent_keywords": ["predict", "submit", "run", "create", "预测", "提交", "运行", "新建", "创建"],
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
    "tasks:delete": {
        "label": "删除任务",
        "description": "删除匹配的任务记录。",
        "payload_keys": ["taskRowId", "taskName"],
        "requires_payload": ["taskRowId"],
        "destructive": True,
        "intent_keywords": ["delete", "remove", "删除", "删掉", "移除"],
    },
    "tasks:rename": {
        "label": "重命名任务",
        "description": "更新匹配任务的名称或描述。",
        "payload_keys": ["taskRowId", "taskName", "taskSummary"],
        "requires_payload": ["taskRowId"],
        "intent_keywords": ["rename", "name", "description", "summary", "重命名", "改名", "描述", "说明"],
    },
    "tasks:cancel": {
        "label": "取消任务",
        "description": "取消或终止匹配的运行/排队任务。",
        "payload_keys": ["taskRowId", "taskName"],
        "requires_payload": ["taskRowId"],
        "destructive": True,
        "requires_active_task": True,
        "intent_keywords": ["cancel", "stop", "terminate", "停止", "取消", "终止"],
    },
    "tasks:failure": {"label": "失败任务", "description": "筛选 FAILURE 任务。", "payload_keys": ["stateFilter"], "payload_defaults": {"stateFilter": "FAILURE"}},
    "tasks:running": {"label": "运行任务", "description": "筛选 RUNNING 任务。", "payload_keys": ["stateFilter"], "payload_defaults": {"stateFilter": "RUNNING"}},
    "tasks:queued": {"label": "排队任务", "description": "筛选 QUEUED 任务。", "payload_keys": ["stateFilter"], "payload_defaults": {"stateFilter": "QUEUED"}},
    "tasks:success": {"label": "成功任务", "description": "筛选 SUCCESS 任务。", "payload_keys": ["stateFilter"], "payload_defaults": {"stateFilter": "SUCCESS"}},
    "tasks:submitted": {"label": "提交时间", "description": "按提交时间排序。", "payload_keys": ["sortKey"], "payload_defaults": {"sortKey": "submitted"}},
    "tasks:sort_plddt": {"label": "按 pLDDT 排序", "description": "按 pLDDT 从高到低排序。", "payload_keys": ["sortKey"], "payload_defaults": {"sortKey": "plddt"}},
    "tasks:sort_iptm": {"label": "按 ipTM 排序", "description": "按 ipTM 从高到低排序。", "payload_keys": ["sortKey"], "payload_defaults": {"sortKey": "iptm"}},
    "tasks:sort_ipsae": {"label": "按 IPSAE 排序", "description": "按 IPSAE 从高到低排序。", "payload_keys": ["sortKey"], "payload_defaults": {"sortKey": "ipsae"}},
    "tasks:sort_pae": {"label": "按 PAE 排序", "description": "按 PAE 从低到高排序。", "payload_keys": ["sortKey"], "payload_defaults": {"sortKey": "pae"}},
    "tasks:backend_boltz": {"label": "Boltz 任务", "description": "筛选 Boltz backend 任务。", "payload_keys": ["backendFilter"], "payload_defaults": {"backendFilter": "boltz"}},
}
