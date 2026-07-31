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
from typing import Any, Callable, Dict, Iterator

LOGGER = logging.getLogger(__name__)

# Placed on the queue to signal the worker thread has finished (after result/error).
_STREAM_SENTINEL = object()


def sse_frame(event: str, data: Any) -> str:
    """One SSE frame, following the project's monitor-stream wire convention."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def copilot_event_stream(
    plan: Callable[[Callable[[Dict[str, Any]], None], threading.Event], Dict[str, Any]],
    *,
    keepalive_seconds: float = 15.0,
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

    def on_step(step: Dict[str, Any]) -> None:
        event_queue.put(("trace", step))

    def worker() -> None:
        try:
            result = plan(on_step, abort)
            event_queue.put(("result", result))
        except Exception as exc:  # surface any planner failure as an honest SSE error frame
            LOGGER.exception("Copilot stream planner failed")
            event_queue.put(("error", {"error": str(exc)[:500]}))
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
