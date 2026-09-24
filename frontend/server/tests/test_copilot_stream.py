"""Tests for the Copilot SSE stream framing + queue/sentinel logic (Flask-free).

The endpoint is thin glue over ``copilot_event_stream``; these tests pin the wire format and the
worker-thread → queue → generator flow, including the error and keepalive paths.
"""

from __future__ import annotations

import json
import time
import unittest
from typing import List, Tuple

from unittest.mock import patch

import vbio_management_api
from management_api.copilot_stream import copilot_event_stream, sse_frame
from vbio_management_api import app


def _parse_frames(frames: List[str]) -> List[Tuple[str, str]]:
    """Parse emitted SSE frames into ``(event, data_json)`` pairs, skipping comment lines."""
    parsed: List[Tuple[str, str]] = []
    for frame in frames:
        for chunk in frame.split("\n\n"):
            chunk = chunk.strip()
            if not chunk:
                continue
            event = ""
            data = ""
            for line in chunk.split("\n"):
                if line.startswith("event: "):
                    event = line[len("event: "):]
                elif line.startswith("data: "):
                    data = line[len("data: "):]
            if event:
                parsed.append((event, data))
    return parsed


class CopilotStreamFrameTests(unittest.TestCase):
    def test_sse_frame_follows_project_wire_convention(self) -> None:
        frame = sse_frame("trace", {"round": 0, "event": "model_request", "detail": {}})
        self.assertTrue(frame.startswith("event: trace\n"))
        self.assertIn('data: {"round":0,"event":"model_request"', frame)
        self.assertTrue(frame.endswith("\n\n"))


class CopilotEventStreamTests(unittest.TestCase):
    def test_streams_trace_steps_then_terminal_result(self) -> None:
        def plan(on_step, abort, get_steering=None, get_follow_ups=None):
            on_step({"round": 0, "event": "model_request", "detail": {}})
            on_step({"round": 0, "event": "skill_observations", "detail": {}})
            return {"content": "done", "actions": [], "state": "complete", "trace": []}

        frames = list(copilot_event_stream(plan, keepalive_seconds=1))
        events = [event for event, _ in _parse_frames(frames)]
        self.assertEqual(events, ["trace", "trace", "result"])
        # The terminal result frame carries the full turn payload, JSON-serializable.
        result_data = json.loads(_parse_frames(frames)[-1][1])
        self.assertEqual(result_data["content"], "done")
        self.assertEqual(result_data["state"], "complete")

    def test_surfaces_planner_failure_as_an_error_frame(self) -> None:
        def plan(_on_step, abort, get_steering=None, get_follow_ups=None):
            raise RuntimeError("plan exploded")

        frames = list(copilot_event_stream(plan, keepalive_seconds=1))
        parsed = _parse_frames(frames)
        self.assertEqual([event for event, _ in parsed], ["error"])
        self.assertIn("plan exploded", parsed[0][1])

    def test_keepalive_emitted_while_a_round_is_in_flight(self) -> None:
        def plan(on_step, abort, get_steering=None, get_follow_ups=None):
            time.sleep(0.35)  # exceeds the short keepalive window below
            on_step({"round": 0, "event": "model_request", "detail": {}})
            return {"content": "ok", "actions": [], "state": "complete", "trace": []}

        frames = list(copilot_event_stream(plan, keepalive_seconds=0.1))
        # A keepalive comment frame is emitted before the first real event.
        self.assertTrue(any(": keepalive" in frame for frame in frames))
        self.assertEqual([event for event, _ in _parse_frames(frames)][-1], "result")

    def test_abort_is_signalled_when_the_consumer_disconnects(self) -> None:
        # A planner that would do more work; the consumer stops after the first frame. Closing the
        # generator (client disconnect) must set abort so the worker stops making model calls.
        import threading

        captured: list[threading.Event] = []

        def plan(on_step, abort, get_steering=None, get_follow_ups=None):
            captured.append(abort)
            on_step({"round": 0, "event": "model_request", "detail": {}})
            abort.wait(timeout=2.0)  # blocks until the generator close sets abort
            return {"content": "unreached", "actions": [], "state": "complete", "trace": []}

        stream = copilot_event_stream(plan, keepalive_seconds=1)
        for _frame in stream:  # consume the single trace frame, then stop
            break
        stream.close()
        self.assertTrue(captured)  # the worker received an abort event
        self.assertTrue(captured[0].is_set())  # closing the stream set it


if __name__ == "__main__":
    unittest.main()


