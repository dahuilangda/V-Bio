"""Best-effort inline auto-complete for the Copilot composer.

A deliberately tiny counterpart to ``CopilotAssistant``: no planner loop, no harness, no schema —
one OpenAI-compatible chat completion that returns a short continuation of the user's in-progress
input. It is OPTIONAL assistance: every failure path returns an empty string so the composer never
blocks or surfaces an error on the user's behalf.

Unlike the planner, the completer receives the SAME project/page context the planner does
(context_type + context_payload), so its suggestions are anchored to the workflow the user is in,
the page they are on, and the concrete resources visible there — not a generic capability blurb.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import requests

from management_api.copilot import sanitize_context_payload
from management_api.copilot_capabilities import (
    WORKFLOW_PARAMETER_KEYS,
    build_capability_orientation,
)
from management_api.copilot_skills.workflows import infer_workflow_key


# A continuation is a short phrase; cap both the prompt and the model output so this stays fast.
_MAX_CONTENT_CHARS = 500
_MAX_COMPLETION_CHARS = 60
_MAX_COMPLETION_TOKENS = 20
# Cap the derived context summary so the completer prompt stays small (it runs on every keystroke).
_MAX_CONTEXT_SUMMARY_CHARS = 1200

# Human-readable labels for each workflow, surfaced to the completer so its suggestions name the
# right concepts (e.g. "ligand"/"SMILES" on affinity, "binder" on peptide design). Pure labels —
# no example molecules or proteins, which would overfit.
_WORKFLOW_LABELS = {
    "prediction": "structure prediction (protein / complex folding, optionally with affinity)",
    "virtual_screening": "virtual screening (docking a compound library against a protein target)",
    "affinity": "affinity scoring (binding strength / pose between a target and a ligand)",
    "peptide_design": "peptide design (designing peptide binders)",
    "lead_optimization": "lead optimization",
}

# Map the page context the composer is open on into a plain hint for the model.
_CONTEXT_HINTS = {
    "project_list": "the Projects list (starting new work)",
    "task_list": "a project's task list",
    "task_detail": "a specific task and its results",
}

def ctx_hint_fallback(context_type: str) -> str:
    """Readable hint when context_type isn't one of the known keys."""
    value = str(context_type or "").strip()
    return value.replace("_", " ") if value else "a V-Bio workspace"


def _page_context_hint(context_type: str, workflow_key: str) -> str:
    """One-line description of WHERE the user is, combining the page and the workflow."""
    page = _CONTEXT_HINTS.get(context_type) or ctx_hint_fallback(context_type)
    workflow_label = _WORKFLOW_LABELS.get(workflow_key)
    if workflow_label:
        return f"{page} — workflow: {workflow_label}"
    return page


