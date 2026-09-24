"""Digest-batching behavior for task notification emails (mailer).

The Redis interaction is exercised through a minimal in-memory fake so the
window/queue semantics (SET NX EX claim, RPUSH/LRANGE/DEL drain) are tested
without a live broker; SMTP sending is mocked.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.services import mailer  # noqa: E402


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}

    def pipeline(self):
        return self

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    def execute(self):
        return [True]

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def lrange(self, key, start, end):
        items = self.lists.get(key, [])
        if end == -1:
            return list(items[start:])
        return list(items[start:end + 1])

    def delete(self, key):
        return self.lists.pop(key, None) is not None


class DigestWindowTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRedis()
        self.sent: list[tuple[str, str, str]] = []
        send_patch = mock.patch.object(
            mailer, "_send_plain", side_effect=self._capture_send)
        send_patch.start()
        self.addCleanup(send_patch.stop)

    def _capture_send(self, to, subject, body):
        self.sent.append((to, subject, body))
        return True

    def _enqueue(self, n):
        sent_tasks = []
        with mock.patch.object(mailer, "digest_window_seconds", return_value=900), \
             mock.patch("backend.core.celery_app.celery_app") as celery_app, \
             mock.patch("gpu_manager.get_redis_client", return_value=self.fake):
            celery_app.send_task.side_effect = lambda name, args, countdown: sent_tasks.append((name, args, countdown))
            for i in range(n):
                mailer._enqueue_digest_event(
                    to_email="user@example.com",
                    task_id=f"task-{i}", state="SUCCESS",
                    task_kind="dock", project_id="p1")
        return sent_tasks

    def test_first_event_claims_window_and_schedules_flush(self):
        scheduled = self._enqueue(1)
        self.assertEqual(len(scheduled), 1)
        name, args, countdown = scheduled[0]
        self.assertEqual(name, "backend.worker.tasks.flush_notification_digest")
        self.assertEqual(args, ["user@example.com"])
        self.assertEqual(countdown, 900)
        self.assertEqual(len(self.fake.lists["vbio:notify:pending:user@example.com"]), 1)

    def test_events_within_window_do_not_reschedule(self):
        scheduled = self._enqueue(5)
        # exactly ONE flush scheduled for the whole window; 5 queued events
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(len(self.fake.lists["vbio:notify:pending:user@example.com"]), 5)

    def test_flush_drains_and_batches_into_one_email(self):
        self._enqueue(3)
        with mock.patch("gpu_manager.get_redis_client", return_value=self.fake), \
             mock.patch.object(mailer, "_smtp_configured", return_value=True):
            ok = mailer.flush_notification_digest("user@example.com")
        self.assertTrue(ok)
        self.assertEqual(len(self.sent), 1)
        to, subject, body = self.sent[0]
        self.assertIn("3 tasks", subject)
        self.assertIn("task-0", body)
        self.assertIn("task-2", body)
        self.assertIn("15-minute window", body)
        # queue drained
        self.assertNotIn("vbio:notify:pending:user@example.com", self.fake.lists)

    def test_flush_empty_queue_sends_nothing(self):
        with mock.patch("gpu_manager.get_redis_client", return_value=self.fake):
            ok = mailer.flush_notification_digest("user@example.com")
        self.assertTrue(ok)
        self.assertEqual(self.sent, [])

    def test_digest_overflow_line(self):
        self._enqueue(5)
        with mock.patch.object(mailer, "_DIGEST_MAX_ROWS", 3), \
             mock.patch("gpu_manager.get_redis_client", return_value=self.fake), \
             mock.patch.object(mailer, "_smtp_configured", return_value=True):
            mailer.flush_notification_digest("user@example.com")
        _, _, body = self.sent[0]
        self.assertIn("and 2 more", body)

    def test_send_task_notification_uses_digest_when_enabled(self):
        with mock.patch.object(mailer, "digest_window_seconds", return_value=900), \
             mock.patch.object(mailer, "_enqueue_digest_event") as enqueue:
            mailer.send_task_notification(
                to_email="u@x.com", task_id="t", state="SUCCESS", task_kind="dock")
        enqueue.assert_called_once()

    def test_send_task_notification_immediate_when_disabled(self):
        with mock.patch.object(mailer, "digest_window_seconds", return_value=0), \
             mock.patch.object(mailer, "_send_task_email_direct") as direct:
            mailer.send_task_notification(
                to_email="u@x.com", task_id="t", state="SUCCESS", task_kind="dock")
        direct.assert_called_once()


if __name__ == "__main__":
    unittest.main()
