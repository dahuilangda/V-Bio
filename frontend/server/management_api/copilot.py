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


MAX_CONTEXT_STRING_CHARS = 1600
MAX_CONTEXT_LIST_ITEMS = 40
MAX_CONTEXT_DICT_KEYS = 80
MAX_MODEL_MESSAGE_CHARS = 30000
MUTATING_TASK_TOKENS = (
    "submit",
    "run",
    "rerun",
    "predict",
    "delete",
    "rename",
    "提交",
    "运行",
    "重跑",
    "预测",
    "删除",
    "重命名",
    "新的任务",
    "新任务",
)
REDACTED_FILE_TEXT_KEYS = {
    "content",
    "structure_text",
    "structuretext",
    "cif_text",
    "pdb_text",
    "sdf_text",
    "mol_text",
    "file_content",
    "filecontent",
    "raw",
    "blob",
    "bytes",
    "data",
}
FILE_METADATA_KEYS = {
    "filename",
    "file_name",
    "format",
    "type",
    "mimetype",
    "size",
    "chainid",
    "chainids",
    "template_chain_id",
    "templat_chain_id",
    "templatechainid",
    "target_chain_ids",
    "targetchainids",
}


def compact_text(value: Any, limit: int = 1800) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _compact_string(value: str, limit: int = MAX_CONTEXT_STRING_CHARS) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated, original_chars={len(text)}]"


def _normalized_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9_]", "", str(key or "").strip().lower())


def _looks_like_file_payload(parent: Dict[str, Any]) -> bool:
    normalized_keys = {_normalized_key(key) for key in parent.keys()}
    return any(key in normalized_keys for key in FILE_METADATA_KEYS)


def sanitize_context_payload(value: Any, *, depth: int = 0, parent: Dict[str, Any] | None = None, key: Any = None) -> Any:
    """Return a model-safe copy of Copilot context without raw uploaded file bodies."""
    if depth > 8:
        return "[truncated: max depth reached]"

    normalized_key = _normalized_key(key)
    if isinstance(value, str):
        if normalized_key in REDACTED_FILE_TEXT_KEYS and (parent is None or _looks_like_file_payload(parent) or len(value) > MAX_CONTEXT_STRING_CHARS):
            return f"[omitted file/text payload, chars={len(value)}]"
        return _compact_string(value)

    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, list):
        safe_items = [
            sanitize_context_payload(item, depth=depth + 1, parent=None, key=None)
            for item in value[:MAX_CONTEXT_LIST_ITEMS]
        ]
        if len(value) > MAX_CONTEXT_LIST_ITEMS:
            safe_items.append({"_truncated_items": len(value) - MAX_CONTEXT_LIST_ITEMS})
        return safe_items

    if isinstance(value, dict):
        safe: Dict[str, Any] = {}
        for index, (child_key, child_value) in enumerate(value.items()):
            if index >= MAX_CONTEXT_DICT_KEYS:
                safe["_truncated_keys"] = len(value) - MAX_CONTEXT_DICT_KEYS
                break
            safe[str(child_key)] = sanitize_context_payload(
                child_value,
                depth=depth + 1,
                parent=value,
                key=child_key,
            )
        return safe

    return _compact_string(str(value))


