"""Best-effort inline auto-complete for the Copilot composer.

A deliberately tiny counterpart to ``CopilotAssistant``: no planner loop, no harness, no schema —
one OpenAI-compatible chat completion that returns a short continuation of the user's in-progress
input. It is OPTIONAL assistance: every failure path returns an empty string so the composer never
blocks or surfaces an error on the user's behalf.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

import requests

from management_api.copilot_capabilities import build_capability_orientation


# A continuation is a short phrase; cap both the prompt and the model output so this stays fast.
_MAX_CONTENT_CHARS = 500
_MAX_COMPLETION_CHARS = 60
_MAX_COMPLETION_TOKENS = 20

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


def _build_system_prompt(orientation: str) -> str:
    """General, domain-on orientation for the inline completer.

    The capability surface is derived from the registered catalog (single source of truth) so the
    prompt never hardcodes specific engines, compounds, proteins, or example pairs — those overfit
    and fail on neighbors. The model infers intent from the partial text + capability surface.
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
        "Continue what the user is typing with a SHORT, natural suffix — a few words to one short "
        "clause — that completes their intent toward a plausible V-Bio action or question. Infer the "
        "intent from the partial text and the capability surface; do NOT assume any specific molecule, "
        "protein, task, or example. Match the user's language (Chinese or English) and tone. Be concise.\n"
        "Output ONLY the continuation suffix; never repeat text the user already typed. No quotes, "
        "labels, markdown, or commentary. If the input is already complete or is clearly outside V-Bio's "
        "scope, output nothing."
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
        self.timeout_seconds = float(timeout_seconds)
        self.session = session
        self.logger = logger
        # Derived from the registered capability catalog (single source of truth). Falls back to the
        # catalog default when the caller (e.g. tests) omits it.
        self.system_prompt = _build_system_prompt(
            capability_orientation or build_capability_orientation()
        )

    @property
    def configured(self) -> bool:
        return bool(self.chat_api_url and self.chat_model)

    def complete(self, *, context_type: str, content: str) -> str:
        """Return a short continuation of ``content``, or "" on any failure/empty result."""
        if not self.configured:
            return ""
        prompt_content = str(content or "").strip()[:_MAX_CONTENT_CHARS]
        if not prompt_content:
            return ""
        ctx_key = str(context_type or "").strip()
        ctx_hint = _CONTEXT_HINTS.get(ctx_key) or ctx_hint_fallback(ctx_key)
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": (
                    f"[the user is on: {ctx_hint}]\n"
                    f"The user is typing:\n{prompt_content}"
                ),
            },
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
