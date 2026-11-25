from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from flask import Flask

from backend.routes.task import register_task_routes

DONE_AT = datetime(2026, 11, 24, 23, 15, 31, 658158, tzinfo=timezone.utc)
TRACKER_TIME = '2026-11-24T23:15:26.126321'


class CompletedAtSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.upload_dir = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, self.upload_dir)

    def _status(
        self,
        *,
        celery_state: str,
        meta: dict,
        info: dict | None = None,
        archive_path: str | None = None,
        tracker_status: dict | None = None,
    ) -> dict:
        celery_app = mock.Mock()
        celery_app.backend.get_task_meta.return_value = meta
        task_monitor = mock.Mock()
        task_monitor._find_task_processes.return_value = []
        task_monitor._discover_task_containers.return_value = {'containers': []}
        fake_result = mock.Mock()
        fake_result.state = celery_state
        fake_result.info = info if info is not None else {}
        async_result_patch = mock.patch('backend.routes.task.AsyncResult', return_value=fake_result)
        async_result_patch.start()
        self.addCleanup(async_result_patch.stop)

        upload_dir = self.upload_dir
        app = Flask(__name__)
        app.config['UPLOAD_FOLDER'] = upload_dir
        register_task_routes(
            app,
            require_api_token=lambda handler: handler,
            celery_app=celery_app,
            task_monitor=task_monitor,
            predict_task=mock.Mock(),
            config_module=SimpleNamespace(UPLOAD_FOLDER=upload_dir),
            logger=mock.Mock(),
            find_result_archive=lambda _task_id: archive_path,
            resolve_result_archive_path=lambda _task_id: ("results.zip", upload_dir),
            build_or_get_view_archive=lambda *_a, **_k: upload_dir,
            get_tracker_status=lambda _task_id: (tracker_status, None),
            fail_task_if_worker_lost=lambda _task_id, _state: None,
            get_compact_prediction_metrics=lambda _task_id: {},
            list_known_queues=lambda: [],
            get_worker_capability_snapshot=lambda: {},
        )
        response = app.test_client().post('/status/batch', json={'task_ids': ['t-1']})
        self.assertEqual(response.status_code, 200)
        return response.get_json()['statuses'][0]

    def test_failure_time_is_the_one_celery_recorded(self):
        status = self._status(
            celery_state='FAILURE',
            meta={'status': 'FAILURE', 'date_done': DONE_AT},
            tracker_status={'status': 'failed', 'timestamp': TRACKER_TIME},
        )
        self.assertEqual(status['state'], 'FAILURE')
        self.assertEqual(status['info']['completed_at'], DONE_AT.isoformat())

    def test_revoked_time_is_the_one_celery_recorded(self):
        status = self._status(celery_state='REVOKED', meta={'status': 'REVOKED', 'date_done': DONE_AT})
        self.assertEqual(status['state'], 'REVOKED')
        self.assertEqual(status['info']['completed_at'], DONE_AT.isoformat())

    def test_tracker_inferred_failure_uses_tracker_time(self):
        status = self._status(
            celery_state='PENDING',
            meta={'status': 'PENDING'},
            tracker_status={'status': 'failed', 'timestamp': TRACKER_TIME},
        )
        self.assertEqual(status['state'], 'FAILURE')
        self.assertEqual(status['info']['completed_at'], f'{TRACKER_TIME}+00:00')

    def test_archived_success_uses_archive_mtime(self):
        path = os.path.join(self.upload_dir, 't-1_results.zip')
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('manifest.json', '{}')
        os.utime(path, (1_700_000_000, 1_700_000_000))
        self.addCleanup(os.remove, path)
        status = self._status(
            celery_state='PENDING', meta={'status': 'PENDING'}, archive_path='t-1_results.zip'
        )
        self.assertEqual(status['state'], 'SUCCESS')
        self.assertEqual(
            status['info']['completed_at'],
            datetime.fromtimestamp(1_700_000_000, tz=timezone.utc).isoformat(),
        )

    def test_failure_without_any_record_carries_no_time(self):
        status = self._status(celery_state='PROGRESS', meta={'status': 'PROGRESS'}, info={'status': 'Engine failed'})
        self.assertEqual(status['state'], 'FAILURE')
        self.assertNotIn('completed_at', status['info'])


if __name__ == '__main__':
    unittest.main()
