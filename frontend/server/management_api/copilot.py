from __future__ import annotations

import json
import re
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import requests

from management_api.copilot_capabilities import (
    build_cross_context_skill_definitions,
    build_registered_capability_catalog,
    infer_workflow_key,
)
from management_api.copilot_skill_harness import (
    CopilotSkillHarness,
    PlanAudit,
    RECORD_IDENTITY_FIELDS,
    RECORD_LONG_FIELDS,
)
from management_api.copilot_skills.compute_skills import register_compute_skills
from management_api.copilot_skills.online_databases import OnlineDatabaseSkills, OnlineSkillDefinition
from management_api.copilot_trace import (
    TRACE_AUDIT_REJECTED,
    TRACE_MALFORMED_OUTPUT,
    TRACE_MODEL_REQUEST,
    TRACE_NO_CONVERGENCE,
    TRACE_OUTLINE,
    TRACE_SKILL_OBSERVATIONS,
    TRACE_STEP_DONE,
    TRACE_TERMINAL,
    PlannerTrace,
    compact_observations,
    compact_operations,
    compact_usage,
)


MAX_CONTEXT_STRING_CHARS = 1600
MAX_CONTEXT_LIST_ITEMS = 40
MAX_CONTEXT_DICT_KEYS = 80
MAX_MODEL_MESSAGE_CHARS = 64000
# Authoritative long fields (sequence, SMILES) fed back to the planner must be passed in full —
# the model can only quote verbatim what it actually receives. A 50-char preview would make a
# "give me the sequence" answer impossible. The cap only guards against pathological sizes.
MAX_OBSERVATION_LONG_CHARS = 4000

# Record keys that are pure plumbing — the source DB name, the echoed search term, result counts,
# and harness bookkeeping. Never useful in the model's answer, so skipped when rendering a record.
_SUMMARY_META_KEYS = frozenset({"source", "query", "count", "ok", "error", "metadata", "index"})


def _observation_field_line(key: str, value: Any) -> str:
    """Render one record field as ``key=value`` for the observation summary, or "" to skip.

    Surfaces EVERY scalar field of a record — the model sees the complete authoritative result
    (potency units, journal, year, phase, method, …) the way standard tool-result rendering works,
    rather than a hand-maintained allowlist that silently drops any field not enumerated (the bug
    that hid ChEMBL SMILES behind a nested object). Long authoritative fields (SMILES / sequence)
    render in full, bounded only by MAX_OBSERVATION_LONG_CHARS; short strings cap at 80 chars;
    lists join with ", "; nested objects, URLs, and pure-meta keys are skipped.
    """
    if not key or key.startswith("_") or key in _SUMMARY_META_KEYS:
        return ""
    if key.endswith(("Url", "url")):
        return ""
    if value is None or isinstance(value, dict):
        return ""
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(item).strip() for item in value if item is not None and str(item).strip())
    else:
        text = str(value).strip()
    if not text:
        return ""
    if key in RECORD_LONG_FIELDS:
        if len(text) > MAX_OBSERVATION_LONG_CHARS:
            return f"{key}={text[:MAX_OBSERVATION_LONG_CHARS]}... [truncated, {len(text)} chars total]"
        return f"{key}={text}"
    if len(text) > 80:
        return ""
    return f"{key}={text}"
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

CAPABILITY_CATALOG_SKILL = "platform.capability_catalog"

# Cap on how many prior-turn records are carried forward as copilot_memory — enough for continuity
# on a follow-up, small enough to stay well under the context budget.
MAX_MEMORY_RECORDS = 20


