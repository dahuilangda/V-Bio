"""版本存根与批量状态端点：增量轮询两阶段的契约测试。

ver 稳定性契约是核心：状态未变 → ver 不变（前端跳过完整拉取）；
状态/归档变化 → ver 变化（前端仅对变化 id 拉全量）。
"""
from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock

from flask import Flask

from backend.routes.task import register_task_routes


class StubRouteTestBase(unittest.TestCase):
    def _register(self, *, archive_path: str | None, tracker_status=None, heartbeat=None, celery_state: str = 'PENDING') -> Flask:
        # AsyncResult.state 触发 celery backend 读取；桩成固定 state 让单测不依赖 Redis
        fake_result = mock.Mock()
        fake_result.state = celery_state
        async_result_patch = mock.patch('backend.routes.task.AsyncResult', return_value=fake_result)
        async_result_patch.start()
        self.addCleanup(async_result_patch.stop)

        app = Flask(__name__)
        app.config['UPLOAD_FOLDER'] = tempfile.gettempdir()
        # 默认关掉快照缓存，让每个用例聚焦自己的契约；TTL 契约单独验证
        app.config['STATUS_STUB_TTL_SECONDS'] = 0
        register_task_routes(
            app,
            require_api_token=lambda handler: handler,
            celery_app=mock.Mock(),
            task_monitor=mock.Mock(),
            predict_task=mock.Mock(),
            config_module=SimpleNamespace(UPLOAD_FOLDER=tempfile.gettempdir()),
            logger=mock.Mock(),
            find_result_archive=lambda _task_id: archive_path,
            resolve_result_archive_path=lambda _task_id: ("results.zip", archive_path or tempfile.gettempdir()),
            build_or_get_view_archive=lambda *_a, **_k: archive_path or tempfile.gettempdir(),
            get_tracker_status=lambda _task_id: (tracker_status, heartbeat),
            get_compact_prediction_metrics=lambda _task_id: {},
            list_known_queues=lambda: [],
            get_worker_capability_snapshot=lambda: {},
        )
        return app

    def _stubs(self, client, task_ids):
        response = client.post('/status/stubs', json={'task_ids': task_ids})
        self.assertEqual(response.status_code, 200)
        return response.get_json()['stubs']


class StatusStubTests(StubRouteTestBase):
    def test_queued_task_ver_is_stable_when_nothing_changes(self):
        app = self._register(archive_path=None, tracker_status=None, heartbeat=None)
        client = app.test_client()
        first = self._stubs(client, ['t-1'])
        second = self._stubs(client, ['t-1'])
        self.assertEqual(first, second)
        self.assertEqual(first[0]['ver'], second[0]['ver'])

    def test_ver_changes_when_tracker_status_changes(self):
        app = self._register(archive_path=None, tracker_status=None, heartbeat=None)
        client = app.test_client()
        before = self._stubs(client, ['t-1'])[0]['ver']

        # 模拟 tracker 进度推进：换一个全新 app（同一闭包形状，不同 tracker 返回）
        app2 = self._register(
            archive_path=None,
            tracker_status={'status': 'RUNNING', 'details': '60%'},
            heartbeat='hb-1',
        )
        after = app2.test_client().post('/status/stubs', json={'task_ids': ['t-1']}).get_json()['stubs'][0]['ver']
        self.assertNotEqual(before, after)

    def test_archived_task_ver_tracks_archive_mtime(self):
        fd, path = tempfile.mkstemp(suffix='.zip')
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('manifest.json', '{}')
        app = self._register(archive_path=path)
        client = app.test_client()
        before = self._stubs(client, ['t-1'])[0]['ver']

        os.utime(path, (1_700_000_000, 1_700_000_000))
        after = self._stubs(client, ['t-1'])[0]['ver']
        self.assertNotEqual(before, after)

    def test_batch_request_is_capped_and_deduped(self):
        app = self._register(archive_path=None, tracker_status=None, heartbeat=None)
        client = app.test_client()
        ids = ['dup'] * 3 + [f't-{i}' for i in range(2100)]
        stubs = self._stubs(client, ids)
        returned_ids = [s['task_id'] for s in stubs]
        self.assertEqual(len(returned_ids), len(set(returned_ids)))
        self.assertLessEqual(len(returned_ids), 2000)


class StubSnapshotCacheTests(StubRouteTestBase):
    def test_ttl_cache_hides_mtime_change_within_window(self):
        fd, path = tempfile.mkstemp(suffix='.zip')
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('manifest.json', '{}')
        app = self._register(archive_path=path)
        app.config['STATUS_STUB_TTL_SECONDS'] = 3600
        client = app.test_client()
        before = self._stubs(client, ['t-1'])[0]['ver']
        os.utime(path, (1_700_000_000, 1_700_000_000))
        after = self._stubs(client, ['t-1'])[0]['ver']
        self.assertEqual(before, after)


class BatchEtagTests(StubRouteTestBase):
    def test_batch_supports_if_none_match_304(self):
        app = self._register(archive_path=None, tracker_status=None, heartbeat=None)
        client = app.test_client()
        first = client.post('/status/batch', json={'task_ids': ['t-1', 't-2']})
        etag = first.headers['ETag']
        self.assertEqual(first.status_code, 200)

        second = client.post(
            '/status/batch',
            json={'task_ids': ['t-1', 't-2']},
            headers={'If-None-Match': etag},
        )
        self.assertEqual(second.status_code, 304)
        self.assertEqual(second.data, b'')

    def test_batch_returns_200_when_status_changes(self):
        app = self._register(archive_path=None, tracker_status=None, heartbeat=None)
        client = app.test_client()
        first = client.post('/status/batch', json={'task_ids': ['t-1']})
        etag = first.headers['ETag']

        app2 = self._register(
            archive_path=None,
            tracker_status={'status': 'RUNNING', 'details': '60%'},
            heartbeat='hb-1',
        )
        second = app2.test_client().post(
            '/status/batch',
            json={'task_ids': ['t-1']},
            headers={'If-None-Match': etag},
        )
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(second.headers.get('ETag'), etag)


if __name__ == '__main__':
    unittest.main()
