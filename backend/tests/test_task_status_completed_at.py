from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from celery import Celery
from celery.backends.base import KeyValueStoreBackend
from flask import Flask

from backend.routes.task import register_task_routes

DONE_AT = '2026-11-24T23:15:31.658158+00:00'
TRACKER_TIME = '2026-11-24T23:15:26.126321'
BACKEND = f'{__name__}:MemoryBackend'


class MemoryBackend(KeyValueStoreBackend):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entries = {}

    def get(self, key):
        return self.entries.get(key)

    def set(self, key, value):
        self.entries[key] = value

    def delete(self, key):
        self.entries.pop(key, None)

    def mget(self, keys):
        return [self.get(key) for key in keys]


class CompletedAtSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.celery = Celery('completed-at', backend=BACKEND, set_as_current=False)
        self.enterContext(mock.patch.object(self.celery, 'now', return_value=datetime.fromisoformat(DONE_AT)))
        results_dir = self.enterContext(tempfile.TemporaryDirectory())
        self.upload_dir = results_dir
        self.archive_name = None
        self.tracker_status = None
        task_monitor = mock.Mock()
        task_monitor._find_task_processes.return_value = []
        task_monitor._discover_task_containers.return_value = {'containers': []}
        app = Flask(__name__)
        app.config['UPLOAD_FOLDER'] = self.upload_dir
        register_task_routes(
            app,
            require_api_token=lambda handler: handler,
            celery_app=self.celery,
            task_monitor=task_monitor,
            predict_task=mock.Mock(),
            config_module=SimpleNamespace(UPLOAD_FOLDER=self.upload_dir),
            logger=mock.Mock(),
            find_result_archive=lambda _task_id: self.archive_name,
            resolve_result_archive_path=lambda _task_id: ("results.zip", self.upload_dir),
            build_or_get_view_archive=lambda *_a, **_k: self.upload_dir,
            get_tracker_status=lambda _task_id: (self.tracker_status, None),
            fail_task_if_worker_lost=lambda _task_id, _state: None,
            get_compact_prediction_metrics=lambda _task_id: {},
            list_known_queues=lambda: [],
            get_worker_capability_snapshot=lambda: {},
        )
        self.client = app.test_client()

    def _status(self) -> dict:
        response = self.client.post('/status/batch', json={'task_ids': ['t-1']})
        self.assertEqual(response.status_code, 200)
        return response.get_json()['statuses'][0]

    def _finish(self, state: str) -> None:
        self.celery.backend.store_result('t-1', RuntimeError('Engine failed'), state=state)

    def test_failure_time_is_the_one_celery_recorded(self):
        self._finish('FAILURE')
        self.tracker_status = {'status': 'failed', 'timestamp': TRACKER_TIME}
        status = self._status()
        self.assertEqual(status['state'], 'FAILURE')
        self.assertEqual(status['info']['completed_at'], DONE_AT)

    def test_revoked_time_is_the_one_celery_recorded(self):
        self._finish('REVOKED')
        status = self._status()
        self.assertEqual(status['state'], 'REVOKED')
        self.assertEqual(status['info']['completed_at'], DONE_AT)

    def test_tracker_inferred_failure_uses_tracker_time(self):
        self.tracker_status = {'status': 'failed', 'timestamp': TRACKER_TIME}
        status = self._status()
        self.assertEqual(status['state'], 'FAILURE')
        self.assertEqual(status['info']['completed_at'], f'{TRACKER_TIME}+00:00')

    def test_archived_success_uses_archive_mtime(self):
        self.archive_name = 't-1_results.zip'
        with zipfile.ZipFile(os.path.join(self.upload_dir, self.archive_name), 'w') as archive:
            archive.writestr('manifest.json', '{}')
        os.utime(os.path.join(self.upload_dir, self.archive_name), (1_700_000_000, 1_700_000_000))
        status = self._status()
        self.assertEqual(status['state'], 'SUCCESS')
        self.assertEqual(
            status['info']['completed_at'],
            datetime.fromtimestamp(1_700_000_000, tz=timezone.utc).isoformat(),
        )

    def test_failure_without_any_record_carries_no_time(self):
        status = self._status()
        self.assertEqual(status['state'], 'PENDING')
        self.assertNotIn('completed_at', status['info'])


if __name__ == '__main__':
    unittest.main()