def _summarize_context_payload(context_payload: Any) -> str:
    """Project a rich context_payload into a compact, completer-facing summary.

    The host pages already build payloads describing the project, workflow, visible tasks, and the
    current draft. The planner gets them in full; the completer only needs the parts that disambiguate
    intent (project name, workflow, current task name/state, available actions, draft components,
    run-blocked reason, and the visible entity counts). Everything is flattened to short lines so the
    completer can infer what the user is most likely to type next.
    """
    safe = sanitize_context_payload(context_payload)
    if not isinstance(safe, dict) or not safe:
        return ""
    lines: List[str] = []

    project = safe.get("project")
    if isinstance(project, dict):
        name = str(project.get("name") or project.get("projectName") or "").strip()
        if name:
            lines.append(f"project: {name}")
        task_type = str(project.get("task_type") or project.get("workflow") or project.get("workflow_key") or "").strip()
        if task_type:
            lines.append(f"workflow: {task_type}")

    page = safe.get("page")
    if isinstance(page, dict):
        workflow_key = str(page.get("workflowKey") or page.get("workflow_key") or "").strip()
        if workflow_key and not any(line.startswith("workflow:") for line in lines):
            lines.append(f"workflow: {workflow_key}")
        actions = page.get("availableActions")
        if isinstance(actions, list) and actions:
            action_labels = [str(a).strip() for a in actions if str(a).strip()]
            if action_labels:
                lines.append("available actions: " + ", ".join(action_labels[:8]))

    current_task = safe.get("currentTask")
    if isinstance(current_task, dict):
        task_name = str(current_task.get("taskName") or current_task.get("name") or "").strip()
        task_state = str(current_task.get("taskState") or current_task.get("state") or "").strip()
        if task_name:
            lines.append(f"current task: {task_name}" + (f" ({task_state})" if task_state else ""))

    draft = safe.get("draft")
    if isinstance(draft, dict):
        draft_name = str(draft.get("taskName") or "").strip()
        if draft_name and not any("current task:" in line for line in lines):
            lines.append(f"draft task: {draft_name}")
        backend = str(draft.get("backend") or "").strip()
        if backend:
            lines.append(f"backend: {backend}")
        components = draft.get("components")
        if isinstance(components, list) and components:
            comp_descs: List[str] = []
            for comp in components:
                if not isinstance(comp, dict):
                    continue
                ctype = str(comp.get("type") or "").strip()
                if not ctype:
                    continue
                seq = str(comp.get("sequence") or "").strip()
                if seq:
                    comp_descs.append(f"{ctype} ({len(seq)})" if ctype in ("protein", "dna", "rna") else ctype)
                else:
                    comp_descs.append(ctype)
            if comp_descs:
                lines.append("components: " + ", ".join(comp_descs[:6]))
        run_disabled = str(draft.get("runDisabledReason") or safe.get("runDisabledReason") or "").strip()
        if run_disabled:
            lines.append(f"run blocked: {run_disabled}")

    runtime = safe.get("runtime")
    if isinstance(runtime, dict):
        states = runtime.get("taskStates") or runtime.get("states")
        if isinstance(states, dict) and states:
            state_summary = ", ".join(f"{k}={v}" for k, v in list(states.items())[:4] if v)
            if state_summary:
                lines.append(f"task states: {state_summary}")

    # Visible counts help the completer suggest list-scoped actions (filter, open, retry failures).
    for count_key in ("taskCount", "projectCount", "totalTasks", "failedTasks", "runningTasks"):
        value = safe.get(count_key)
        if isinstance(value, int) and value > 0:
            lines.append(f"{count_key}: {value}")

    # Recent conversation lets the completer predict follow-up intent (e.g. after a compound lookup,
    # the user is likely asking about its targets/activity; after a task analysis, about next steps).
    # Only the most recent exchange is surfaced — enough to anticipate the follow-up, not so much
    # that the prompt balloons on every keystroke.
    conversation = safe.get("copilot_conversation")
    if isinstance(conversation, dict):
        recent = conversation.get("recent_messages")
        if isinstance(recent, list):
            for msg in recent[-4:]:
                if not isinstance(msg, dict):
                    continue
                role = str(msg.get("role") or "").strip()
                content = str(msg.get("content") or "").strip()
                if role and content:
                    lines.append(f"recent {role}: {content[:140]}")

    summary = "\n".join(lines)
    if len(summary) > _MAX_CONTEXT_SUMMARY_CHARS:
        summary = summary[:_MAX_CONTEXT_SUMMARY_CHARS].rstrip() + "…"
    return summary


