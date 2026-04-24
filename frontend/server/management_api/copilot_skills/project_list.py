from __future__ import annotations

from typing import Any, Dict


PROJECT_LIST_ACTION_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "projects:create": {
        "label": "新建项目",
        "description": "打开新建项目窗口。",
        "payload_keys": ["create"],
        "payload_defaults": {"create": True},
        "input_schema": {
            "type": "object",
            "properties": {"create": {"type": "boolean", "const": True}},
            "required": ["create"],
            "additionalProperties": False,
        },
    },
    "projects:open": {
        "label": "打开项目",
        "description": "打开匹配的项目。",
        "payload_keys": ["projectId", "projectName"],
        "requires_payload": ["projectId"],
        "intent_keywords": ["open", "show", "view", "打开", "进入", "查看"],
        "input_schema": {
            "type": "object",
            "properties": {
                "projectId": {"type": "string", "description": "ID copied exactly from context_payload.projects[].id."},
                "projectName": {"type": "string", "description": "Human readable project name from context."},
            },
            "required": ["projectId"],
            "additionalProperties": False,
        },
    },
    "projects:delete": {
        "label": "删除项目",
        "description": "删除匹配的项目。",
        "payload_keys": ["projectId", "projectName"],
        "requires_payload": ["projectId"],
        "destructive": True,
        "intent_keywords": ["delete", "remove", "删除", "删掉", "移除"],
        "input_schema": {
            "type": "object",
            "properties": {
                "projectId": {"type": "string", "description": "ID copied exactly from context_payload.projects[].id."},
                "projectName": {"type": "string"},
            },
            "required": ["projectId"],
            "additionalProperties": False,
        },
    },
    "projects:cancel_active": {
        "label": "取消项目运行",
        "description": "取消匹配项目中的 active runtime 任务。",
        "payload_keys": ["projectId", "projectName"],
        "requires_payload": ["projectId"],
        "destructive": True,
        "requires_active_project": True,
        "intent_keywords": ["cancel", "stop", "terminate", "停止", "取消", "终止"],
        "input_schema": {
            "type": "object",
            "properties": {
                "projectId": {"type": "string", "description": "ID copied exactly from context_payload.projects[].id."},
                "projectName": {"type": "string"},
            },
            "required": ["projectId"],
            "additionalProperties": False,
        },
    },
    "projects:failed": {
        "label": "失败项目",
        "description": "筛选包含失败任务的项目。",
        "payload_keys": ["activityFilter"],
        "payload_defaults": {"activityFilter": "failed"},
        "input_schema": {
            "type": "object",
            "properties": {"activityFilter": {"type": "string", "enum": ["failed"]}},
            "required": ["activityFilter"],
            "additionalProperties": False,
        },
    },
    "projects:active": {
        "label": "运行中项目",
        "description": "筛选包含排队或运行任务的项目。",
        "payload_keys": ["activityFilter"],
        "payload_defaults": {"activityFilter": "active"},
        "input_schema": {
            "type": "object",
            "properties": {"activityFilter": {"type": "string", "enum": ["active"]}},
            "required": ["activityFilter"],
            "additionalProperties": False,
        },
    },
    "projects:workflow_prediction": {
        "label": "Prediction 项目",
        "description": "筛选结构预测 workflow 项目。",
        "payload_keys": ["workflowFilter"],
        "payload_defaults": {"workflowFilter": "prediction"},
    },
    "projects:workflow_affinity": {
        "label": "Affinity 项目",
        "description": "筛选 Affinity Scoring workflow 项目。",
        "payload_keys": ["workflowFilter"],
        "payload_defaults": {"workflowFilter": "affinity"},
    },
    "projects:workflow_peptide_design": {
        "label": "Peptide Design 项目",
        "description": "筛选 Peptide Design workflow 项目。",
        "payload_keys": ["workflowFilter"],
        "payload_defaults": {"workflowFilter": "peptide_design"},
    },
    "projects:workflow_lead_optimization": {
        "label": "Lead Optimization 项目",
        "description": "筛选 Lead Optimization workflow 项目。",
        "payload_keys": ["workflowFilter"],
        "payload_defaults": {"workflowFilter": "lead_optimization"},
    },
    "projects:updated_desc": {
        "label": "最新更新",
        "description": "按最近更新时间降序排序。",
        "payload_keys": ["sortBy"],
        "payload_defaults": {"sortBy": "updated_desc"},
    },
    "projects:updated_asc": {
        "label": "最早更新",
        "description": "按最近更新时间升序排序。",
        "payload_keys": ["sortBy"],
        "payload_defaults": {"sortBy": "updated_asc"},
    },
    "projects:backend_boltz": {
        "label": "Boltz 项目",
        "description": "筛选 Boltz backend 项目。",
        "payload_keys": ["backendFilter"],
        "payload_defaults": {"backendFilter": "boltz"},
    },
}
