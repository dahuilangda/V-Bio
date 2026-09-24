"""Tests for the structured planner trace (agent observability).

The trace is a passive recorder of the planner loop; these tests pin its contract:
- unit behavior of PlannerTrace / compact_observations / compact_operations
- the planner records the expected event sequence for read→terminal, audit→replan,
  and malformed→retry trajectories
- the trace is JSON-serializable and returned in the turn result
"""

from __future__ import annotations

import json
import logging
import unittest
from types import SimpleNamespace
from typing import Any, Dict, List

from management_api.copilot import CopilotAssistant
from management_api.copilot_trace import (
    TRACE_WRITES_MATERIALIZED,
    TRACE_AUDIT_REJECTED,
    TRACE_MALFORMED_OUTPUT,
    TRACE_MODEL_REQUEST,
    TRACE_SKILL_OBSERVATIONS,
    TRACE_TERMINAL,
    PlannerTrace,
    compact_observations,
    compact_operations,
    compact_usage,
)


def _choice(content: str) -> Dict[str, Any]:
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}


class StubResponse:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self.ok = True
        self.status_code = 200
        self.text = ""
        self._payload = payload

    def json(self) -> Dict[str, Any]:
        return self._payload


class StubSession:
    """Hands back canned model responses in order; records the requests."""

    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        self.responses = [StubResponse(payload) for payload in responses]
        self.posts: List[Dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> StubResponse:
        self.posts.append({"url": url, **kwargs})
        if not self.responses:
            if kwargs.get("json", {}).get("response_format") == {"type": "text"}:
                # Unscripted phase-2 enrichment: empty free text falls back to the planner message.
                return StubResponse(_choice(""))
            raise AssertionError("unexpected model request")
        return self.responses.pop(0)


def _assistant(session: StubSession, *, rounds: int = 8) -> CopilotAssistant:
    return CopilotAssistant(
        chat_api_url="https://model.invalid/v1/chat/completions",
        chat_api_key="",
        chat_model="model",
        timeout_seconds=10,
        session=session,
        logger=logging.getLogger("copilot-trace-test"),
        max_planner_rounds=rounds,
    )


def _turn(content: str) -> str:
    return json.dumps({"message": content, "questions": [], "operations": []})


class PlannerTraceUnitTests(unittest.TestCase):
    def test_records_steps_in_order_and_serializes(self) -> None:
        trace = PlannerTrace()
        trace.record(0, TRACE_MODEL_REQUEST, messages_chars=42)
        trace.record(0, TRACE_SKILL_OBSERVATIONS, observations=[{"id": "a", "skill": "x", "ok": True}])
        trace.record(1, TRACE_TERMINAL, state="complete", operations=[])

        steps = trace.steps()
        self.assertEqual([s["event"] for s in steps], [TRACE_MODEL_REQUEST, TRACE_SKILL_OBSERVATIONS, TRACE_TERMINAL])
        self.assertEqual(steps[0]["round"], 0)
        self.assertEqual(steps[0]["detail"], {"messages_chars": 42})
        # Must be JSON-serializable for jsonify / frontend consumption.
        json.dumps(steps)

    def test_summary_counts_events_without_payload(self) -> None:
        trace = PlannerTrace()
        trace.record(0, TRACE_MODEL_REQUEST)
        trace.record(0, TRACE_AUDIT_REJECTED, issues=["x"])
        trace.record(0, TRACE_AUDIT_REJECTED, issues=["y"])
        trace.record(1, TRACE_SKILL_OBSERVATIONS)
        trace.record(1, TRACE_MALFORMED_OUTPUT)
        trace.record(1, TRACE_TERMINAL, state="await_confirmation")

        summary = trace.summary()
        self.assertIn("rounds=2", summary)
        self.assertIn("reads=1", summary)
        self.assertIn("audit_rejected=2", summary)
        self.assertIn("malformed=1", summary)
        self.assertIn("terminal=await_confirmation", summary)

    def test_writes_materialized_event_records(self) -> None:
        # Deferred-materialization terminal: the harness surfaces write actions built from
        # same-round reads — the event carries how many were materialized.
        trace = PlannerTrace()
        trace.record(1, TRACE_WRITES_MATERIALIZED, materialized=2)
        self.assertEqual(trace.steps()[0]["event"], "writes_materialized")
        self.assertEqual(trace.steps()[0]["detail"]["materialized"], 2)

    def test_observer_is_invoked_per_step_in_order(self) -> None:
        seen: List[Dict[str, Any]] = []
        trace = PlannerTrace(on_step=seen.append)
        trace.record(0, TRACE_MODEL_REQUEST, messages_chars=1)
        trace.record(1, TRACE_TERMINAL, state="complete")
        # The observer mirrors exactly what is recorded, in order, as the serializable dict form.
        self.assertEqual([s["event"] for s in seen], [TRACE_MODEL_REQUEST, TRACE_TERMINAL])
        self.assertEqual(seen[0], {"round": 0, "event": TRACE_MODEL_REQUEST, "detail": {"messages_chars": 1}})
        # No observer -> no side effect (default path unchanged).
        PlannerTrace().record(0, TRACE_MODEL_REQUEST)


class CompactHelpersTests(unittest.TestCase):
    def test_compact_observations_reduces_to_safe_fields(self) -> None:
        observations = {
            "find": {
                "skill": "pubchem.search",
                "ok": True,
                "count": 1,
                "successCount": 1,
                "values": [{"source": "pubchem", "results": [{"cid": "123", "smiles": "C1CC1"}]}],
                "items": [{"index": 0, "arguments": {"identifier": "benzene"}}],
            },
            "bad": {"skill": "x", "ok": False, "count": 1, "successCount": 0, "errors": [{"index": 0, "error": "boom"}]},
        }
        compacted = compact_observations(observations)
        self.assertEqual(
            compacted,
            [
                {"id": "find", "skill": "pubchem.search", "ok": True, "count": 1, "successCount": 1},
                {"id": "bad", "skill": "x", "ok": False, "count": 1, "successCount": 0},
            ],
        )
        # No record bodies or argument values leak into the trace detail.
        flat = json.dumps(compacted)
        self.assertNotIn("benzene", flat)
        self.assertNotIn("C1CC1", flat)

    def test_compact_operations_reads_skill_and_effect(self) -> None:
        operations = [
            SimpleNamespace(skill="pubchem.search", definition=SimpleNamespace(effect="read")),
            SimpleNamespace(skill="projects:create", definition=SimpleNamespace(effect="create")),
        ]
        self.assertEqual(
            compact_operations(operations),
            [{"skill": "pubchem.search", "effect": "read"}, {"skill": "projects:create", "effect": "create"}],
        )

    def test_compact_usage_maps_to_genai_names_and_drops_non_integer(self) -> None:
        # OpenAI-compatible usage → OTel GenAI token metric names; non-integer/absent fields dropped.
        self.assertEqual(
            compact_usage({"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}),
            {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
        )
        self.assertEqual(compact_usage({}), {})
        self.assertEqual(compact_usage({"prompt_tokens": "n/a"}), {})
        self.assertEqual(compact_usage(None), {})


class PlannerTraceIntegrationTests(unittest.TestCase):
    def test_read_then_terminal_emits_expected_events(self) -> None:
        # Round 0: run the registered read-only catalog skill; round 1: terminal answer.
        round0 = json.dumps(
            {
                "message": "Looking up what the platform supports.",
                "questions": [],
                "operations": [
                    {"id": "cat", "skill": "platform.capability_catalog", "arguments": {}, "depends_on": []}
                ],
            }
        )
        session = StubSession([_choice(round0), _choice(_turn("The catalog lists the registered workflows."))])
        assistant = _assistant(session)

        result = assistant.plan_turn(
            context_type="workspace",
            context_payload={},
            user_id="user",
            username="user",
            content="what can you do",
        )

        self.assertEqual(result["state"], "complete")
        self.assertIn("trace", result)
        events = [step["event"] for step in result["trace"]]
        self.assertIn(TRACE_MODEL_REQUEST, events)
        self.assertIn(TRACE_SKILL_OBSERVATIONS, events)
        self.assertEqual(events[-1], TRACE_TERMINAL)

        obs_step = next(step for step in result["trace"] if step["event"] == TRACE_SKILL_OBSERVATIONS)
        self.assertEqual(obs_step["round"], 0)
        self.assertEqual(
            obs_step["detail"]["observations"],
            [{"id": "cat", "skill": "platform.capability_catalog", "ok": True, "count": 1, "successCount": 1}],
        )
        terminal_step = result["trace"][-1]
        self.assertEqual(terminal_step["detail"]["state"], "complete")
        # Two model requests (one per round).
        self.assertEqual(events.count(TRACE_MODEL_REQUEST), 2)
        # Fully serializable.
        json.dumps(result["trace"])

    def test_audit_rejection_then_replan_is_traced(self) -> None:
        rejected = json.dumps(
            {"message": "No operation.", "questions": [], "operations": [], "additionalProperties": False}
        )
        session = StubSession([_choice(rejected), _choice(_turn("Done.")), _choice("Done.")])
        assistant = _assistant(session)

        result = assistant.plan_turn(
            context_type="workspace",
            context_payload={},
            user_id="user",
            username="user",
            content="hello",
        )

        events = [step["event"] for step in result["trace"]]
        self.assertIn(TRACE_AUDIT_REJECTED, events)
        self.assertEqual(events[-1], TRACE_TERMINAL)
        rejected_step = next(step for step in result["trace"] if step["event"] == TRACE_AUDIT_REJECTED)
        self.assertTrue(rejected_step["detail"]["issues"])

    def test_json_output_accepted(self) -> None:
        # With grammar, the model outputs valid JSON. The planner parses it directly.
        session = StubSession([_choice(_turn("你好！我是 V-Bio Copilot。")), _choice("你好！我是 V-Bio Copilot。")])
        assistant = _assistant(session)

        result = assistant.plan_turn(
            context_type="workspace",
            context_payload={},
            user_id="user",
            username="user",
            content="hello",
        )

        events = [step["event"] for step in result["trace"]]
        self.assertEqual(events[-1], TRACE_TERMINAL)
        self.assertEqual(result["state"], "complete")
        self.assertIn("V-Bio", result["content"])

    def test_model_token_usage_is_captured_in_trace(self) -> None:
        # When the model server returns a usage object, it is mapped to GenAI token metrics on the
        # model_request step; absent usage degrades to an empty dict (never breaks the turn).
        payload = {
            "choices": [{"message": {"content": _turn("Done.")}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
        }
        session = StubSession([payload, _choice("Done.")])
        assistant = _assistant(session)

        result = assistant.plan_turn(
            context_type="workspace",
            context_payload={},
            user_id="user",
            username="user",
            content="hello",
        )

        request_step = next(step for step in result["trace"] if step["event"] == TRACE_MODEL_REQUEST)
        self.assertEqual(
            request_step["detail"]["usage"],
            {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
        )

    def test_observer_streams_steps_live_and_in_order(self) -> None:
        # The on_event observer mirrors the final trace exactly, firing live as each step records —
        # this is the hook the SSE endpoint streams over.
        round0 = json.dumps(
            {
                "message": "Looking up.",
                "questions": [],
                "operations": [
                    {"id": "cat", "skill": "platform.capability_catalog", "arguments": {}, "depends_on": []}
                ],
            }
        )
        session = StubSession([_choice(round0), _choice(_turn("Done."))])
        assistant = _assistant(session)
        live: List[Dict[str, Any]] = []
        result = assistant.plan_turn(
            context_type="workspace",
            context_payload={},
            user_id="user",
            username="user",
            content="hi",
            on_event=live.append,
        )
        self.assertEqual([s["event"] for s in live], [s["event"] for s in result["trace"]])
        self.assertEqual(live[0]["event"], TRACE_MODEL_REQUEST)
        self.assertEqual(live[-1]["event"], TRACE_TERMINAL)


class CopilotMemoryTests(unittest.TestCase):
    def test_compact_memory_records_projects_identity_and_truncates_long_fields(self) -> None:
        from management_api.copilot import _compact_memory_records

        observations = {
            "find": {
                "ok": True,
                "values": [
                    {"source": "pubchem", "results": [{"cid": "123", "title": "benzene ring", "smiles": "C" * 80, "sequence": "M" * 80}]}
                ],
            },
            "bad": {"ok": False, "values": []},
        }
        records = _compact_memory_records(observations)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["source"], "pubchem")
        self.assertEqual(record["cid"], "123")
        self.assertEqual(record["title"], "benzene ring")
        # Long fields are dropped ENTIRELY (identity-only memory): a 60-char prefix would be
        # a structurally invalid value one weak paste away from corrupting a write argument.
        self.assertNotIn("smiles", record)
        self.assertNotIn("sequence", record)
        # Failed observations are skipped.
        self.assertNotIn("C" * 80, json.dumps(records))

    def test_compact_memory_records_caps_total_records(self) -> None:
        from management_api.copilot import MAX_MEMORY_RECORDS, _compact_memory_records

        observations = {
            "big": {
                "ok": True,
                "values": [{"source": "x", "results": [{"accession": f"A{i}"} for i in range(MAX_MEMORY_RECORDS + 5)]}],
            }
        }
        self.assertEqual(len(_compact_memory_records(observations)), MAX_MEMORY_RECORDS)

    def test_turn_result_carries_observations_for_memory(self) -> None:
        session = StubSession([_choice(_turn("Done.")), _choice("Done.")])
        result = _assistant(session).plan_turn(
            context_type="workspace",
            context_payload={},
            user_id="user",
            username="user",
            content="hi",
        )
        self.assertIn("observations", result)
        self.assertIsInstance(result["observations"], list)


if __name__ == "__main__":
    unittest.main()