def _build_system_prompt(orientation: str) -> str:
    """Domain-aware orientation for the inline completer.

    The capability surface is derived from the registered catalog (single source of truth) so the
    prompt never hardcodes specific engines, compounds, proteins, or example pairs — those overfit
    and fail on neighbors. The model infers intent from the partial text + the live project context
    + the recent conversation.
    """
    orientation = (orientation or "").strip()
    capability_block = (
        f"V-Bio's capability surface (what the user can meaningfully ask for):\n{orientation}"
        if orientation
        else "V-Bio is a structural-biology workbench."
    )
    return (
        "You are the inline autocomplete for V-Bio. The user is typing a request to the Copilot and "
        "you propose a short continuation of the text they are typing.\n"
        f"{capability_block}\n\n"
        "You receive the user's live project context (workflow, page, current task/draft, visible "
        "resources) AND the recent conversation. Use BOTH to predict what they are most likely to "
        "say next.\n\n"
        "WHAT V-BIO USERS DO (predict toward these patterns):\n"
        "- Ask questions about biological entities: look up a protein sequence, a compound SMILES, a "
        "structure, known inhibitors/targets, bioactivity, literature, or clinical trials. When the "
        "user names an entity or a database concept, complete toward the lookup question.\n"
        "- Analyze the current task's results: interpret confidence (pLDDT, ipTM, PAE), affinity "
        "scores, failure reasons, and component setup. On a task_detail page with a completed/failed "
        "task, a question about results or 'why it failed' is highly likely.\n"
        "- Query metrics and compare: average potency, best pLDDT, count of failures, comparison "
        "between tasks or compounds. After a lookup or analysis, a follow-up about a related metric "
        "or entity is the most probable next turn — use the recent conversation to anticipate it.\n"
        "- Act on data: change a parameter, run/rerun, filter a list, create a task. Complete toward "
        "the concrete action the current page offers.\n\n"
        "PREDICTION RULES:\n"
        "- Name the workflow's real concepts where they fit (ligand, binder, target, receptor, seed, "
        "backend, components, affinity mode, iterations) — not generic terms.\n"
        "- After the conversation retrieved an entity, prefer follow-ups about that entity's "
        "properties (targets, activity, structure, related compounds).\n"
        "- Match the user's language (Chinese or English) and tone.\n"
        "- Never invent identifiers, accessions, or compound names that are not in the context.\n\n"
        "Continue what the user is typing with a SHORT, natural suffix — a few words to one short "
        "clause — that completes their intent. Output ONLY the continuation suffix; never repeat "
        "text the user already typed. No quotes, labels, markdown, or commentary. If the input is "
        "already complete or is clearly outside V-Bio's scope, output nothing."
    )