def compact_context_payload(value: Any, limit: int = 5000) -> str:
    safe_value = sanitize_context_payload(value)
    try:
        text = json.dumps(safe_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(safe_value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [context truncated, original_chars={len(text)}]"


def normalize_chat_messages_for_template(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
    system_parts: List[str] = []
    non_system: List[Dict[str, str]] = []
    for message in messages:
        role = str(message.get("role") or "").strip() or "user"
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
            continue
        non_system.append({"role": role, "content": content})
    if not system_parts:
        return non_system
    return [{"role": "system", "content": "\n\n".join(system_parts)}] + non_system


def _candidate_requests_clarification(candidate: Dict[str, Any]) -> bool:
    capability = str(candidate.get("capability") or "").strip().lower()
    missing_questions = candidate.get("missing_questions")
    return capability == "clarification_needed" or (
        isinstance(missing_questions, list) and any(str(item or "").strip() for item in missing_questions)
    )


def _looks_like_mutating_task_request(content: str) -> bool:
    normalized = str(content or "").strip().lower()
    return any(token in normalized for token in MUTATING_TASK_TOKENS)


def _extract_chat_message_content(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif isinstance(item.get("content"), str):
                    parts.append(str(item.get("content")))
        return "\n".join(part.strip() for part in parts if part and part.strip()).strip()
    for key in ("text",):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _payload_has_reasoning_without_content(payload: Any) -> bool:
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not choices or not isinstance(choices[0], dict):
        return False
    message = choices[0].get("message")
    if not isinstance(message, dict) or _extract_chat_message_content(message):
        return False
    for key in ("reasoning", "reasoning_content"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _error_mentions_field(text: Any, field: str) -> bool:
    normalized = str(text or "").lower()
    return field.lower() in normalized and any(token in normalized for token in ("unsupported", "unknown", "extra", "unexpected", "not permitted", "unrecognized"))


def _read_authoritative_workflow_key(context_payload: Any) -> str:
    if not isinstance(context_payload, dict):
        return ""
    page = context_payload.get("page")
    if isinstance(page, dict):
        value = page.get("workflowKey") or page.get("workflow_key") or page.get("workflow")
        if value:
            return str(value or "").strip().lower()
    project = context_payload.get("project")
    if isinstance(project, dict):
        value = project.get("workflow_key") or project.get("workflow") or project.get("task_type")
        if value:
            return str(value or "").strip().lower()
    return ""


def remove_page_workflow_conflicts(context_payload: Any) -> Any:
    if not isinstance(context_payload, dict):
        return context_payload
    workflow_key = _read_authoritative_workflow_key(context_payload)
    if not workflow_key or "affinity" in workflow_key:
        return context_payload
    current_task = context_payload.get("currentTask")
    if not isinstance(current_task, dict):
        return context_payload
    cleaned_task = dict(current_task)
    cleaned_task.pop("affinity", None)
    properties = cleaned_task.get("properties")
    if isinstance(properties, dict):
        cleaned_properties = dict(properties)
        cleaned_properties.pop("affinityMode", None)
        cleaned_properties.pop("affinity_mode", None)
        cleaned_task["properties"] = cleaned_properties
    cleaned_task["_copilot_context_note"] = (
        "Workflow-incompatible affinity result fields were omitted because the authoritative page workflow is "
        f"{workflow_key}."
    )
    cleaned = dict(context_payload)
    cleaned["currentTask"] = cleaned_task
    return cleaned


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

    def _call_model(self, messages: List[Dict[str, str]], *, strip_internal: bool = True, json_response: bool = False) -> str:
        if not self.chat_api_url:
            raise RuntimeError("Copilot API URL is not configured.")
        messages = normalize_chat_messages_for_template(messages)
        total_chars = sum(len(str(message.get("content") or "")) for message in messages)
        if total_chars > MAX_MODEL_MESSAGE_CHARS:
            raise ValueError(f"Copilot context is too large after compaction ({total_chars} chars).")
        headers = {"Content-Type": "application/json"}
        if self.chat_api_key:
            headers["Authorization"] = f"Bearer {self.chat_api_key}"
        attempts = [
            {"response_format": json_response, "disable_thinking": False, "requires_reasoning_empty": False},
            {"response_format": json_response, "disable_thinking": True, "requires_reasoning_empty": True},
            {"response_format": False, "disable_thinking": True, "requires_reasoning_empty": True},
            {"response_format": False, "disable_thinking": False, "requires_reasoning_empty": False},
        ] if json_response else [
            {"response_format": False, "disable_thinking": False, "requires_reasoning_empty": False},
            {"response_format": False, "disable_thinking": True, "requires_reasoning_empty": True},
            {"response_format": False, "disable_thinking": False, "requires_reasoning_empty": False},
        ]
        saw_reasoning_empty = False
        for attempt, options in enumerate(attempts):
            if options["requires_reasoning_empty"] and not saw_reasoning_empty:
                continue
            request_messages = messages
            if attempt > 0:
                retry_instruction = (
                    "The previous response was empty. Return a non-empty response. "
                    "If JSON output is requested, return exactly one valid JSON object matching the schema."
                )
                request_messages = normalize_chat_messages_for_template(
                    [{"role": "system", "content": retry_instruction}] + messages
                )
            body: Dict[str, Any] = {
                "model": self.chat_model,
                "messages": request_messages,
                "max_tokens": 900,
                "temperature": 0.2,
            }
            if options["disable_thinking"]:
                body["chat_template_kwargs"] = {"enable_thinking": False}
            if options["response_format"]:
                body["response_format"] = {"type": "json_object"}
            response = self.session.post(
                self.chat_api_url,
                headers=headers,
                json=body,
                timeout=self.timeout_seconds,
            )
            if not response.ok:
                retry_body = dict(body)
                if response.status_code == 400 and "chat_template_kwargs" in retry_body and _error_mentions_field(response.text, "chat_template_kwargs"):
                    retry_body.pop("chat_template_kwargs", None)
                if response.status_code == 400 and "response_format" in retry_body and _error_mentions_field(response.text, "response_format"):
                    retry_body.pop("response_format", None)
                if retry_body != body:
                    response = self.session.post(self.chat_api_url, headers=headers, json=retry_body, timeout=self.timeout_seconds)
                if not response.ok:
                    raise RuntimeError(f"Chat model HTTP {response.status_code}: {response.text[:500]}")
            payload = response.json()
            choices = payload.get("choices") if isinstance(payload, dict) else None
            if not choices:
                raise RuntimeError("Chat model returned no choices.")
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            content = _extract_chat_message_content(message)
            if content:
                if strip_internal:
                    return strip_internal_capability_lines(content)
                return content
            saw_reasoning_empty = saw_reasoning_empty or _payload_has_reasoning_without_content(payload)
            finish_reason = choices[0].get("finish_reason") if isinstance(choices[0], dict) else None
            self.logger.warning(
                "Copilot model returned an empty message; retrying with response_format=%s disable_thinking=%s attempt=%s finish_reason=%s payload=%s",
                bool(body.get("response_format")),
                bool(body.get("chat_template_kwargs")),
                attempt + 1,
                finish_reason,
                compact_text(payload, 1000),
            )
        raise RuntimeError("Chat model returned an empty message.")

    def answer_context(self, *, context_type: str, context_payload: Dict[str, Any], user_id: str, username: str, content: str) -> str:
        normalized_content = compact_text(content, 6000)
        if not normalized_content:
            raise ValueError("content is required.")
        safe_context_payload = remove_page_workflow_conflicts(sanitize_context_payload(context_payload))
        system_prompt = (
            "你是 V-Bio Copilot，也是协作留言助手和任务操作规划助手。"
            "根据能力定义选择最小可用能力。"
            "不要向用户展示内部能力名、工具名、skill 名、action id，也不要写“能力使用”或“能力调用”。"
            "页面模块必须以 context_payload.page.workflowKey / page.workflowTitle 为准；如果 currentTask、历史结果、properties 或 affinity 字段与 page 冲突，不能用它们覆盖当前页面模块。"
            "回答当前所在功能时，优先引用 page.workflowTitle；不要因为结果记录里存在 affinity/score 字段就说用户在 Affinity 页面。"
            "任何提交、取消、删除、修改参数、筛选排序等动作都必须通过 candidate_plan_actions 的 schema 按钮执行，先给出计划并等待用户确认，不能声称已经执行。"
            "如果用户不在正确的 project 功能页或 task 功能页，明确指出当前功能不适合该操作，并告诉用户应进入哪个功能；不要编造可执行按钮。"
            "只有当 context_payload.candidate_plan_actions 是非空数组时，才允许说明界面会在消息下方显示确认按钮；否则不要提确认按钮。"
            "如果没有 candidate_plan_actions，但用户要求提交、运行、删除或修改，说明当前没有生成可执行确认操作，不要声称已经规划出按钮。"
            "只能承诺本 Copilot schema 和页面已有功能能完成的事：分析当前上下文、规划需确认的参数/组件修改、提交/删除/重命名、应用已上传文件。"
            "不要承诺生成、返回、导出、下载或计算化合物坐标、结构文件、后台结果文件；除非 context_payload 或 candidate_plan_actions 明确提供了对应能力。"
            "不要泛泛建议“通过下载按钮/下载选项获取结果文件”；只有当 context_payload.page.availableActions 或 context_payload.resultDownloads 明确包含下载能力时才可以提下载。"
            "Affinity Scoring 不是仅凭一个多肽/蛋白序列做结构预测；如果用户只给序列并要求提交结构预测，不要把它解释为 affinity 提交。"
            "回答要明确区分：分析结论、计划、确认项。优先使用中文。\n\n"
            f"{render_capability_prompt()}"
        )
        context_block = f"context_type: {context_type}\ncontext_payload: {compact_context_payload(safe_context_payload, 5000)}"
        model_messages = [
            {"role": "system", "content": f"{system_prompt}\n\n{context_block}"},
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

    def _repair_plan_json(
        self,
        *,
        system_prompt: str,
        context_block: str,
        username: str,
        user_id: str,
        content: str,
        previous_output: Any,
        reason: str,
    ) -> Dict[str, Any]:
        repair_hint = (
            "The previous planning output did not satisfy the schema. "
            "Return one valid JSON object only. Do not include markdown or prose. "
            "For task_detail component replacement, use parameter_patch.componentsReplacement.components and preserve every explicitly labeled component. "
            "For task_list creation, use actions[].payload.components and preserve every explicitly labeled component. "
            f"Failure reason: {compact_text(reason, 1000)}\n"
            f"Previous output: {compact_text(previous_output, 2500)}"
        )
        repair_messages = [
            {"role": "system", "content": f"{system_prompt}\n\n{context_block}\n\n{repair_hint}"},
            {"role": "user", "content": f"{username or user_id}: {content}"},
        ]
        repaired_raw_content = self._call_model(repair_messages, strip_internal=False, json_response=True)
        return self._parse_json_response(repaired_raw_content)

    def plan_actions(self, *, context_type: str, context_payload: Dict[str, Any], user_id: str, username: str, content: str) -> List[Dict[str, Any]]:
        normalized_content = compact_text(content, 6000)
        if not normalized_content:
            raise ValueError("content is required.")
        normalized_ctx = str(context_type or "").strip()
        safe_context_payload = remove_page_workflow_conflicts(sanitize_context_payload(context_payload))

        system_prompt = (
            "You are a deterministic planning adapter for V-Bio Copilot. "
            "Return JSON only. Do not explain. Do not include markdown. "
            "Treat the provided action input_schema as the contract for every payload. "
            "If the user asks to create a prediction task, extract all structural components into payload.components; "
            "do not use legacy protein-only shortcuts and do not omit small molecules or other component types.\n\n"
            f"{render_context_plan_schema_prompt(normalized_ctx, safe_context_payload)}"
        )
        context_block = f"context_payload: {compact_context_payload(safe_context_payload, 5000)}"
        model_messages = [
            {"role": "system", "content": f"{system_prompt}\n\n{context_block}"},
            {"role": "user", "content": f"{username or user_id}: {normalized_content}"},
        ]
        raw_content = self._call_model(model_messages, strip_internal=False, json_response=True)
        try:
            candidate = self._parse_json_response(raw_content)
        except ValueError as exc:
            candidate = self._repair_plan_json(
                system_prompt=system_prompt,
                context_block=context_block,
                username=username,
                user_id=user_id,
                content=normalized_content,
                previous_output=raw_content,
                reason=str(exc),
            )

        if normalized_ctx == "task_detail":
            workflow_key = infer_workflow_key(safe_context_payload)
            actions = build_task_submission_actions(candidate, normalized_content, workflow_key)
            if actions:
                return actions
            if _candidate_requests_clarification(candidate):
                return []
            repaired_candidate = self._repair_plan_json(
                system_prompt=system_prompt,
                context_block=context_block,
                username=username,
                user_id=user_id,
                content=normalized_content,
                previous_output=candidate,
                reason="The JSON parsed successfully but produced no valid task_detail action after schema validation.",
            )
            repaired_actions = build_task_submission_actions(repaired_candidate, normalized_content, workflow_key)
            if repaired_actions:
                return repaired_actions
            if _candidate_requests_clarification(repaired_candidate):
                return []
            if _looks_like_mutating_task_request(normalized_content):
                self.logger.warning(
                    "Copilot plan_actions: model failed task_detail schema after repair. candidate=%s repaired=%s",
                    compact_text(candidate, 1000),
                    compact_text(repaired_candidate, 1000),
                )
                raise ValueError("Copilot could not produce a valid confirmation action from the task-detail schema.")
            return []

        # task_list / project_list — generic action builder
        actions = build_context_actions(normalized_ctx, candidate, safe_context_payload, normalized_content)
        if actions or normalized_ctx != "task_list" or not self._candidate_includes_action(candidate, "tasks:create_with_sequence"):
            return actions

        repair_hint = (
            "The previous JSON did not satisfy the action schema. "
            "For tasks:create_with_sequence, payload.components is required and must include every user-provided component. "
            "Use type ligand for small molecules, compounds, CCD IDs, and SMILES strings. "
            "When the user labels a value with 小分子, 配体, small molecule, ligand, compound, or SMILES, that value must be a ligand component even if it is uppercase. "
            f"Previous JSON: {compact_text(candidate, 2000)}"
        )
        repair_messages = [
            {"role": "system", "content": f"{system_prompt}\n\n{context_block}\n\n{repair_hint}"},
            {"role": "user", "content": f"{username or user_id}: {normalized_content}"},
        ]
        repaired_raw_content = self._call_model(repair_messages, strip_internal=False, json_response=True)
        repaired_candidate = self._parse_json_response(repaired_raw_content)
        return build_context_actions(normalized_ctx, repaired_candidate, safe_context_payload, normalized_content)
