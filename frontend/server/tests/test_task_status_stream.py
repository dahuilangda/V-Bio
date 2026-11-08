"""任务状态 SSE hub 与网关流的契约测试（进程内扇出、跨 worker 通知、鉴权、订阅生命周期）。"""
from __future__ import annotations

import json
import queue
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from management_api.task_status_stream import TaskStatusHub, task_status_sse_frames


class FakeNotifyConnection:
    """记录 pg_notify 调用；SET_SESSION 由 hub 调用，需可响应。"""

    def __init__(self):
        self.notifies: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def set_session(self, **_kwargs):
        pass

    def cursor(self):
        connection = self

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, params=None):
                if "pg_notify" in sql and params:
                    with connection._lock:
                        connection.notifies.append(tuple(params))

        return Cursor()


class FakeListenConnection:
    """LISTEN 连接桩：真实 socketpair 驱动 select；poll 弹出预置 notify。"""

    def __init__(self, pending: "queue.Queue[tuple[str, str]]"):
        import socket

        self.pending = pending
        self.notifies: list = []
        self._sink, self._source = socket.socketpair()

    def fileno(self) -> int:
        return self._source.fileno()

    def cursor(self):
        connection = self

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql):
                assert "listen" in sql

        return Cursor()

    def poll(self):
        try:
            self._source.recv(1)
        except Exception:
            pass
        try:
            channel, payload = self.pending.get_nowait()
            self.notifies.append(type("N", (), {"channel": channel, "payload": payload})())
        except queue.Empty:
            pass

    def close(self):
        try:
            self._source.close()
            self._sink.close()
        except Exception:
            pass


class TaskStatusHubTests(unittest.TestCase):
    def test_publish_fans_out_to_matching_project_in_process(self):
        hub = TaskStatusHub("", notify_connection_factory=lambda _url: FakeNotifyConnection())
        sub_a = hub.subscribe("p-1")
        sub_b = hub.subscribe("p-2")
        hub.publish("p-1", [{"task_id": "t-1", "state": "SUCCESS"}])
        event = sub_a.wait(timeout=1)
        self.assertIsNotNone(event)
        self.assertEqual(event["entries"][0]["task_id"], "t-1")
        self.assertIsNone(sub_b.wait(timeout=0.05))
        sub_a.close()
        sub_b.close()

    def test_unsubscribed_subscriber_stops_receiving(self):
        hub = TaskStatusHub("", notify_connection_factory=lambda _url: FakeNotifyConnection())
        sub = hub.subscribe("p-1")
        sub.close()
        hub.publish("p-1", [{"task_id": "t-1", "state": "FAILURE"}])
        self.assertIsNone(sub.wait(timeout=0.05))

    def test_pg_notify_carries_payload_for_other_worker(self):
        connection = FakeNotifyConnection()
        hub = TaskStatusHub(
            "postgresql://x",
            notify_connection_factory=lambda _url: connection,
        )
        hub.publish("p-1", [{"task_id": "t-1", "state": "SUCCESS"}])
        self.assertEqual(len(connection.notifies), 1)
        channel, payload = connection.notifies[0]
        self.assertEqual(channel, "vbio_task_events")
        self.assertIn("t-1", payload)

    def test_listen_loop_fans_out_remote_notification(self):
        pending: "queue.Queue[tuple[str, str]]" = queue.Queue()
        connection = FakeListenConnection(pending)
        hub = TaskStatusHub("postgresql://x", connection_factory=lambda _url: connection)
        sub = hub.subscribe("p-1")
        hub.start()
        try:
            pending.put(("vbio_task_events", json.dumps({"project_id": "p-1", "entries": [{"task_id": "t-9", "state": "SUCCESS"}]})))
            connection._sink.send(b"x")  # 唤醒 select
            event = sub.wait(timeout=2)
            self.assertIsNotNone(event)
            self.assertEqual(event[0]["task_id"], "t-9")
        finally:
            hub.stop()
            sub.close()

    def test_full_queue_drops_newest_frame_and_counts(self):
        hub = TaskStatusHub("", notify_connection_factory=lambda _url: FakeNotifyConnection())
        sub = hub.subscribe("p-1")
        # 队列上限 8：先灌 8 条，第 9 条应被丢弃并计数
        for i in range(9):
            hub.publish("p-1", [{"task_id": f"t-{i}", "state": "SUCCESS"}])
        self.assertGreater(hub._dropped, 0)
        first = sub.wait(timeout=0.1)
        self.assertIsNotNone(first)
        sub.close()


class SseFrameTests(unittest.TestCase):
    def test_frames_include_changed_event_and_heartbeat(self):
        pending: "queue.Queue[tuple[str, str]]" = queue.Queue()
        hub = TaskStatusHub(
            "",
            connection_factory=lambda _url: FakeListenConnection(pending),
            notify_connection_factory=lambda _url: FakeNotifyConnection(),
        )
        sub = hub.subscribe("p-1")
        hub.publish("p-1", [{"task_id": "t-1", "state": "SUCCESS"}])
        frames = list(task_status_sse_frames(sub, keepalive_seconds=0.05, max_lifetime_seconds=0.3))
        hub.unsubscribe("p-1")
        joined = "".join(frames)
        self.assertIn("event: changed", joined)
        self.assertIn('"state":"SUCCESS"', joined)

    def test_idle_subscription_yields_keepalives_then_reconnect(self):
        pending: "queue.Queue[tuple[str, str]]" = queue.Queue()
        hub = TaskStatusHub(
            "",
            connection_factory=lambda _url: FakeListenConnection(pending),
            notify_connection_factory=lambda _url: FakeNotifyConnection(),
        )
        sub = hub.subscribe("p-empty")
        frames = list(task_status_sse_frames(sub, keepalive_seconds=0.02, max_lifetime_seconds=0.15))
        hub.unsubscribe("p-empty")
        joined = "".join(frames)
        self.assertIn(": keepalive", joined)
        self.assertIn("event: reconnect", joined)


if __name__ == "__main__":
    unittest.main()