class CopilotCompleter:
    """One-shot chat-completion autocomplete, isolated from the planner model call."""

    def __init__(
        self,
        *,
        chat_api_url: str,
        chat_api_key: str,
        chat_model: str,
        timeout_seconds: float,
        session: requests.Session,
        logger: Any,
        capability_orientation: str = "",
    ) -> None:
        self.chat_api_url = (chat_api_url or "").rstrip("/")
        self.chat_api_key = (chat_api_key or "").strip()
        self.chat_model = (chat_model or "").strip()
        # Remember the env-var defaults so update_runtime_overrides can revert to them
        # when the user clears a field in the settings UI.
        self._default_api_url = self.chat_api_url
        self._default_api_key = self.chat_api_key
        self._default_model = self.chat_model
        self.timeout_seconds = float(timeout_seconds)
        self.session = session
        self.logger = logger
        # Per-call outbound proxy (from runtime settings). None means direct connection.
        self._proxies: Optional[Dict[str, str]] = None
        # Derived from the registered capability catalog (single source of truth). Falls back to the
        # catalog default when the caller (e.g. tests) omits it.
        self.system_prompt = _build_system_prompt(
            capability_orientation or build_capability_orientation()
        )

    @property
    def configured(self) -> bool:
        return bool(self.chat_api_url and self.chat_model)

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
        defaults stored in ``__init__`` when empty, so clearing a field reverts
        the singleton to its original configuration.
        """
        self._proxies = proxies
        self.chat_api_url = chat_api_url.strip().rstrip("/") or self._default_api_url
        self.chat_api_key = chat_api_key.strip() or self._default_api_key
        self.chat_model = chat_model.strip() or self._default_model

    def complete(
        self,
        *,
        context_type: str,
        content: str,
        context_payload: Any = None,
        user_id: str = "",
        username: str = "",
    ) -> str:
        """Return a short continuation of ``content``, or "" on any failure/empty result.

        The completer now receives the same ``context_payload`` the planner does, so its suggestions
        are anchored to the project the user is actually in (workflow, page, current task/draft,
        visible resources) rather than a generic capability blurb.
        """
        if not self.configured:
            return ""
        prompt_content = str(content or "").strip()[:_MAX_CONTENT_CHARS]
        if not prompt_content:
            return ""
        safe_payload = sanitize_context_payload(context_payload) if context_payload is not None else {}
        workflow_key = infer_workflow_key(safe_payload, default="")
        ctx_hint = _page_context_hint(context_type, workflow_key)
        context_summary = _summarize_context_payload(safe_payload)
        # Make the workflow's parameter keys visible so suggestions reference real knobs (seed,
        # affinityMode, peptideBinderLength, etc.) instead of generic guesses.
        workflow_params = WORKFLOW_PARAMETER_KEYS.get(workflow_key) or []
        param_hint = (
            f"workflow parameters: {', '.join(workflow_params)}"
            if workflow_params and workflow_key else ""
        )
        user_block_parts = [f"[the user is on: {ctx_hint}]"]
        if param_hint:
            user_block_parts.append(f"[{param_hint}]")
        if context_summary:
            user_block_parts.append(f"[live project context]\n{context_summary}")
        user_block_parts.append(f"The user is typing:\n{prompt_content}")
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": "\n\n".join(user_block_parts)},
        ]
        raw = self._call_model(messages)
        return self._normalize_completion(raw, prompt_content)

    def _call_model(self, messages: List[Dict[str, str]]) -> str:
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.chat_api_key:
            headers["Authorization"] = f"Bearer {self.chat_api_key}"
        body: Dict[str, Any] = {
            "model": self.chat_model,
            "messages": messages,
            "max_tokens": _MAX_COMPLETION_TOKENS,
            "temperature": 0.2,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        try:
            response = self.session.post(
                self.chat_api_url,
                headers=headers,
                json=body,
                timeout=self.timeout_seconds,
                proxies=self._proxies,
            )
        except requests.RequestException as exc:
            self.logger.warning("Copilot completion request failed: %s", str(exc)[:200])
            return ""
        if not response.ok:
            self.logger.debug(
                "Copilot completion model rejected the request: status=%s body=%s",
                response.status_code,
                str(getattr(response, "text", "") or "")[:200],
            )
            return ""
        try:
            payload = response.json()
        except (TypeError, ValueError):
            return ""
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not choices or not isinstance(choices[0], dict):
            return ""
        message = choices[0].get("message")
        content = message.get("content") if isinstance(message, dict) else None
        return str(content or "").strip()

    @staticmethod
    def _normalize_completion(content: str, prefix: str) -> str:
        """Trim, drop an echoed copy of the in-progress text, collapse to one line, cap length."""
        text = str(content or "").strip()
        if not text:
            return ""
        # Strip surrounding quotes the model sometimes adds.
        if len(text) >= 2 and text[0] in "\"'“”‘" and text[-1] in "\"'””’":
            text = text[1:-1].strip()
        # Drop an echoed copy of the in-progress text (only for a non-trivial prefix, to avoid
        # stripping a coincidental shared word).
        prefix_stripped = str(prefix or "").strip()
        if len(prefix_stripped) >= 4 and text.startswith(prefix_stripped):
            text = text[len(prefix_stripped):].strip()
        # A chat input continues on one line; collapse whitespace/newlines.
        text = " ".join(text.split())
        if len(text) > _MAX_COMPLETION_CHARS:
            text = text[:_MAX_COMPLETION_CHARS].rstrip()
        return text


def completion_config_from_env(env_getter) -> Tuple[str, str, str]:
    """Resolve (api_url, api_key, model) for completion, falling back to the planner values.

    ``env_getter`` is ``os.environ.get`` in the app (injected so this is testable without monkey-
    patching the global environ).
    """
    api_url = str(env_getter("VBIO_COPILOT_COMPLETE_API_URL", "") or "").strip()
    api_key = str(env_getter("VBIO_COPILOT_COMPLETE_API_KEY", "") or "").strip()
    model = str(env_getter("VBIO_COPILOT_COMPLETE_MODEL", "") or "").strip()
    return api_url, api_key, model


# Module-level logger for ad-hoc use; the app passes its own configured logger into the instance.
LOGGER = logging.getLogger(__name__)