def _compact_memory_records(observations: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Project a turn's observations into compact, carry-forwardable records for copilot_memory.

    Uses the shared RECORD_*_FIELDS registry (single source of truth for record identity). Each
    record keeps its source + identity fields (≤80 chars) and truncated long fields (≤60), so a
    follow-up turn can recognize a previously retrieved entity without re-searching, while no full
    sequence/SMILES body is hauled across turns. General — projects whatever the skills returned.
    """
    records: List[Dict[str, Any]] = []
    for observation in observations.values():
        if not isinstance(observation, dict) or not observation.get("ok"):
            continue
        for value in observation.get("values") or []:
            if not isinstance(value, dict):
                continue
            nested = value.get("results")
            candidates = nested if isinstance(nested, list) else [value]
            source = str(value.get("source") or "")
            for record in candidates:
                if not isinstance(record, dict):
                    continue
                entry: Dict[str, Any] = {}
                if source:
                    entry["source"] = source
                for field in RECORD_IDENTITY_FIELDS:
                    text = str(record.get(field) or "").strip()
                    if text:
                        entry[field] = text[:80]
                for field in RECORD_LONG_FIELDS:
                    text = str(record.get(field) or "").strip()
                    if text:
                        entry[field] = text[:60]
                if len(entry) > (1 if source else 0):
                    records.append(entry)
                    if len(records) >= MAX_MEMORY_RECORDS:
                        return records
    return records


def _full_observation_records(observations: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Project a turn's observations into full records for user-facing display.

    Unlike _compact_memory_records (which truncates for cross-turn memory), this preserves long
    fields (sequences, SMILES) in full so the frontend 'Retrieved data' card shows the complete
    authoritative value the user can copy. Only scalar fields are kept; meta/plumbing keys are
    dropped the same way the summarizer does.
    """
    records: List[Dict[str, Any]] = []
    for observation in observations.values():
        if not isinstance(observation, dict) or not observation.get("ok"):
            continue
        for value in observation.get("values") or []:
            if not isinstance(value, dict):
                continue
            nested = value.get("results")
            candidates = nested if isinstance(nested, list) else [value]
            source = str(value.get("source") or "")
            for record in candidates:
                if not isinstance(record, dict):
                    continue
                entry: Dict[str, Any] = {}
                if source:
                    entry["source"] = source
                for key, val in record.items():
                    if not key or key.startswith("_") or key in _SUMMARY_META_KEYS:
                        continue
                    if key.endswith(("Url", "url")):
                        continue
                    if val is None or isinstance(val, (dict, list)):
                        continue
                    text = str(val).strip()
                    if text:
                        entry[key] = text
                if len(entry) > (1 if source else 0):
                    records.append(entry)
                    if len(records) >= MAX_MEMORY_RECORDS:
                        return records
    return records

# JSON-schema constraint keys the model-server grammar decoder cannot enforce. The harness
# audit validates the full schema, so these are stripped from the server-side grammar only,
# loosening generation-time constraints without weakening correctness.
_GRAMMAR_UNSUPPORTED_KEYS = frozenset({"uniqueItems", "pattern", "format"})



def _sanitize_schema_for_grammar(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {
            key: _sanitize_schema_for_grammar(value)
            for key, value in schema.items()
            if key not in _GRAMMAR_UNSUPPORTED_KEYS
        }
    if isinstance(schema, list):
        return [_sanitize_schema_for_grammar(item) for item in schema]
    return schema


def _read_registered_capability_catalog(_arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Return the registered capability catalog as structured data.

    The catalog lists every operation and workflow the platform supports. The planner reads this
    when it needs to know the platform's surface — e.g. to answer capability questions or to decide
    whether a user request maps to a registered action. The model writes its own natural-language
    answer from this data; the data is NOT pre-formatted text to echo.
    """
    return build_registered_capability_catalog()


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


def _normalize_json_control_chars(text: str) -> str:
    """Escape literal control characters inside JSON string values.

    Without a grammar constraint, the model writes JSON with literal newlines/tabs inside string
    values (e.g., the message field contains multi-line text with real \\n). This is invalid JSON
    per RFC 8259 — control characters must be escaped. We walk the text, tracking whether we're
    inside a string value, and escape literal control chars (\\n, \\r, \\t, and other control chars
    < 0x20) to their \\uXXXX or shorthand form. This preserves the model's intent (a valid JSON
    object with multi-line strings) without altering any content.
    """
    result: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                result.append(char)
                escaped = False
                continue
            if char == "\\":
                result.append(char)
                escaped = True
                continue
            if char == '"':
                result.append(char)
                in_string = False
                continue
            # Inside a string: escape control characters
            if char == "\n":
                result.append("\\n")
            elif char == "\r":
                result.append("\\r")
            elif char == "\t":
                result.append("\\t")
            elif ord(char) < 0x20:
                result.append(f"\\u{ord(char):04x}")
            else:
                result.append(char)
        else:
            if char == '"':
                in_string = True
            result.append(char)
    return "".join(result)


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


def _extract_first_json_object(text: str) -> Any:
    """Return the first complete JSON object in ``text``, or None.

    Used for thinking-mode planner output, which is free prose around the JSON. Unlike a greedy
    ``{.*}`` regex, ``json.JSONDecoder().raw_decode`` parses one object from a position and ignores
    trailing data, so stray braces in surrounding prose don't corrupt the extraction.
    """
    decoder = json.JSONDecoder()
    for start in (index for index, char in enumerate(text) if char == "{"):
        try:
            obj, _consumed = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        return obj
    return None


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
        max_planner_rounds: int = 8,
        max_malformed_retries: int = 3,
        enable_thinking: bool = False,
    ) -> None:
        self.chat_api_url = chat_api_url.rstrip("/")
        self.chat_api_key = chat_api_key.strip()
        self.chat_model = chat_model.strip() or "gemma4-31b"
        # Remember the env-var defaults so update_runtime_overrides can revert to them
        # when the user clears a field in the settings UI.
        self._default_api_url = self.chat_api_url
        self._default_api_key = self.chat_api_key
        self._default_model = self.chat_model
        self.timeout_seconds = float(timeout_seconds)
        self.session = session
        self.logger = logger
        self.max_planner_rounds = max(1, min(20, int(max_planner_rounds)))
        self.max_malformed_retries = max(0, min(6, int(max_malformed_retries)))
        # Let the model reason before emitting structured output. Improves grounding/multi-step for models that
        # can think AND conform to a strict schema; gemma4-31b conforms worse with thinking on, so default off.
        self.enable_thinking = bool(enable_thinking)
        # Per-call outbound proxy (from runtime settings). None means direct connection.
        self._proxies: Optional[Dict[str, str]] = None
        skills = OnlineDatabaseSkills(session=session, timeout_seconds=min(self.timeout_seconds, 30.0))
        skills.register(
            OnlineSkillDefinition(
                name=CAPABILITY_CATALOG_SKILL,
                description=(
                    "Read the canonical platform workflow, operation, input, parameter, and option catalog "
                    "from the registered schemas before answering what the platform supports."
                ),
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            ),
            _read_registered_capability_catalog,
        )
        register_compute_skills(skills)
        self.skill_harness = CopilotSkillHarness(skills=skills)

    def update_runtime_overrides(
        self,
        *,
        proxies: Optional[Dict[str, str]] = None,
        chat_api_url: str = "",
        chat_api_key: str = "",
        chat_model: str = "",
    ) -> None:
        """Apply runtime settings (proxy / LLM endpoint) without restarting.

        ``proxies`` is always set (``None`` clears a previously-configured proxy).
        ``chat_api_url`` / ``chat_api_key`` / ``chat_model`` fall back to the env-var
        defaults stored in ``__init__`` when empty, so clearing a field in the UI
        reverts the singleton to its original configuration.
        """
        self._proxies = proxies
        self.chat_api_url = chat_api_url.strip().rstrip("/") or self._default_api_url
        self.chat_api_key = chat_api_key.strip() or self._default_api_key
        self.chat_model = chat_model.strip() or self._default_model
        # Propagate proxy to the online-database skills (UniProt, ChEMBL, etc.).
        skills_obj = getattr(self.skill_harness, "skills", None)
        if skills_obj is not None and hasattr(skills_obj, "_proxies"):
            skills_obj._proxies = self._proxies

    def _call_model(
        self,
        messages: List[Dict[str, str]],
        *,
        response_schema: Dict[str, Any],
        schema_name: str = "vbio_copilot_turn",
    ) -> Tuple[str, Dict[str, Any]]:
        if not self.chat_api_url:
            raise RuntimeError("Copilot API URL is not configured.")
        messages = normalize_chat_messages_for_template(messages)
        total_chars = sum(len(str(message.get("content") or "")) for message in messages)
        if total_chars > MAX_MODEL_MESSAGE_CHARS:
            raise ValueError(f"Copilot context is too large after compaction ({total_chars} chars).")
        headers = {"Content-Type": "application/json"}
        if self.chat_api_key:
            headers["Authorization"] = f"Bearer {self.chat_api_key}"
        body: Dict[str, Any] = {
            "model": self.chat_model,
            "messages": messages,
            "max_tokens": 4096 if self.enable_thinking else 3072,
            "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": self.enable_thinking},
            # Simple grammar (skill:enum, permissive arguments). This ensures the model outputs
            # VALID JSON so _parse_planner_turn succeeds. The grammar does NOT constrain message
            # length (no maxLength) — a short message is a model behavior, not a grammar limitation.
            # The system prompt's MESSAGE FIELD directive guides the model to write complete answers.
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": _sanitize_schema_for_grammar(response_schema),
                },
            },
        }
        response = self.session.post(
            self.chat_api_url,
            headers=headers,
            json=body,
            timeout=self.timeout_seconds,
            proxies=self._proxies,
        )
        if not response.ok:
            self.logger.error(
                "Copilot model rejected the required structured-output contract: status=%s response=%s",
                response.status_code,
                str(response.text or "")[:1000],
            )
            raise RuntimeError("Copilot model rejected the required structured-output contract.")
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Copilot model returned an invalid protocol response.") from exc
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not choices or not isinstance(choices[0], dict):
            raise RuntimeError("Copilot model returned no planner choice.")
        content = _extract_chat_message_content(choices[0].get("message"))
        if not content:
            raise RuntimeError("Copilot model returned an empty structured response.")
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        return content, usage

    @staticmethod
    def _parse_planner_turn(raw_content: str) -> Dict[str, Any]:
        text = str(raw_content or "").strip()
        if not text:
            raise ValueError("planner output is empty")
        # Normalize literal control characters inside JSON string values (the model may include
        # real newlines in multi-line messages — invalid JSON per spec, fixable by escaping).
        text = _normalize_json_control_chars(text)
        # Fast path: direct JSON parse.
        try:
            candidate = json.loads(text)
            if isinstance(candidate, dict):
                return candidate
        except json.JSONDecodeError:
            pass
        # Slow path: extract the FIRST complete JSON object from free text (thinking mode wraps
        # JSON in prose, or the model adds commentary around the JSON).
        candidate = _extract_first_json_object(text)
        if candidate is None:
            raise ValueError("planner output contains no JSON object")
        if not isinstance(candidate, dict):
            raise ValueError("planner output must be a JSON object")
        return candidate

    def _generate_full_answer(
        self,
        planner_messages: List[Dict[str, str]],
        short_message: str,
    ) -> str:
        """Second-phase generation: produce a complete natural-language answer without grammar.

        The planner (phase 1) decided this turn needs NO tools — it's a pure answer (capability
        question, greeting, context analysis). The JSON-constrained message is typically short
        because the grammar decoder treats it as a string field. This second call lets the model
        write freely — no JSON, no grammar — producing the full answer the user expects.

        The model sees the SAME conversation context (system prompt + user message + any prior
        assistant messages). It just outputs plain text instead of JSON.
        """
        # Build a minimal prompt: the original system prompt + user message, plus a directive
        # to write the full answer as plain text. The directive must be merged into the system
        # message — the model server rejects multiple system messages or system-after-user.
        answer_messages: List[Dict[str, str]] = []
        system_parts: List[str] = []
        for msg in planner_messages:
            role = str(msg.get("role") or "").strip()
            content = str(msg.get("content") or "").strip()
            if not content:
                continue
            if role == "system":
                system_parts.append(content)
            elif role == "user":
                answer_messages.append({"role": role, "content": content})
        # Merge all system content into ONE system message at the beginning, plus the directive.
        system_parts.append(
            "You decided no tools are needed for this request. Now write your COMPLETE answer "
            "to the user as plain text — no JSON, no code blocks. Use Markdown for readability. "
            "Write in the user's language. Be helpful, specific, and complete."
        )
        answer_messages = [{"role": "system", "content": "\n\n".join(system_parts)}] + answer_messages
        body: Dict[str, Any] = {
            "model": self.chat_model,
            "messages": answer_messages,
            "max_tokens": 2048,
            "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
            # The model server requires a response_format field. Use {type: text} so the model
            # generates free text (no JSON structure) — this is the phase-2 answer generation.
            "response_format": {"type": "text"},
        }
        headers = {"Content-Type": "application/json"}
        if self.chat_api_key:
            headers["Authorization"] = f"Bearer {self.chat_api_key}"
        try:
            response = self.session.post(
                self.chat_api_url,
                headers=headers,
                json=body,
                timeout=self.timeout_seconds,
                proxies=self._proxies,
            )
        except (requests.RequestException, OSError, ValueError, TypeError) as exc:
            # Phase-2 is an enrichment of an already-valid planner message: a transport/parse
            # failure keeps the planner's message. Anything else (a programming error) must
            # surface instead of being silently swallowed.
            self.logger.warning("Copilot phase-2 answer generation failed: %s", str(exc)[:200])
            return short_message
        if not response.ok:
            self.logger.warning("Copilot phase-2 answer rejected: status=%s", response.status_code)
            return short_message
        try:
            payload = response.json()
        except (TypeError, ValueError):
            return short_message
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not choices or not isinstance(choices[0], dict):
            return short_message
        full_answer = _extract_chat_message_content(choices[0].get("message"))
        self.logger.info(
            "Copilot phase-2 answer: short=%d chars, full=%d chars, preview=%s",
            len(short_message), len(full_answer or ""),
            repr((full_answer or "")[:100]),
        )
        if not full_answer or len(full_answer) < len(short_message):
            # If the free-text answer is shorter than the planner's message, keep the planner's.
            return short_message
        return full_answer

    def plan_turn(
        self,
        *,
        context_type: str,
        context_payload: Dict[str, Any],
        user_id: str,
        username: str,
        content: str,
        on_event: Callable[[Dict[str, Any]], None] | None = None,
        abort: threading.Event | None = None,
    ) -> Dict[str, Any]:
        normalized_content = str(content or "").strip()
        if not normalized_content:
            raise ValueError("content is required.")
        normalized_context = str(context_type or "").strip()
        safe_context_payload = sanitize_context_payload(context_payload)
        workflow_key = infer_workflow_key(safe_context_payload, default="")
        # Progressive disclosure: the planner sees ONLY the current host page's action skills plus
        # the universal read-only catalog — not every page's skills. A plan advances page by page:
        # each confirmation action carries the page it navigates to (targetContextType), the
        # harness groups confirmations by target page, and the frontend navigates page by page as
        # the user confirms each step. The next page's Copilot turn then exposes that page's action
        # skills, so every step is planned with only the tools relevant at that point. A later
        # operation may consume an earlier operation's observation via $fromObservation regardless
        # of which page each step lives on.
        host_definitions = build_cross_context_skill_definitions(
            current_context=normalized_context,
            context_payload=safe_context_payload,
            workflow_key=workflow_key,
        )
        definitions = self.skill_harness.definitions(host_definitions)
        protocol = self.skill_harness.render_protocol_prompt(definitions)
        # Always use the simple schema (skill:enum + permissive arguments). The full oneOf schema
        # (16K+ chars, 19 variants) makes the grammar decoder constrain every token against all
        # variants, truncating the model's natural-language answer mid-list. The harness's
        # _validate_schema is the authoritative argument check; the grammar is a loose guide only.
        response_schema = self.skill_harness.planner_output_schema_simple(definitions)
        system_prompt = (
            "You are V-Bio Copilot, an AI assistant for a structural biology platform.\n"
            "You help users with protein/compound lookups, task analysis, and project management.\n"
            "The harness validates your output, executes read tools, and returns results.\n"
            "After each step the harness tells you what to do next — follow its instructions.\n"
            "Read the context_payload to understand where the user is and what resources are available.\n"
            "Never fabricate data or identifiers.\n\n"
            "LANGUAGE: Always reply in the SAME language the user writes in. If they write Chinese, "
            "reply in Chinese. If English, reply in English. This is mandatory.\n\n"
            "MESSAGE FIELD: The \"message\" field IS your complete answer to the user. Write it as a "
            "FULL, self-contained response — multiple sentences or paragraphs with real content. Do NOT "
            "write a one-line label or title and stop. Do NOT end with a colon promising a list — "
            "write the list items right there in the message. The user only sees your message; it must "
            "be substantive and complete on its own.\n\n"
            "FORMATTING: Use Markdown for readability — **bold** for key terms, bullet lists for "
            "enumerations, `code` for identifiers. Break long answers into short paragraphs.\n\n"
            "CONTEXT-AWARE ANSWERS:\n"
            "- context_payload contains the current project, task, draft, and runtime state. On a "
            "task_detail page it includes the selected task's result: state, metric values (pLDDT, "
            "ipTM, pAE, affinity), components, parameters, and error text. Answer analysis/explanation "
            "questions directly from this data — cite the actual values.\n"
            "- On a project_list page, context_payload.summary carries precomputed totals "
            "(allTypeCounts, allBackendCounts, allTaskStateCounts, activeProjects, failedProjects). "
            "Answer statistics questions by enumerating these counts inline.\n"
            "- copilot_memory: entities retrieved in earlier turns of this conversation, with their "
            "identity fields (accession, name, CID, …) and source. Sequences/SMILES in memory are "
            "TRUNCATED — memory carries identity, not full data. When the user refers to an entity "
            "that appears in copilot_memory, you already know its identity: answer directly, or "
            "re-retrieve the FULL record with a resolve skill when exact values are needed. Never "
            "invent or complete field values from memory, and never claim a value you did not retrieve "
            "in this conversation.\n"
            "- Lead with the answer, not a preamble.\n\n"
            "SKILL EXPOSURE:\n"
            "- Only the current page's action skills are listed; read skills are universal.\n"
            "- A plan advances page by page: confirming an action navigates to its target page, and "
            "the next turn exposes that page's action skills.\n"
            "- Skills are atomic unit operations: emit one operation per unit of work, never a fused "
            "multi-step shortcut, and never invent arguments the schemas do not declare.\n\n"
            "EXECUTION PRINCIPLES:\n"
            "- For complex tasks, first emit goal_steps outlining the phases. The outline is fixed "
            "once emitted: do not re-emit it, and do not combine it with operations or questions in "
            "the same round. The harness drives each step one at a time.\n"
            "- For each step, emit only the operations that step requires. Read operations do not "
            "advance a step; it advances when you emit its confirmation operations or conclude it.\n"
            "- After reads return, verify the results match what the user asked for before proceeding.\n"
            "- If a read returns unexpected data (wrong organism, wrong protein, irrelevant match), "
            "retry with a more precise query.\n"
            "- If a read fails (source unavailable) or returns no authoritative match, you must "
            "either retry with different arguments under a new operation id, ask the user, or state "
            "the outcome plainly to the user — never proceed as if the data had been retrieved.\n"
            "- Reference retrieved values via $fromObservation, never paste long values.\n"
            "- When a user names a protein family or another ambiguous entity, ask which specific "
            "member they mean before searching — do not guess and search for a random member.\n\n"
            f"{protocol}"
        )
        context_json = json.dumps(safe_context_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        context_block = (
            f"context_type: {normalized_context}\n"
            f"context_payload: {context_json}"
        )
        planner_messages: List[Dict[str, str]] = [
            {"role": "system", "content": f"{system_prompt}\n\n{context_block}"},
            {"role": "user", "content": f"{username or user_id}: {normalized_content}"},
        ]
        observations: Dict[str, Dict[str, Any]] = {}
        trace = PlannerTrace(on_step=on_event)
        last_issues: List[str] = []
        rejected_audits: set[tuple[str, ...]] = set()
        # The signature of the LAST rejected audit, used for CONSECUTIVE-repeat detection. The
        # set above is not enough: in a multi-step (outline) plan the SAME structural mistake can
        # legitimately recur in a later step after the model fixed it once (e.g. forgetting
        # depends_on in step 1, fixing it, then forgetting it again in step 2). That is a
        # fixable error in a new context, not non-convergence. Only CONSECUTIVE identical
        # rejections (no accepted round in between) mean the model cannot fix the error.
        last_rejected_signature: tuple[str, ...] | None = None
        # Consecutive failures per skill across rounds — lets the harness escalate correction
        # guidance when a source stays down (the planner then reports instead of retrying).
        skill_failures: Dict[str, int] = {}
        message: str = ""
        # Hierarchical planning: when the planner emits a goal_steps outline, the harness drives
        # step-by-step concretization. outline holds the abstract steps; outline_index tracks which
        # step is being concretized; all_step_actions accumulates confirmation actions across steps.
        outline: List[Dict[str, Any]] = []
        outline_index = 0
        all_step_actions: List[Dict[str, Any]] = []
        plan_id = uuid.uuid4().hex

        malformed_attempts = 0
        for round_index in range(self.max_planner_rounds):
            # An SSE consumer that disconnected sets the abort event; stop before the next (costly)
            # model call rather than running the whole round budget for a client that's gone.
            if abort is not None and abort.is_set():
                raise RuntimeError("planner aborted")
            try:
                raw_content, usage = self._call_model(planner_messages, response_schema=response_schema)
            except RuntimeError as exc:
                if "empty structured response" in str(exc):
                    # Empty model output — treat as malformed, retry.
                    malformed_attempts += 1
                    self.logger.warning("Copilot model returned empty output (attempt %d)", malformed_attempts)
                    trace.record(round_index, TRACE_MALFORMED_OUTPUT, attempt=malformed_attempts, error="empty response")
                    last_issues = ["model returned empty output"]
                    if malformed_attempts > self.max_malformed_retries:
                        break
                    # Retry WITHOUT echoing an empty assistant message: several model servers
                    # reject a message with empty content (HTTP 400), which would burn the whole
                    # retry budget and surface as a 502 instead of a real retry. The system
                    # instruction alone tells the model what happened.
                    planner_messages.extend(
                        [
                            {"role": "system", "content": "Your previous response was empty. Output a valid JSON object."},
                        ]
                    )
                    continue
                raise
            trace.record(
                round_index,
                TRACE_MODEL_REQUEST,
                messages_chars=sum(len(str(msg.get("content") or "")) for msg in planner_messages),
                usage=compact_usage(usage),
            )
            try:
                candidate = self._parse_planner_turn(raw_content)
            except ValueError as exc:
                # The model returned unparseable/truncated JSON. Log the raw output for debugging.
                malformed_attempts += 1
                self.logger.warning(
                    "Copilot model returned unparseable planner JSON (attempt %d): %s | raw_output[:500]=%s",
                    malformed_attempts,
                    str(exc),
                    repr(str(raw_content or "")[:500]),
                )
                trace.record(round_index, TRACE_MALFORMED_OUTPUT, attempt=malformed_attempts, error=str(exc))
                planner_messages.extend(
                    [
                        {"role": "assistant", "content": str(raw_content or "")[:2000]},
                        {
                            "role": "system",
                            "content": (
                                "Your previous response was not valid JSON for the required schema (it was "
                                "truncated or malformed). Re-emit ONLY the complete JSON object — no prose, no "
                                "code fences. Keep it CONCISE: reference every retrieved sequence, SMILES, or long "
                                "value with {\"$fromObservation\": \"<id>\", \"field\": \"<field>\", \"index\": <n>} "
                                "and never paste long values into the output."
                            ),
                        },
                    ]
                )
                last_issues = [f"planner output was not valid JSON: {exc}"]
                if malformed_attempts > self.max_malformed_retries:
                    # Stop retrying unparseable output so the user gets a graceful message instead of a long hang.
                    self.logger.error("Copilot exceeded malformed-JSON retry budget (%d).", self.max_malformed_retries)
                    break
                continue

            audit = self.skill_harness.audit_plan(
                candidate,
                definitions,
                observations=observations,
                context_type=normalized_context,
                active_outline=outline if outline else None,
            )
            message = str(candidate.get("message") or "").strip()
            audit_issues = list(audit.issues)
            if audit_issues:
                last_issues = audit_issues
                audit_signature = tuple(audit_issues)
                if audit_signature == last_rejected_signature:
                    # The planner emitted the SAME rejected output on CONSECUTIVE rounds — genuine
                    # non-convergence. The harness audits structure only (operations, schema,
                    # dependencies, grounding); message text is never rejected, so a repeated
                    # structural rejection means the model truly cannot fix the structural error.
                    # Break to failed state honestly.
                    self.logger.warning("Copilot planner repeated a rejected plan: %s", "; ".join(audit_issues))
                    break
                last_rejected_signature = audit_signature
                rejected_audits.add(audit_signature)
                trace.record(round_index, TRACE_AUDIT_REJECTED, issues=audit_issues)
                planner_messages.extend(
                    [
                        # Echo the model's own output back, capped like the malformed-JSON path so a
                        # very long (or degenerate) response cannot inflate the context unboundedly.
                        {"role": "assistant", "content": str(raw_content or "")[:2000]},
                        {
                            "role": "system",
                            "content": (
                                "Your previous output was rejected. Fix these issues and try again:\n"
                                + "\n".join(f"- {issue}" for issue in audit_issues)
                            ),
                        },
                    ]
                )
                continue

            # An accepted round resets the consecutive-repeat tracker: the model changed its
            # output and the harness accepted it, so a later identical mistake is a NEW context
            # (e.g. the next outline step), not a stuck loop.
            last_rejected_signature = None

            # Validated planner questions for this round (the audit rejected any malformed item,
            # so at this point every question in the candidate is well-formed).
            questions = list(candidate.get("questions") or [])

            # ── Hierarchical planning: outline + step-by-step concretization ──
            # When the planner emits a goal_steps outline (state="outline"), store it and ask the
            # planner to concretize the first step. The outline is the plan's declared direction:
            # once stored it is IMMUTABLE — any re-emission of goal_steps is rejected by the audit
            # (active_outline), so the harness drives every later step from the locked outline.
            if audit.state == "outline" and audit.goal_steps:
                outline = list(audit.goal_steps)
                outline_index = 0
                trace.record(
                    round_index, TRACE_OUTLINE,
                    steps=[str(s.get("description") or "")[:120] for s in outline],
                )
                planner_messages.extend(
                    [
                        {"role": "assistant", "content": raw_content},
                        {
                            "role": "system",
                            "content": (
                                f"Step 1 of {len(outline)}: {outline[0].get('description', '')}\n"
                                "Emit the operations for this step only."
                            ),
                        },
                    ]
                )
                continue

            # A question mid-plan ends the turn: the answer determines the remaining steps, and the
            # outline cannot persist across turns (the next turn re-plans with the user's answer and
            # cross-turn memory). Return the questions to the user instead of advancing the step or
            # silently discarding them.
            if questions and audit.state == "needs_input" and outline:
                trace.record(
                    round_index,
                    TRACE_TERMINAL,
                    state="needs_input",
                    operations=compact_operations(audit.operations),
                    message_chars=len(message),
                )
                self.logger.info(trace.summary())
                return {
                    "content": message,
                    "actions": [],
                    "state": "needs_input",
                    "questions": list(questions),
                    "plan_id": plan_id,
                    "trace": trace.steps(),
                    "observations": _full_observation_records(observations),
                }

            read_operations = [item for item in audit.operations if item.definition.read_only]
            if read_operations:
                round_observations = self.skill_harness.execute_operations(read_operations)
                observations.update(round_observations)
                # Track per-skill consecutive failures ACROSS rounds, so the harness can tell the
                # planner when a source has failed repeatedly and retrying is unlikely to help.
                for obs in round_observations.values():
                    if isinstance(obs, dict) and not obs.get("ok"):
                        skill = str(obs.get("skill") or "").strip() or "unknown"
                        skill_failures[skill] = skill_failures.get(skill, 0) + 1
                trace.record(
                    round_index,
                    TRACE_SKILL_OBSERVATIONS,
                    observations=compact_observations(round_observations),
                )
                planner_messages.extend(
                    [
                        {"role": "assistant", "content": raw_content},
                        {
                            "role": "system",
                            "content": (
                                # The harness audits every executed unit operation and reports its
                                # outcome class (SUCCESS / NO_MATCH / FAILED) with the correction
                                # policy for each — the planner decides the concrete correction.
                                CopilotAssistant._execution_classification(
                                    round_observations, skill_failures=skill_failures
                                )
                                + "\n\n"
                                + "CORRECTION RULES:\n"
                                "- NO_MATCH means the source answered authoritatively that nothing "
                                "matches: re-emit a read with corrected arguments (a NEW operation "
                                "id) or tell the user plainly that nothing was found.\n"
                                "- FAILED means the source could not be reached: retry with a NEW "
                                "operation id or tell the user the source is unavailable. Never "
                                "report failed lookups as 'no data exists'.\n"
                                "- Never reuse an operation id that already produced an observation.\n"
                                "- Verify that the results match what the user asked for before "
                                "proceeding (organism, entity, units).\n\n"
                                + (
                                    f"Step {outline_index + 1} of {len(outline)}: emit the operations for this step, "
                                    f"or if this step is done, emit no operations.\n\n"
                                    if outline else
                                    "Data retrieved. Now emit the action operations the task requires, "
                                    "or answer the question if it is a lookup.\n\n"
                                )
                                + CopilotAssistant._summarize_observations(round_observations)
                            ),
                        },
                    ]
                )
                continue

            # ── Write operations (await_confirmation) or empty completion ──
            # When inside an outline, accumulate the step's actions and advance to the next step.
            # When not in an outline (simple task), return immediately as before.
            if outline and outline_index < len(outline):
                # This round produced operations (or none) for outline step outline_index. A
                # zero-operation round is the planner's declaration that the step needs no further
                # work (a lookup-only step) — allowed and traced, but surfaced to the planner so a
                # mistakenly skipped step is corrected instead of silently passing.
                step_operation_count = len(audit.operations)
                if audit.operations:
                    plan_id = plan_id or uuid.uuid4().hex
                    step_actions = self.skill_harness.build_confirmation_actions(
                        [op for op in audit.operations if not op.definition.read_only],
                        plan_id=plan_id,
                        context_type=normalized_context,
                        workflow_key=workflow_key,
                    )
                    all_step_actions.extend(step_actions)
                trace.record(
                    round_index, TRACE_STEP_DONE,
                    step=outline_index + 1, total=len(outline),
                    description=str(outline[outline_index].get("description") or "")[:120],
                    operations=step_operation_count,
                )
                outline_index += 1
                if outline_index < len(outline):
                    # Advance to the next outline step.
                    next_desc = str(outline[outline_index].get("description") or "")
                    step_note = (
                        "\nNote: the previous step concluded without operations. If it required "
                        "any, emit them now as part of this step.\n"
                        if step_operation_count == 0 else ""
                    )
                    planner_messages.extend(
                        [
                            {"role": "assistant", "content": raw_content},
                            {
                                "role": "system",
                                "content": (
                                    f"Step {outline_index + 1} of {len(outline)}: {next_desc}\n"
                                    f"Emit the operations for this step.{step_note}"
                                ),
                            },
                        ]
                    )
                    continue
                else:
                    # All outline steps concretized. Return the accumulated actions.
                    # A pure-analysis outline (no confirmation actions, no lookups) ends with the
                    # last step's short JSON-constrained message — enrich it with phase-2 the same
                    # way the non-outline pure-answer path does, so the user gets a complete
                    # summary instead of a one-line step message.
                    final_outline_message = message
                    if not all_step_actions and not observations:
                        final_outline_message = self._generate_full_answer(planner_messages, message)
                    trace.record(
                        round_index, TRACE_TERMINAL,
                        state="await_confirmation" if all_step_actions else "complete",
                        operations=compact_operations(audit.operations),
                        message_chars=len(final_outline_message),
                    )
                    self.logger.info(trace.summary())
                    return {
                        "content": final_outline_message,
                        "actions": all_step_actions,
                        "state": "await_confirmation" if all_step_actions else "complete",
                        "questions": [],
                        "plan_id": plan_id,
                        "trace": trace.steps(),
                        "observations": _full_observation_records(observations),
                    }

            plan_id = uuid.uuid4().hex
            actions = self.skill_harness.build_confirmation_actions(
                audit.operations,
                plan_id=plan_id,
                context_type=normalized_context,
                workflow_key=workflow_key,
            )

            # TWO-PHASE GENERATION: when the planner decides NO tools are needed (pure answer turn),
            # the JSON-constrained message is typically short because the grammar decoder treats it
            # as a short string field. Do a SECOND model call WITHOUT grammar — the model writes a
            # complete, natural-language answer. This is the root-cause fix for truncated answers.
            # The planner already decided the answer needs no tools/questions/operations — this phase
            # only enriches the message text, preserving the planner's structural decision exactly.
            final_message = message
            if audit.state == "complete" and not actions and not observations:
                final_message = self._generate_full_answer(planner_messages, message)

            trace.record(
                round_index,
                TRACE_TERMINAL,
                state=audit.state,
                operations=compact_operations(audit.operations),
                message_chars=len(final_message),
            )
            self.logger.info(trace.summary())
            return {
                "content": final_message,
                "actions": actions,
                "state": audit.state,
                "questions": list(candidate.get("questions") or []),
                "plan_id": plan_id,
                "trace": trace.steps(),
                "observations": _full_observation_records(observations),
            }

        # The planner loop exhausted its round budget without reaching a terminal state. The turn
        # fails honestly: state="failed", a plain user-facing message, and the audit reasons are kept
        # in the server log (NOT the user-facing message — that text is written for the model: it names
        # internal mechanisms like "emit operations" and "summary.allTypeCounts" that mean nothing to a
        # user). No last-message reuse and no observation summarization as an answer — both would
        # present a failed plan as a completed result. The observations gathered so far are still
        # returned (they are real data, shown as retrieved, not as an answer), and the next user
        # turn can continue from them via cross-turn memory.
        detail = "; ".join(last_issues) if last_issues else "planner did not reach a terminal state"
        trace.record(self.max_planner_rounds, TRACE_NO_CONVERGENCE, reason=detail)
        self.logger.warning("Copilot turn failed to converge; audit detail: %s", detail)
        # Honest failure: state plainly that this attempt didn't produce a usable answer, and invite
        # the user to rephrase. No injected content, no capability list, no fallback answer — the
        # planner+harness loop either produces a correct answer or reports failure honestly. The raw
        # audit detail stays in the server log only (model-internal vocabulary, meaningless to a user).
        failure_message = "I could not complete this request. Please try rephrasing."
        trace.record(
            self.max_planner_rounds,
            TRACE_TERMINAL,
            state="failed",
            operations=[],
            message_chars=len(failure_message),
        )
        self.logger.info(trace.summary())
        return {
            "content": failure_message,
            "actions": [],
            "state": "failed",
            "questions": [],
            "plan_id": plan_id,
            "trace": trace.steps(),
            "observations": _full_observation_records(observations),
        }

    @staticmethod
    def _execution_classification(
        observations: Dict[str, Dict[str, Any]],
        *,
        skill_failures: Dict[str, int] | None = None,
    ) -> str:
        """Render the harness's per-operation audit of the just-executed reads.

        Every executed unit operation is classified into exactly one of SUCCESS / NO_MATCH /
        FAILED (the harness's three-state outcome audit) and echoed to the planner together with
        the correction policy. The planner decides the concrete correction — retry with new
        arguments, ask the user, or report the outcome — but it can no longer silently proceed
        past a lookup that did not actually succeed.

        When the SAME source has failed repeatedly across rounds (skill_failures), the audit
        escalates its correction guidance: the harness has observed the outage persist, so it
        tells the planner plainly to stop retrying and report to the user. The planner still
        decides — the harness only supplies the state it has audited.
        """
        failures = dict(skill_failures or {})
        statuses: List[str] = []
        for obs_id, obs in observations.items():
            if not isinstance(obs, dict):
                continue
            skill = str(obs.get("skill") or obs_id)
            status = CopilotSkillHarness.classify_observation(obs)
            if status == "FAILED":
                failure_count = failures.get(skill, 0)
                if failure_count >= 2:
                    statuses.append(
                        f"{obs_id} [{skill}] FAILED (source unavailable) — this source has now "
                        f"failed {failure_count} times in a row for this request. Do NOT retry it "
                        "again in this turn: further attempts are unlikely to succeed right now. "
                        "Tell the user plainly that the source is currently unavailable, and offer "
                        "a concrete alternative (e.g. provide the identifier so it can be resolved "
                        "once the source is back)."
                    )
                else:
                    statuses.append(
                        f"{obs_id} [{skill}] FAILED (source unavailable). If you retry this "
                        f"lookup, emit it under a NEW operation id — the failed id {obs_id} "
                        "cannot be reused for a retry (re-emitting it is rejected)."
                    )
            elif status == "NO_MATCH":
                statuses.append(f"{obs_id} [{skill}] NO_MATCH (authoritative empty result)")
            else:
                statuses.append(f"{obs_id} [{skill}] SUCCESS")
        return "EXECUTION REPORT (audit of every executed operation):\n" + "\n".join(
            f"- {line}" for line in statuses
        ) if statuses else "EXECUTION REPORT: (no operations executed)"

    @staticmethod
    def _summarize_observations(observations: Dict[str, Dict[str, Any]]) -> str:
        """Render observations as a concise, human-readable summary instead of a raw JSON blob.

        This makes the key data (compound name, accession, SMILES, etc.) immediately
        visible to the model, so it can ground its answer without parsing nested JSON.

        Failed observations (ok=False) are surfaced explicitly as a source/transport error —
        distinct from an authoritative "no match" (which returns ok=True with zero results). A
        transient upstream outage (HTTP 5xx, timeout, connection drop) must not be reported to
        the user as "nothing found", so the model is told the source was unavailable.
        """
        lines: List[str] = []
        for obs_id, obs in observations.items():
            if not isinstance(obs, dict):
                continue
            skill = str(obs.get("skill") or obs_id)
            items = obs.get("items") if isinstance(obs.get("items"), list) else []
            first_item = items[0] if items and isinstance(items[0], dict) else {}
            label_args = first_item.get("arguments") if isinstance(first_item.get("arguments"), dict) else {}
            label_query = str(
                label_args.get("query")
                or label_args.get("identifier")
                or label_args.get("text")
                or label_args.get("accession")
                or label_args.get("name")
                or ""
            ).strip()

            if not obs.get("ok"):
                errors = obs.get("errors") if isinstance(obs.get("errors"), list) else []
                error_messages = [
                    str(err.get("error") or "").strip()
                    for err in errors
                    if isinstance(err, dict) and err.get("error")
                ] or ["unknown error"]
                header = f'Observation "{obs_id}" [{skill}'
                if label_query:
                    header += f"({label_query})"
                header += "] SOURCE UNAVAILABLE — a transport/source error, NOT an authoritative no-match:"
                lines.append(header)
                for message in error_messages[:2]:
                    lines.append(f"  - error: {message[:300]}")
                lines.append(
                    "  The data source could not be reached; it may be temporarily down. Tell the user the "
                    "lookup could not be completed and to retry shortly — do NOT claim the data is absent."
                )
                continue

            values = obs.get("values") or []
            records: List[Dict[str, Any]] = []
            for val in values:
                if not isinstance(val, dict):
                    continue
                nested = val.get("results")
                if isinstance(nested, list) and nested:
                    records.extend(r for r in nested if isinstance(r, dict))
                else:
                    records.append(val)
            first_val = values[0] if values and isinstance(values[0], dict) else {}
            query = first_val.get("query") or first_val.get("name") or first_val.get("identifier") or first_val.get("text") or ""
            header = f'Observation "{obs_id}" [{skill}'
            if query:
                header += f"({query})"
            header += f"] {len(records)} result(s):"
            lines.append(header)
            for rec in records[:3]:
                if not isinstance(rec, dict):
                    continue
                # Surface every scalar field so the model sees the complete record (no allowlist
                # to forget when a skill adds a field). Long fields render in full via the helper.
                parts = [
                    _observation_field_line(key, value)
                    for key, value in rec.items()
                ]
                parts = [part for part in parts if part]
                if parts:
                    lines.append("  - " + " | ".join(parts))
            if len(records) > 3:
                lines.append(f"  ... and {len(records) - 3} more.")
        return "\n".join(lines) if lines else "(no observations)"

    def answer_context(
        self,
        *,
        context_type: str,
        context_payload: Dict[str, Any],
        user_id: str,
        username: str,
        content: str,
    ) -> str:
        turn = self.plan_turn(
            context_type=context_type,
            context_payload=context_payload,
            user_id=user_id,
            username=username,
            content=content,
        )
        if turn["actions"]:
            raise ValueError("This request requires the confirmation-capable Copilot turn endpoint.")
        return str(turn["content"])

    def plan_actions(
        self,
        *,
        context_type: str,
        context_payload: Dict[str, Any],
        user_id: str,
        username: str,
        content: str,
    ) -> List[Dict[str, Any]]:
        turn = self.plan_turn(
            context_type=context_type,
            context_payload=context_payload,
            user_id=user_id,
            username=username,
            content=content,
        )
        return list(turn["actions"])
