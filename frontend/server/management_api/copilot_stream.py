"""SSE streaming for the Copilot planner — live progress for a single turn.

Mirrors the project's established monitor_stream pattern (queue.Queue + worker thread +
stream_with_context) but for a finite single-turn stream: the planner runs in a worker thread,
each trace step is pushed to the queue as it records, and the generator emits SSE frames until the
turn completes with a ``result`` (or ``error``) frame.

The planner itself is unchanged — ``CopilotAssistant.plan_turn(on_event=...)`` supplies the live
trace observer; this module only frames and transports it. Kept Flask-free so the framing and
queue/sentinel logic is unit-testable without a running server.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from typing import List, Any, Callable, Dict, Iterator

from management_api.copilot_error import safe_error_message

LOGGER = logging.getLogger(__name__)

# Placed on the queue to signal the worker thread has finished (after result/error).
_STREAM_SENTINEL = object()


def sse_frame(event: str, data: Any) -> str:
    """One SSE frame, following the project's monitor-stream wire convention."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"event: {event}\ndata: {payload}\n\n"


# ── Steering registry (pi steering alignment) ────────────────────────────────────────────
# A user interjects mid-turn by POSTing to /vbio-api/copilot/steer with the streaming turn's
# key; the loop drains the queue between planner rounds. Registry lifetime == stream lifetime.
_STEERING_MAX_ENTRIES = 5
_steering_lock = threading.Lock()
_steering_queues: Dict[str, "queue.Queue[str]"] = {}


def submit_follow_up(turn_key: str, text: str) -> bool:
    """Queue work for AFTER the in-flight turn would otherwise stop (pi follow-ups)."""
    key = str(turn_key or "").strip() + _FOLLOWUP_SUFFIX
    payload = str(text or "").strip()
    if not turn_key or not payload:
        return False
    with _steering_lock:
        q = _steering_queues.get(key)
        if q is None:
            return False
        if q.qsize() >= _STEERING_MAX_ENTRIES:
            return False
        q.put(payload)
        return True


def _register_steering(turn_key: str) -> "queue.Queue[str]":
    return register_steering(turn_key)


# ── Abort registry (pi destructive-interrupt alignment) ──────────────────────────────────
# Steering is cooperative (drained between rounds); STOP is the independent destructive
# channel. The streaming generator registers its abort event here so an explicit
# POST /vbio-api/copilot/stop can end the turn even while the SSE connection is still open
# (and the in-flight round's remaining model calls are skipped at the next check point).
_abort_lock = threading.Lock()
_abort_events: Dict[str, threading.Event] = {}


def register_abort_event(turn_key: str, event: threading.Event) -> None:
    key = str(turn_key or "").strip()
    if not key:
        return
    with _abort_lock:
        _abort_events[key] = event


def request_abort(turn_key: str) -> bool:
    """Signal the named in-flight turn to stop. False when the key is unknown."""
    key = str(turn_key or "").strip()
    with _abort_lock:
        event = _abort_events.get(key)
    if event is None:
        return False
    event.set()
    return True


def _unregister_abort_event(turn_key: str) -> None:
    with _abort_lock:
        _abort_events.pop(str(turn_key or "").strip(), None)


_FOLLOWUP_SUFFIX = "::followup"


def submit_steering(turn_key: str, text: str) -> bool:
    """Queue one interjection for the named in-flight turn. False when the key is unknown
    (turn finished/never existed) or the queue is full — the endpoint reports both honestly.
    Follow-up keys are a separate rail: an interjection must never land in the follow-up
    queue (it would then only drain at would-complete, the wrong time)."""
    key = str(turn_key or "").strip()
    if key.endswith(_FOLLOWUP_SUFFIX):
        return False
    payload = str(text or "").strip()
    if not key or not payload:
        return False
    with _steering_lock:
        q = _steering_queues.get(key)
        if q is None:
            return False
        if q.qsize() >= _STEERING_MAX_ENTRIES:
            return False
        q.put(payload)
        return True


def register_steering(turn_key: str) -> "queue.Queue[str]":
    """Idempotent: the endpoint registers eagerly, the generator re-registers on first run —
    both must land on the SAME queue or early steers would be lost."""
    with _steering_lock:
        q = _steering_queues.get(turn_key)
        if q is None:
            q = queue.Queue()
            _steering_queues[turn_key] = q
        return q


_register_steering = register_steering


def _unregister_steering(turn_key: str) -> None:
    with _steering_lock:
        _steering_queues.pop(turn_key, None)


def copilot_event_stream(
    plan: Callable[[Callable[[Dict[str, Any]], None], threading.Event], Dict[str, Any]],
    *,
    keepalive_seconds: float = 15.0,
    turn_key: str = "",
) -> Iterator[str]:
    """Run a blocking planner call in a worker thread, streaming its trace steps as SSE frames.

    ``plan`` receives an ``on_step`` observer it must invoke for each trace step, an ``abort`` event
    it should poll between rounds, and must return the final turn result dict. The stream emits
    ``event: trace`` frames as steps record, then a single terminal ``event: result`` (or
    ``event: error``) frame, then closes. If the consumer stops reading (client disconnect), the
    generator is closed and ``abort`` is set so the worker stops making model calls.
    """
    event_queue: "queue.Queue[Any]" = queue.Queue()
    abort = threading.Event()
    if turn_key:
        register_abort_event(turn_key, abort)
    steering_queue = _register_steering(turn_key) if turn_key else None

    def on_step(step: Dict[str, Any]) -> None:
        event_queue.put(("trace", step))

    def drain_steering() -> List[str]:
        if steering_queue is None:
            return []
        drained: List[str] = []
        while True:
            try:
                drained.append(steering_queue.get_nowait())
            except queue.Empty:
                return drained

    followup_queue = register_steering(turn_key + "::followup") if turn_key else None

    def drain_follow_ups() -> List[str]:
        if followup_queue is None:
            return []
        drained: List[str] = []
        while True:
            try:
                drained.append(followup_queue.get_nowait())
            except queue.Empty:
                return drained

    def worker() -> None:
        try:
            result = plan(on_step, abort, drain_steering, drain_follow_ups)
            event_queue.put(("result", result))
        except Exception as exc:  # surface any planner failure as an honest SSE error frame
            LOGGER.exception("Copilot stream planner failed")
            event_queue.put((
                "error",
                {"error": safe_error_message(exc, default_msg="Copilot turn failed.")},
            ))
        finally:
            event_queue.put(_STREAM_SENTINEL)

    thread = threading.Thread(target=worker, name="vbio-copilot-stream", daemon=True)
    thread.start()

    try:
        while True:
            try:
                item = event_queue.get(timeout=keepalive_seconds)
            except queue.Empty:
                # Keep the connection alive during a long model round without sending a trace step.
                yield ": keepalive\n\n"
                continue
            if item is _STREAM_SENTINEL:
                return
            kind, data = item
            yield sse_frame(kind, data)
    finally:
        # Consumer gone (disconnect) or stream finished — stop the worker's remaining model calls.
        abort.set()
        if turn_key:
            _unregister_steering(turn_key)
            _unregister_steering(turn_key + "::followup")
            _unregister_abort_event(turn_key)
