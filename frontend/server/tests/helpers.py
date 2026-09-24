"""Shared test helpers for copilot integration tests."""

from __future__ import annotations

from typing import Any, Dict


class FakeResponse:
    ok = True
    status_code = 200
    text = ""

    def __init__(self, content: str = "", finish_reason: str = "", usage: Dict[str, int] | None = None):
        self._content = content
        self._finish_reason = finish_reason
        self._usage = usage

    def json(self):
        choice: Dict[str, Any] = {"message": {"content": self._content}}
        if self._finish_reason:
            choice["finish_reason"] = self._finish_reason
        payload: Dict[str, Any] = {"choices": [choice]}
        if self._usage is not None:
            payload["usage"] = self._usage
        return payload


def is_phase2_request(json_body: Any) -> bool:
    """True when the request body is a phase-2 free-text answer generation call.

    The planner loop's phase-2 enrichment is best-effort: when a scenario test's scripted
    responses are exhausted by an unscripted phase-2 call, the mock returns empty content, which
    makes the assistant fall back to the (already audited) planner message. Planner protocol
    calls (json_schema response_format) keep the strict unexpected-request guard.
    """
    return isinstance(json_body, dict) and json_body.get("response_format") == {"type": "text"}


class NullLogger:
    def info(self, *_: Any, **__: Any) -> None:
        pass

    def warning(self, *_: Any, **__: Any) -> None:
        pass

    def error(self, *_: Any, **__: Any) -> None:
        pass

    def exception(self, *_: Any, **__: Any) -> None:
        pass

    def debug(self, *_: Any, **__: Any) -> None:
        pass
