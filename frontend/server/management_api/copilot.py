from __future__ import annotations

import json
import re
from typing import Any, Dict, List

import requests

from management_api.copilot_capabilities import (
    build_context_actions,
    build_task_submission_actions,
    infer_workflow_key,
    render_capability_prompt,
    render_context_plan_schema_prompt,
)


def compact_text(value: Any, limit: int = 1800) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def strip_internal_capability_lines(content: str) -> str:
    cleaned_lines: List[str] = []
    internal_label_pattern = re.compile(
        r"^\s*(?:[*_`#>\-\s]*)(?:能力使用|能力调用|capability\s*(?:used|call)?|tool\s*(?:used|call)?)\s*[:：]",
        re.IGNORECASE,
    )
    internal_id_pattern = re.compile(
        r"\b(?:project_list_analysis|project_list_filter_sort|task_list_analysis|task_list_filter_sort|task_result_analysis|task_submission_planning|prediction\.submit_plan|affinity\.submit_plan|peptide_design\.submit_plan|lead_optimization\.submit_plan)\b"
    )
    for line in str(content or "").splitlines():
        if internal_label_pattern.search(line) or internal_id_pattern.search(line):
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


class CopilotAssistant:
    def __init__(
        self,
        *,
        chat_api_url: str,
        chat_api_key: str,
        chat_model: str,
        timeout_seconds: float,
        session: requests.Session,
        logger: Any,
    ) -> None:
        self.chat_api_url = chat_api_url.rstrip("/")
        self.chat_api_key = chat_api_key.strip()
        self.chat_model = chat_model.strip() or "gemma4-31b"
        self.timeout_seconds = float(timeout_seconds)
        self.session = session
        self.logger = logger

    def _call_model(self, messages: List[Dict[str, str]], *, strip_internal: bool = True) -> str:
        if not self.chat_api_url:
            raise RuntimeError("Copilot API URL is not configured.")
        headers = {"Content-Type": "application/json"}
        if self.chat_api_key:
            headers["Authorization"] = f"Bearer {self.chat_api_key}"
        response = self.session.post(
            self.chat_api_url,
            headers=headers,
            json={
                "model": self.chat_model,
                "messages": messages,
                "max_tokens": 900,
                "temperature": 0.2,
            },
            timeout=self.timeout_seconds,
        )
        if not response.ok:
            raise RuntimeError(f"Chat model HTTP {response.status_code}: {response.text[:500]}")
        payload = response.json()
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not choices:
            raise RuntimeError("Chat model returned no choices.")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else ""
        content = str(content or "").strip()
        if not content:
            raise RuntimeError("Chat model returned an empty message.")
        if strip_internal:
            return strip_internal_capability_lines(content)
        return content

    def answer_context(self, *, context_type: str, context_payload: Dict[str, Any], user_id: str, username: str, content: str) -> str:
        normalized_content = compact_text(content, 6000)
        if not normalized_content:
            raise ValueError("content is required.")
        system_prompt = (
            "你是 V-Bio Copilot，也是协作留言助手和任务操作规划助手。"
            "根据能力定义选择最小可用能力。"
            "不要向用户展示内部能力名、工具名、skill 名、action id，也不要写“能力使用”或“能力调用”。"
            "任何提交、取消、删除、修改参数、筛选排序等动作都必须通过 candidate_plan_actions 的 schema 按钮执行，先给出计划并等待用户确认，不能声称已经执行。"
            "如果用户不在正确的 project 功能页或 task 功能页，明确指出当前功能不适合该操作，并告诉用户应进入哪个功能；不要编造可执行按钮。"
            "如果上下文中存在 candidate_plan_actions，说明界面会在消息下方显示确认按钮；这时不要要求用户再输入“确认”，只需提示用户检查按钮。"
            "Affinity Scoring 不是仅凭一个多肽/蛋白序列做结构预测；如果用户只给序列并要求提交结构预测，不要把它解释为 affinity 提交。"
            "回答要明确区分：分析结论、计划、确认项。优先使用中文。\n\n"
            f"{render_capability_prompt()}"
        )
        model_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "system", "content": f"context_type: {context_type}\ncontext_payload: {compact_text(context_payload, 5000)}"},
            {"role": "user", "content": f"{username or user_id}: {normalized_content}"},
        ]
        return self._call_model(model_messages)

    def _parse_json_response(self, raw_content: str) -> Dict[str, Any]:
        cleaned = raw_content.strip()
        fence_match = re.match(r"^```(?:json)?\s*\n(.*)```\s*$", cleaned, re.DOTALL)
        if fence_match:
            cleaned = fence_match.group(1).strip()
        try:
            candidate = json.loads(cleaned)
        except json.JSONDecodeError:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start < 0 or end <= start:
                self.logger.warning("Copilot plan_actions: failed to extract JSON from model response: %s", raw_content[:500])
                raise ValueError("Model did not return valid JSON plan.")
            candidate = json.loads(cleaned[start : end + 1])
        if not isinstance(candidate, dict):
            raise ValueError("Model plan must be a JSON object.")
        if candidate.get("execute_now") is True:
            raise ValueError("Model attempted to execute without confirmation.")
        return candidate

    def _candidate_includes_action(self, candidate: Dict[str, Any], action_id: str) -> bool:
        planned = candidate.get("actions") or candidate.get("plan_actions") or []
        if isinstance(planned, dict):
            planned = [planned]
        if not isinstance(planned, list):
            return False
        return any(isinstance(item, dict) and str(item.get("id") or "").strip() == action_id for item in planned)

    def plan_actions(self, *, context_type: str, context_payload: Dict[str, Any], user_id: str, username: str, content: str) -> List[Dict[str, Any]]:
        normalized_content = compact_text(content, 6000)
        if not normalized_content:
            raise ValueError("content is required.")
        normalized_ctx = str(context_type or "").strip()

        system_prompt = (
            "You are a deterministic planning adapter for V-Bio Copilot. "
            "Return JSON only. Do not explain. Do not include markdown. "
            "Treat the provided action input_schema as the contract for every payload. "
            "If the user asks to create a prediction task, extract all structural components into payload.components; "
            "do not use legacy protein-only shortcuts and do not omit small molecules or other component types.\n\n"
            f"{render_context_plan_schema_prompt(normalized_ctx, context_payload)}"
        )
        model_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "system", "content": f"context_payload: {compact_text(context_payload, 5000)}"},
            {"role": "user", "content": f"{username or user_id}: {normalized_content}"},
        ]
        raw_content = self._call_model(model_messages, strip_internal=False)
        candidate = self._parse_json_response(raw_content)

        if normalized_ctx == "task_detail":
            workflow_key = infer_workflow_key(context_payload)
            return build_task_submission_actions(candidate, normalized_content, workflow_key)

        # task_list / project_list — generic action builder
        actions = build_context_actions(normalized_ctx, candidate, context_payload, normalized_content)
        if actions or normalized_ctx != "task_list" or not self._candidate_includes_action(candidate, "tasks:create_with_sequence"):
            return actions

        repair_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "system", "content": f"context_payload: {compact_text(context_payload, 5000)}"},
            {
                "role": "system",
                "content": (
                    "The previous JSON did not satisfy the action schema. "
                    "For tasks:create_with_sequence, payload.components is required and must include every user-provided component. "
                    "Use type ligand for small molecules, compounds, CCD IDs, and SMILES strings. "
                    f"Previous JSON: {compact_text(candidate, 2000)}"
                ),
            },
            {"role": "user", "content": f"{username or user_id}: {normalized_content}"},
        ]
        repaired_raw_content = self._call_model(repair_messages, strip_internal=False)
        repaired_candidate = self._parse_json_response(repaired_raw_content)
        return build_context_actions(normalized_ctx, repaired_candidate, context_payload, normalized_content)
