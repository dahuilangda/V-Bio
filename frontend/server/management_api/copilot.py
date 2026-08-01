from __future__ import annotations

import json
import re
import threading
import uuid
from typing import Any, Callable, Dict, List, Tuple

import requests

from management_api.copilot_capabilities import (
    build_registered_capability_catalog,
    build_task_detail_skill_definitions,
    infer_workflow_key,
)
from management_api.copilot_skill_harness import (
    CopilotSkillHarness,
    RECORD_IDENTITY_FIELDS,
    RECORD_LONG_FIELDS,
    RECORD_NUMERIC_FIELDS,
)
from management_api.copilot_skills.context_actions import build_context_skill_definitions
from management_api.copilot_skills.compute_skills import register_compute_skills
from management_api.copilot_skills.online_databases import OnlineDatabaseSkills, OnlineSkillDefinition
from management_api.copilot_trace import (
    TRACE_AUDIT_REJECTED,
    TRACE_FALLBACK,
    TRACE_MALFORMED_OUTPUT,
    TRACE_MODEL_REQUEST,
    TRACE_NO_CONVERGENCE,
    TRACE_SKILL_OBSERVATIONS,
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
        self.timeout_seconds = float(timeout_seconds)
        self.session = session
        self.logger = logger
        self.max_planner_rounds = max(1, min(20, int(max_planner_rounds)))
        self.max_malformed_retries = max(0, min(6, int(max_malformed_retries)))
        # Let the model reason before emitting structured output. Improves grounding/multi-step for models that
        # can think AND conform to a strict schema; gemma4-31b conforms worse with thinking on, so default off.
        self.enable_thinking = bool(enable_thinking)
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
            "max_tokens": 4096 if self.enable_thinking else 2048,
            "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": self.enable_thinking},
        }
        # When thinking is enabled, omit the strict json_schema grammar constraint — the grammar
        # engine conflicts with the model's reasoning tokens, causing conformance failures. Without
        # it the model reasons freely and emits parseable JSON naturally (the harness validates).
        if not self.enable_thinking:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": _sanitize_schema_for_grammar(response_schema),
                },
            }
        response = self.session.post(
            self.chat_api_url,
            headers=headers,
            json=body,
            timeout=self.timeout_seconds,
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
        # Fast path: direct JSON parse (strict-schema mode emits a single clean object).
        try:
            candidate = json.loads(text)
        except json.JSONDecodeError:
            # Slow path: thinking mode emits free text — extract the FIRST complete JSON object.
            # raw_decode parses one object and tolerates trailing prose/braces (a greedy {.*} match
            # would span stray trailing braces and fail). Scanning from each '{' finds the real one.
            candidate = _extract_first_json_object(text)
            if candidate is None:
                raise ValueError("planner output contains no JSON object")
        if not isinstance(candidate, dict):
            raise ValueError("planner output must be a JSON object")
        return candidate

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
        if normalized_context == "task_detail":
            host_definitions = build_task_detail_skill_definitions(workflow_key)
        else:
            host_definitions = build_context_skill_definitions(
                normalized_context,
                safe_context_payload,
                workflow_key=workflow_key,
            )
        definitions = self.skill_harness.definitions(host_definitions)
        protocol = self.skill_harness.render_protocol_prompt(definitions)
        if self.enable_thinking:
            response_schema = self.skill_harness.planner_output_schema_simple(definitions)
        else:
            response_schema = self.skill_harness.planner_output_schema(definitions)
        system_prompt = (
            "You are the V-Bio Copilot planner. Reason from the user's request, conversation context, current page "
            "state, and registered skills. Skills are atomic operations; compose the smallest complete operation set "
            "for the request. Use read-only skills when authoritative information is required. The harness may return "
            "observations or audit issues for another planning round. Every non-read-only operation remains pending "
            "until the user explicitly confirms it in the host application. The message is user-visible: do not expose "
            "planner or harness internals, skill names, operation ids, schemas, or internal context fields. Do not invent "
            "an operation or observation. Ground platform-capability claims in the registered capability catalog. "
            "Answer the requested scope directly. "
            "The context_payload may include copilot_memory: discrete records retrieved earlier in this same "
            "conversation. You may cite a memory record to answer a follow-up about that exact entity, but any "
            "compound or protein the user newly names still requires a fresh search — memory is for continuity, "
            "never a substitute for a new lookup. "
            "When resolving biological or chemical identifiers, use the registered search and resolve skills. "
            "Use your own knowledge to recognize what kind of identifier the user gave you, and choose the skill "
            "argument that matches its kind. Never fabricate, partially extract, or coerce an identifier into a "
            "different form or namespace. Always run a search skill for any compound or protein the user names — "
            "never claim a record is missing without searching first, and never answer a structure or sequence "
            "question from memory. If a search or resolve returns no authoritative match, report that plainly; "
            "never substitute a different record. Your answer must report exactly what the skill observation "
            "returned: use the identity, SMILES, and sequence from the observation verbatim, and never substitute "
            "your own belief about what an identifier names.\n\n"
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

        malformed_attempts = 0
        for round_index in range(self.max_planner_rounds):
            # An SSE consumer that disconnected sets the abort event; stop before the next (costly)
            # model call rather than running the whole round budget for a client that's gone.
            if abort is not None and abort.is_set():
                raise RuntimeError("planner aborted")
            raw_content, usage = self._call_model(planner_messages, response_schema=response_schema)
            trace.record(
                round_index,
                TRACE_MODEL_REQUEST,
                messages_chars=sum(len(str(msg.get("content") or "")) for msg in planner_messages),
                usage=compact_usage(usage),
            )
            try:
                candidate = self._parse_planner_turn(raw_content)
            except ValueError as exc:
                # The model returned unparseable/truncated JSON. Nudge it to re-emit valid JSON instead of
                # failing the whole turn on a transient protocol slip (bounded by the round budget).
                malformed_attempts += 1
                self.logger.warning(
                    "Copilot model returned unparseable planner JSON (attempt %d): %s",
                    malformed_attempts,
                    str(exc),
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
            )
            message = str(candidate.get("message") or "").strip()
            audit_issues = list(audit.issues)
            if audit_issues:
                last_issues = audit_issues
                audit_signature = tuple(audit_issues)
                if audit_signature in rejected_audits:
                    self.logger.warning("Copilot planner repeated a rejected plan: %s", "; ".join(audit_issues))
                    break
                rejected_audits.add(audit_signature)
                trace.record(round_index, TRACE_AUDIT_REJECTED, issues=audit_issues)
                planner_messages.extend(
                    [
                        {"role": "assistant", "content": raw_content},
                        {
                            "role": "system",
                            "content": json.dumps(
                                {"event": "audit_rejected", "round": round_index + 1, "issues": audit_issues},
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                        },
                    ]
                )
                continue

            read_operations = [item for item in audit.operations if item.definition.read_only]
            if read_operations:
                round_observations = self.skill_harness.execute_operations(read_operations)
                observations.update(round_observations)
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
                                "Authoritative skill observations for this turn. These results override any "
                                "prior knowledge: your next message must be grounded in them and must quote "
                                "the identity, SMILES, and sequence they contain verbatim, never a substitute.\n\n"
                                + CopilotAssistant._summarize_observations(round_observations)
                            ),
                        },
                    ]
                )
                continue

            plan_id = uuid.uuid4().hex
            actions = self.skill_harness.build_confirmation_actions(
                audit.operations,
                plan_id=plan_id,
                context_type=normalized_context,
                workflow_key=workflow_key,
            )
            trace.record(
                round_index,
                TRACE_TERMINAL,
                state=audit.state,
                operations=compact_operations(audit.operations),
                message_chars=len(message),
            )
            self.logger.info(trace.summary())
            return {
                "content": message,
                "actions": actions,
                "state": audit.state,
                "questions": list(candidate.get("questions") or []),
                "plan_id": plan_id,
                "trace": trace.steps(),
                "observations": _compact_memory_records(observations),
            }

        # Structured planning did not converge. As a last resort, let the model answer
        # conversationally (no schema pressure) using the context + any observations gathered.
        # The grounding guard still checks the answer against observations, so this cannot
        # surface a hallucination that ignores retrieved data.
        fallback_content = self._fallback_answer(planner_messages, observations)
        trace.record(self.max_planner_rounds, TRACE_FALLBACK)
        if fallback_content:
            trace.record(
                self.max_planner_rounds,
                TRACE_TERMINAL,
                state="complete",
                operations=[],
                message_chars=len(fallback_content),
            )
            self.logger.info(trace.summary())
            return {
                "content": fallback_content,
                "actions": [],
                "state": "complete",
                "questions": [],
                "plan_id": uuid.uuid4().hex,
                "trace": trace.steps(),
                "observations": _compact_memory_records(observations),
            }

        # Last resort: if observations were gathered, present them to the user so the data
        # is not lost — the model failed to compose an answer, but the authoritative retrieved
        # data is still valuable. This is NOT a fallback/substitution: it presents what the
        # database returned, transparently, when the model could not ground its answer.
        if observations:
            summary = self._summarize_observations(observations)
            trace.record(
                self.max_planner_rounds,
                TRACE_TERMINAL,
                state="complete",
                operations=[],
                message_chars=len(summary),
                surfaced_observations=True,
            )
            self.logger.info(trace.summary())
            return {
                "content": (
                    "I retrieved the following data but could not reliably compose a complete answer. "
                    "Here is what the databases returned:\n\n" + summary
                ),
                "actions": [],
                "state": "complete",
                "questions": [],
                "plan_id": uuid.uuid4().hex,
                "trace": trace.steps(),
                "observations": _compact_memory_records(observations),
            }

        detail = "; ".join(last_issues) if last_issues else "planner did not reach a terminal state"
        trace.record(self.max_planner_rounds, TRACE_NO_CONVERGENCE, reason=detail)
        self.logger.info(trace.summary())
        self.logger.error("Copilot planner did not reach a terminal state: %s", detail)
        raise RuntimeError("Copilot could not complete an auditable plan. No operation was executed.")

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
                parts: List[str] = []
                for key in RECORD_IDENTITY_FIELDS:
                    v = str(rec.get(key) or "").strip()
                    if v and len(v) <= 80:
                        parts.append(f"{key}={v}")
                for key in RECORD_LONG_FIELDS:
                    v = str(rec.get(key) or "").strip()
                    if not v:
                        continue
                    # Pass the full authoritative value so the model can quote it verbatim. Only
                    # bound pathological lengths; never reduce a real sequence/SMILES to a preview.
                    if len(v) > MAX_OBSERVATION_LONG_CHARS:
                        parts.append(f"{key}={v[:MAX_OBSERVATION_LONG_CHARS]}... [truncated, {len(v)} chars total]")
                    else:
                        parts.append(f"{key}={v}")
                for key in RECORD_NUMERIC_FIELDS:
                    v = rec.get(key)
                    if v is not None:
                        parts.append(f"{key}={v}")
                if parts:
                    lines.append("  - " + " | ".join(parts))
            if len(records) > 3:
                lines.append(f"  ... and {len(records) - 3} more.")
        return "\n".join(lines) if lines else "(no observations)"

    def _fallback_answer(
        self,
        planner_messages: List[Dict[str, str]],
        observations: Dict[str, Dict[str, Any]],
    ) -> str:
        """Try an unstructured chat completion when structured planning fails.

        Returns the model's conversational answer if it passes the grounding guard
        (or if no observations exist to guard against). Returns empty string on any
        failure so the caller falls through to graceful degradation.
        """
        if not self.chat_api_url:
            return ""
        fallback_messages = list(planner_messages)
        fallback_messages.append(
            {
                "role": "system",
                "content": (
                    "Structured planning did not converge. Answer the user's question directly and "
                    "conversationally. Use any observations above verbatim. If you do not have the "
                    "data, say so plainly."
                ),
            }
        )
        headers = {"Content-Type": "application/json"}
        if self.chat_api_key:
            headers["Authorization"] = f"Bearer {self.chat_api_key}"
        body: Dict[str, Any] = {
            "model": self.chat_model,
            "messages": normalize_chat_messages_for_template(fallback_messages),
            "max_tokens": 1024,
            "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        try:
            response = self.session.post(
                self.chat_api_url,
                headers=headers,
                json=body,
                timeout=self.timeout_seconds,
            )
            if not response.ok:
                return ""
            payload = response.json()
            choices = payload.get("choices") if isinstance(payload, dict) else None
            if not choices or not isinstance(choices[0], dict):
                return ""
            content = _extract_chat_message_content(choices[0].get("message"))
            if not content or not content.strip():
                return ""
            content = content.strip()
            # If observations were gathered, the fallback answer must still be grounded.
            if observations:
                grounding_issue = self.skill_harness._grounding_issue(content, observations)
                if grounding_issue:
                    self.logger.warning("Copilot fallback answer was not grounded; rejecting.")
                    return ""
            return content
        except Exception:
            return ""

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
