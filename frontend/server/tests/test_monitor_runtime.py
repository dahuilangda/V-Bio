from __future__ import annotations

import json
import queue
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

SERVER_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(__file__).resolve().parents[3]
for path in (str(SERVER_ROOT), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from backend.monitoring.monitor_collector import MonitorCollector, normalize_celery_event
from management_api.monitor_stream import MonitorSubscription, sse_frames


class _FakeRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def xack(self, *args: object) -> None:
        self.calls.append(("xack", args))


class MonitorCollectorTests(unittest.TestCase):
    def test_normalizes_generic_celery_lifecycle_events(self) -> None:
        event = normalize_celery_event({
            "type": "task-started",
            "uuid": "task-1",
            "hostname": "gpu@node",
            "name": "tasks.execute",
            "routing_key": "cap.alpha.default",
            "timestamp": 1_700_000_000,
            "clock": 9,
        })
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["kind"], "task")
        self.assertEqual(event["task_id"], "task-1")
        self.assertEqual(event["worker_id"], "gpu@node")
        self.assertEqual(event["capability"], "alpha")
        self.assertEqual(event["event_key"], "celery:task-started:task-1:9")

    def test_ack_is_after_postgresql_projection_commit(self) -> None:
        order: list[str] = []
        store = Mock()
        store.apply_event.side_effect = lambda _event: order.append("commit")
        redis_client = _FakeRedis()
        collector = MonitorCollector(store, redis_client)
        collector._apply_stream_message(
            "4-0",
            {"payload": json.dumps({"kind": "task_status", "task_id": "task-1", "status": "RUNNING"})},
        )
        order.append(redis_client.calls[0][0])
        self.assertEqual(order, ["commit", "xack"])

    def test_failed_projection_does_not_ack_stream_entry(self) -> None:
        store = Mock()
        store.apply_event.side_effect = RuntimeError("database unavailable")
        redis_client = _FakeRedis()
        collector = MonitorCollector(store, redis_client)
        with self.assertRaises(RuntimeError):
            collector._apply_stream_message(
                "5-0",
                {"payload": json.dumps({"kind": "task_status", "task_id": "task-1", "status": "RUNNING"})},
            )
        self.assertEqual(redis_client.calls, [])


class MonitorStreamTests(unittest.TestCase):
    def test_sse_frame_contains_cursor_and_compact_snapshot(self) -> None:
        broker = Mock()
        notifications: queue.Queue[int] = queue.Queue(maxsize=1)
        notifications.put(17)
        subscription = MonitorSubscription("sub", notifications, broker)
        frames = sse_frames(
            subscription,
            lambda: {"sequence": 17, "cluster": {"workers": {}}, "tasks": {}},
            keepalive_seconds=0.01,
            coalesce_seconds=0,
        )
        frame = next(frames)
        self.assertIn("id: 17", frame)
        self.assertIn("event: overview", frame)
        self.assertIn('"sequence":17', frame)
        frames.close()
        broker.unsubscribe.assert_called_once_with("sub")


if __name__ == "__main__":
    unittest.main()
