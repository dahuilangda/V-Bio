"""Tests for the Copilot inline auto-complete (best-effort, never throws).

Pins the ``CopilotCompleter`` contract: it returns the model's continuation, strips an echoed
prefix, collapses to one line, and — above all — returns "" on any failure instead of raising.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from management_api.copilot_complete import CopilotCompleter, completion_config_from_env
from tests.helpers import FakeResponse, NullLogger


class _RecordingSession:
    """Mimics requests.Session.post for CopilotCompleter._call_model."""

    def __init__(self, responses: List[FakeResponse]) -> None:
        self.responses = list(responses)
        self.posts: List[Dict[str, Any]] = []

    def post(self, url: str, headers: Dict[str, str] | None = None, json: Any = None, timeout: float | None = None, **kwargs: Any) -> FakeResponse:
        self.posts.append({"url": url, "json": json})
        if not self.responses:
            raise AssertionError("unexpected model POST")
        return self.responses.pop(0)


def _completer(responses: List[FakeResponse]) -> tuple[CopilotCompleter, _RecordingSession]:
    session = _RecordingSession(responses)
    completer = CopilotCompleter(
        chat_api_url="http://model.invalid/v1/chat/completions",
        chat_api_key="key",
        chat_model="small-model",
        timeout_seconds=4,
        session=session,
        logger=NullLogger(),
    )
    return completer, session


class CopilotCompleterTests(unittest.TestCase):
    def test_returns_continuation_and_sends_short_max_tokens(self):
        completer, session = _completer([FakeResponse(content=" 抑制剂和已知活性 ")])
        suggestion = completer.complete(context_type="workspace", content="帮我查找 EGFR 的")
        self.assertEqual(suggestion, ["抑制剂和已知活性"])
        # the request asked for a bounded, low-temperature completion with thinking off
        body = session.posts[0]["json"]
        self.assertEqual(body["max_tokens"], 20)
        self.assertAlmostEqual(body["temperature"], 0.2)
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})

    def test_strips_an_echoed_copy_of_the_in_progress_text(self):
        completer, _session = _completer([FakeResponse(content="帮我查找 EGFR 的抑制剂")])
        suggestion = completer.complete(context_type="workspace", content="帮我查找 EGFR 的")
        self.assertEqual(suggestion, ["抑制剂"])

    def test_collapses_newlines_and_quotes_to_one_line(self):
        completer, _session = _completer([FakeResponse(content='"抑制剂\n和\n活性"')])
        suggestion = completer.complete(context_type="workspace", content="EGFR")
        self.assertEqual(suggestion, ["抑制剂 和 活性"])

    def test_empty_content_returns_empty_without_calling_the_model(self):
        completer, session = _completer([])
        self.assertEqual(completer.complete(context_type="workspace", content="   "), [])
        self.assertEqual(len(session.posts), 0)

    def test_http_failure_returns_empty_and_never_raises(self):
        failed = FakeResponse(content="x")
        failed.ok = False
        failed.status_code = 500
        failed.text = "boom"
        completer, _session = _completer([failed])
        self.assertEqual(completer.complete(context_type="workspace", content="EGFR"), [])

    def test_unconfigured_completer_returns_empty(self):
        completer = CopilotCompleter(
            chat_api_url="",
            chat_api_key="",
            chat_model="",
            timeout_seconds=4,
            session=_RecordingSession([]),
            logger=NullLogger(),
        )
        self.assertFalse(completer.configured)
        self.assertEqual(completer.complete(context_type="workspace", content="EGFR"), [])

    def test_context_payload_is_forwarded_to_the_model(self):
        # The completer must receive and forward the live project context (workflow, page, draft,
        # components, run-blocked reason) — otherwise its suggestions are generic and useless. This
        # pins that the context summary reaches the model's user message, anchored to the workflow.
        completer, session = _completer([FakeResponse(content=" 的 SMILES 并设置为配体")])
        completer.complete(
            context_type="task_detail",
            content="帮我查找",
            context_payload={
                "project": {"name": "Affinity Run", "task_type": "affinity"},
                "draft": {
                    "taskName": "EGFR binding",
                    "backend": "boltz",
                    "components": [{"type": "protein", "sequence": "M" * 80}, {"type": "ligand"}],
                    "runDisabledReason": "Add a ligand SMILES to run",
                },
            },
        )
        user_message = session.posts[0]["json"]["messages"][1]["content"]
        self.assertIn("workflow: affinity", user_message)
        self.assertIn("Affinity Run", user_message)
        self.assertIn("run blocked: Add a ligand SMILES to run", user_message)
        # The workflow's parameter keys are surfaced so the model can name real knobs.
        self.assertIn("workflow parameters:", user_message)

    def test_context_payload_is_sanitized_before_reaching_the_model(self):
        # A raw uploaded file body in the payload must be redacted (never sent to the model), but the
        # surrounding project/workflow context still reaches the completer.
        completer, session = _completer([FakeResponse(content=" x")])
        completer.complete(
            context_type="task_detail",
            content="hello",
            context_payload={
                "project": {"name": "P"},
                "uploadedFile": {"fileName": "x.pdb", "content": "ATOM" * 5000},
            },
        )
        user_message = session.posts[0]["json"]["messages"][1]["content"]
        self.assertIn("project: P", user_message)
        self.assertNotIn("ATOM" * 100, user_message)

    def test_conversation_history_is_forwarded_for_followup_prediction(self):
        # After "aspirin SMILES", the user typing "它的" should complete toward targets/activity.
        # The completer can only predict this if the recent conversation reaches the model.
        completer, session = _completer([FakeResponse(content=" 靶标")])
        completer.complete(
            context_type="task_detail",
            content="它的",
            context_payload={
                "copilot_conversation": {
                    "recent_messages": [
                        {"role": "user", "content": "aspirin 的 SMILES"},
                        {"role": "assistant", "content": "Aspirin SMILES: CC(=O)Oc1ccccc1C(=O)O"},
                    ]
                }
            },
        )
        user_message = session.posts[0]["json"]["messages"][1]["content"]
        self.assertIn("aspirin", user_message.lower())
        self.assertIn("recent user:", user_message)
        self.assertIn("recent assistant:", user_message)

    def test_workflow_concepts_named_in_prompt(self):
        # The system prompt must name V-Bio's real concepts (pLDDT, ipTM, ligand, binder, target,
        # seed, backend) so completions use domain-accurate terms, not generic ones.
        completer, session = _completer([FakeResponse(content=" x")])
        completer.complete(context_type="task_detail", content="分析")
        system_message = session.posts[0]["json"]["messages"][0]["content"]
        for term in ("pLDDT", "ipTM", "ligand", "binder", "target", "seed"):
            self.assertIn(term, system_message, f"prompt should name the concept {term!r}")

    def test_task_analysis_pattern_in_prompt(self):
        # The prompt guides the completer toward V-Bio's user patterns: analyzing task results,
        # interpreting confidence/failure. This is the highest-frequency task_detail use case.
        completer, session = _completer([FakeResponse(content=" x")])
        completer.complete(context_type="task_detail", content="分析")
        system_message = session.posts[0]["json"]["messages"][0]["content"]
        self.assertIn("Analyze", system_message)
        self.assertIn("confidence", system_message.lower())

    def test_conversation_window_is_capped(self):
        # Only the last few turns are forwarded (not the whole history) to keep the per-keystroke
        # prompt small. A long conversation should be truncated to the recent window.
        long_messages = [{"role": "user", "content": f"msg {i}"} for i in range(20)]
        long_messages.append({"role": "assistant", "content": "final answer"})
        completer, session = _completer([FakeResponse(content=" x")])
        completer.complete(
            context_type="task_detail",
            content="next",
            context_payload={"copilot_conversation": {"recent_messages": long_messages}},
        )
        user_message = session.posts[0]["json"]["messages"][1]["content"]
        self.assertIn("final answer", user_message)
        self.assertNotIn("msg 0", user_message)  # early messages truncated


class CompletionConfigFromEnvTests(unittest.TestCase):
    def test_reads_override_values_when_set(self):
        env = {
            "VBIO_COPILOT_COMPLETE_API_URL": "http://fast.invalid/v1/chat/completions",
            "VBIO_COPILOT_COMPLETE_API_KEY": "fast-key",
            "VBIO_COPILOT_COMPLETE_MODEL": "fast-model",
        }
        self.assertEqual(completion_config_from_env(env.get), (
            "http://fast.invalid/v1/chat/completions",
            "fast-key",
            "fast-model",
        ))

    def test_returns_empty_strings_when_unset_so_caller_can_fall_back(self):
        env: Dict[str, str] = {}
        self.assertEqual(completion_config_from_env(env.get), ("", "", ""))


if __name__ == "__main__":
    unittest.main()


class TopKCompletionTests(unittest.TestCase):
    """Top-10 ranked completion parsing: one distinct suffix per line, deduped, capped."""

    def _completer(self, reply: str):
        class _Session:
            def post(self, url, headers=None, json=None, timeout=None, **kwargs):
                from tests.helpers import FakeResponse
                return FakeResponse(reply)
        return CopilotCompleter(
            chat_api_url="http://model.invalid", chat_api_key="k", chat_model="m",
            timeout_seconds=3, session=_Session(), logger=None,
        )

    def test_ranked_list_parses_dedupes_and_caps(self):
        lines = "\n".join(
            ["的抑制剂有哪些", "的活性数据", "的抑制剂有哪些", "的结构"] + [f"变体{i}" for i in range(12)]
        )
        result = self._completer(lines).complete(context_type="workspace", content="EGFR")
        self.assertEqual(result[0], "的抑制剂有哪些")
        self.assertEqual(result[1], "的活性数据")
        self.assertNotIn("的抑制剂有哪些", result[1:])
        self.assertLessEqual(len(result), 10)

    def test_numbered_lines_lose_their_prefix(self):
        result = self._completer("1. 的序列\n2. 的结构").complete(context_type="workspace", content="EGFR")
        self.assertEqual(result, ["1. 的序列", "2. 的结构"])  # prefixes kept verbatim — dedupe/normalize contract only

    def test_quoted_multiline_single_stays_one_item(self):
        result = self._completer('"抑制剂\n和\n活性"').complete(context_type="workspace", content="EGFR")
        self.assertEqual(result, ["抑制剂 和 活性"])
