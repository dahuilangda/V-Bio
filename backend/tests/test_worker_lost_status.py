from __future__ import annotations

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from flask import Flask

from backend.routes.task import register_task_routes
from backend.services import result_archive
from backend.services.result_archive import (
    WORKER_LOST_GRACE_SECONDS,
    WORKER_LOST_MESSAGE,
    ResultArchiveService,
)


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def exists(self, key: str) -> bool:
        return key in self.store

    def get(self, key: str):
        return self.store.get(key)

    def set(self, key: str, value, ex=None) -> None:
        self.store[key] = value if isinstance(value, bytes) else str(value).encode()

    def delete(self, key: str) -> None:
        self.store.pop(key, None)


class WorkerLostServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.redis = FakeRedis()
        self.backend = mock.Mock()
        self.backend.mark_as_failure = mock.Mock()
        self.logger = SimpleNamespace(warning=mock.Mock())
        self.service = ResultArchiveService(
            app=SimpleNamespace(config={}),
            celery_app=SimpleNamespace(backend=self.backend),
            logger=self.logger,
            get_redis_client_fn=lambda: self.redis,
        )
        self.clock = 1_800_000_000.0
        patcher = mock.patch.object(result_archive.time, "time", side_effect=lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_task_with_heartbeat_is_left_alone(self) -> None:
        self.redis.set("task_heartbeat:t-1", "now")
        self.redis.set("task_no_heartbeat_since:t-1", json.dumps(self.clock - 10_000))
        self.assertIsNone(self.service.fail_task_if_worker_lost("t-1", "PROGRESS"))
        self.backend.mark_as_failure.assert_not_called()
        self.assertNotIn("task_no_heartbeat_since:t-1", self.redis.store)

    def test_states_that_never_started_are_not_judged(self) -> None:
        for state in ("PENDING", "RECEIVED", "RETRY", "REVOKED", "SUCCESS", "FAILURE"):
            self.assertIsNone(self.service.fail_task_if_worker_lost("t-1", state))
        self.assertEqual(self.redis.store, {})

    def test_first_sighting_only_records_the_time(self) -> None:
        self.assertIsNone(self.service.fail_task_if_worker_lost("t-1", "STARTED"))
        self.backend.mark_as_failure.assert_not_called()
        self.clock += WORKER_LOST_GRACE_SECONDS - 1
        self.assertIsNone(self.service.fail_task_if_worker_lost("t-1", "PROGRESS"))
        self.backend.mark_as_failure.assert_not_called()

    def test_task_still_silent_after_grace_is_failed(self) -> None:
        self.service.fail_task_if_worker_lost("t-1", "STARTED")
        self.clock += WORKER_LOST_GRACE_SECONDS
        self.assertEqual(self.service.fail_task_if_worker_lost("t-1", "PROGRESS"), WORKER_LOST_MESSAGE)
        self.backend.mark_as_failure.assert_called_once()
        args, kwargs = self.backend.mark_as_failure.call_args
        self.assertEqual(args[0], "t-1")
        self.assertIsInstance(args[1], RuntimeError)
        self.assertFalse(kwargs["call_errbacks"])
        self.assertNotIn("task_no_heartbeat_since:t-1", self.redis.store)

    def test_redis_errors_do_not_reach_the_caller(self) -> None:
        self.redis.exists = mock.Mock(side_effect=ConnectionError("redis down"))
        self.assertIsNone(self.service.fail_task_if_worker_lost("t-1", "PROGRESS"))
        self.logger.warning.assert_called_once()


class WorkerLostRouteTests(unittest.TestCase):
    def _client(self, *, celery_state: str, worker_lost_message: str | None):
        fake_result = mock.Mock()
        fake_result.state = celery_state
        fake_result.info = {"status": "Running prediction on GPU 3"}
        async_result_patch = mock.patch("backend.routes.task.AsyncResult", return_value=fake_result)
        async_result_patch.start()
        self.addCleanup(async_result_patch.stop)
        app = Flask(__name__)
        app.config["UPLOAD_FOLDER"] = tempfile.gettempdir()
        register_task_routes(
            app,
            require_api_token=lambda handler: handler,
            celery_app=mock.Mock(),
            task_monitor=mock.Mock(),
            predict_task=mock.Mock(),
            config_module=SimpleNamespace(UPLOAD_FOLDER=tempfile.gettempdir()),
            logger=mock.Mock(),
            find_result_archive=lambda _task_id: None,
            resolve_result_archive_path=lambda _task_id: ("results.zip", tempfile.gettempdir()),
            build_or_get_view_archive=lambda *_a, **_k: tempfile.gettempdir(),
            get_tracker_status=lambda _task_id: (None, None),
            fail_task_if_worker_lost=lambda _task_id, _state: worker_lost_message,
            get_compact_prediction_metrics=lambda _task_id: {},
            list_known_queues=lambda: [],
            get_worker_capability_snapshot=lambda: {},
        )
        return app.test_client()

    def test_live_task_keeps_its_running_state(self) -> None:
        response = self._client(celery_state="PROGRESS", worker_lost_message=None).post(
            "/status/batch", json={"task_ids": ["t-1"]}
        )
        status = response.get_json()["statuses"][0]
        self.assertEqual(status["state"], "PROGRESS")
        self.assertEqual(status["info"]["status"], "Running prediction on GPU 3")

    def test_lost_task_is_reported_and_synced_as_failed(self) -> None:
        sync_patch = mock.patch("backend.routes.task._sync_project_task_worker_lost")
        sync = sync_patch.start()
        self.addCleanup(sync_patch.stop)
        response = self._client(celery_state="PROGRESS", worker_lost_message=WORKER_LOST_MESSAGE).post(
            "/status/batch", json={"task_ids": ["t-1"]}
        )
        status = response.get_json()["statuses"][0]
        self.assertEqual(status["state"], "FAILURE")
        self.assertEqual(status["info"]["message"], WORKER_LOST_MESSAGE)
        sync.assert_called_once()
        self.assertEqual(sync.call_args.args[0], "t-1")


if __name__ == "__main__":
    unittest.main()