class SteeringTests(unittest.TestCase):
    """pi steering alignment: interjections queue per in-flight turn and drain between rounds."""

    def test_submit_and_drain(self):
        from management_api.copilot_stream import _register_steering, _unregister_steering, submit_steering

        q = _register_steering("t-1")
        self.assertTrue(submit_steering("t-1", "用 4NFF 那个"))
        self.assertTrue(submit_steering("t-1", "人的就行"))
        self.assertFalse(submit_steering("t-unknown", "x"))  # unknown key → honest False
        self.assertEqual(q.get_nowait(), "用 4NFF 那个")
        self.assertEqual(q.get_nowait(), "人的就行")
        _unregister_steering("t-1")
        self.assertFalse(submit_steering("t-1", "late"))  # post-stream submit rejected

    def test_queue_is_bounded(self):
        from management_api.copilot_stream import _STEERING_MAX_ENTRIES, _register_steering, _unregister_steering, submit_steering

        q = _register_steering("t-2")
        for i in range(_STEERING_MAX_ENTRIES):
            self.assertTrue(submit_steering("t-2", f"m{i}"))
        self.assertFalse(submit_steering("t-2", "overflow"))
        _unregister_steering("t-2")

    def test_steer_endpoint_routes(self):
        from management_api.copilot_stream import _register_steering, _unregister_steering, submit_steering

        _register_steering("t-3")
        # Unknown key → 409 (honest, actionable); known key → 200 queued.
        with patch("vbio_management_api.copilot_assistant") as _ca, patch(
            "vbio_management_api._copilot_is_configured", return_value=True
        ), patch("vbio_management_api._copilot_request_too_large", return_value=False):
            client = app.test_client()
            r1 = client.post("/vbio-api/copilot/steer", json={"turn_key": "nope", "text": "hi"})
            self.assertEqual(r1.status_code, 409)
            r2 = client.post("/vbio-api/copilot/steer", json={"turn_key": "t-3", "text": "改用人的"})
            self.assertEqual(r2.status_code, 200)
            self.assertTrue(r2.get_json()["queued"])
            r3 = client.post("/vbio-api/copilot/steer", json={"turn_key": "t-3"})
            self.assertEqual(r3.status_code, 400)
        _unregister_steering("t-3")


class SteeringLoopIntegrationTests(unittest.TestCase):
    """The drained interjection must reach the planner between rounds and be visible to the
    candidate pre-choice guards (_turn_user_text joins ALL user messages)."""

    def test_interjection_becomes_part_of_turn_user_text(self):
        import sys as _sys
        _sys.path.insert(0, '.')
        from management_api.copilot import _turn_user_text

        planner_messages = [
            {"role": "system", "content": "prompt"},
            {"role": "user", "content": "对接 KLK2"},
            {"role": "assistant", "content": "round1"},
            {"role": "user", "content": "e2e (interjects): 用 4NFF 那个"},
        ]
        joined = _turn_user_text(planner_messages)
        self.assertIn("用 4NFF", joined)
        self.assertIn("KLK2", joined)


class FollowUpTests(unittest.TestCase):
    """pi follow-up alignment: queued work revives the loop at would-complete."""

    def test_submit_follow_up_routes_to_sibling_queue(self):
        from management_api.copilot_stream import (
            register_steering, submit_follow_up, submit_steering, _unregister_steering,
        )
        register_steering("t-f")
        register_steering("t-f::followup")
        # A steering submit must NOT land in the follow-up queue and vice versa.
        self.assertTrue(submit_steering("t-f", "interject"))
        self.assertTrue(submit_follow_up("t-f", "after this"))
        self.assertFalse(submit_steering("t-f::followup", "wrong rail"))
        _unregister_steering("t-f")
        _unregister_steering("t-f::followup")


class StopChannelTests(unittest.TestCase):
    """The explicit destructive-interrupt rail (POST /vbio-api/copilot/stop).

    Steering is cooperative (drained between rounds); stop is the independent channel:
    it sets the registered abort event directly, works even while the SSE connection is
    still open, and reports honestly when the key is unknown.
    """

    def test_stop_signals_registered_turn(self):
        import threading

        from management_api.copilot_stream import (
            _unregister_abort_event,
            register_abort_event,
            request_abort,
        )

        event = threading.Event()
        register_abort_event("t-stop", event)
        self.assertFalse(event.is_set())
        self.assertTrue(request_abort("t-stop"))
        self.assertTrue(event.is_set())
        # Idempotent: stopping again still reports True while registered.
        self.assertTrue(request_abort("t-stop"))
        _unregister_abort_event("t-stop")
        self.assertFalse(request_abort("t-stop"))

    def test_stop_unknown_key_is_honest_false(self):
        from management_api.copilot_stream import request_abort

        self.assertFalse(request_abort("never-registered-key"))
        self.assertFalse(request_abort(""))


class ConfirmationFooterTests(unittest.TestCase):
    """Every await_confirmation terminal carries the deterministic bilingual footer.

    The model's prose framing is audited by zh/en patterns; the footer is the
    language-independent user-facing anchor (code-appended, never model-generated).
    """

    def test_footer_appended_once_and_idempotent(self):
        from management_api.copilot import _confirmation_footer

        once = _confirmation_footer("计划如下。")
        self.assertIn("确认后才会执行", once)
        self.assertIn("after you confirm", once)
        twice = _confirmation_footer(once)
        self.assertEqual(once, twice)

    def test_footer_is_bilingual_and_deterministic(self):
        from management_api.copilot import CONFIRMATION_PENDING_FOOTER

        self.assertIn("确认后才会执行", CONFIRMATION_PENDING_FOOTER)
        self.assertIn("after you confirm", CONFIRMATION_PENDING_FOOTER)
        self.assertIn("⚠️", CONFIRMATION_PENDING_FOOTER)
