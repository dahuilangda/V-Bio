"""result_cleanup 单元测试。
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from backend.monitoring.result_cleanup import (
    _is_uuid_text,
    _looks_like_task_id,
    purge_stale_msa_cache,
    purge_stale_task_results,
)


def _make_task_uuid() -> str:
    return str(uuid.uuid4())


def _set_mtime(path: Path, days_ago: float) -> None:
    ts = time.time() - days_ago * 86400.0
    os.utime(path, (ts, ts))


class ResultCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_stale_zip_deleted_fresh_zip_kept(self) -> None:
        task_id = _make_task_uuid()
        old_zip = self.root / f"{task_id}_results.zip"
        fresh_zip = self.root / f"{_make_task_uuid()}_affinity_results.zip"
        old_zip.write_bytes(b"x" * 1024)
        fresh_zip.write_bytes(b"y" * 1024)
        _set_mtime(old_zip, 100)
        _set_mtime(fresh_zip, 1)

        stats = purge_stale_task_results(retention_days=90, results_base_dir=self.root,
                                         lead_opt_output_dir=self.root / "nonexistent",
                                         active_task_ids=set(), dry_run=False)

        self.assertFalse(old_zip.exists(), "超过保留期的 zip 应被删除")
        self.assertTrue(fresh_zip.exists(), "保留期内的 zip 应保留")
        self.assertEqual(stats.deleted_files, 1)
        self.assertGreaterEqual(stats.freed_bytes, 1024)

    def test_retention_boundary(self) -> None:
        task_id = _make_task_uuid()
        boundary = self.root / f"{task_id}_results.zip"
        just_within = self.root / f"{_make_task_uuid()}_results.zip"
        boundary.write_bytes(b"")
        just_within.write_bytes(b"")
        _set_mtime(boundary, 90 + 0.5)      # 老于保留期
        _set_mtime(just_within, 90 - 0.5)   # 新于保留期

        purge_stale_task_results(retention_days=90, results_base_dir=self.root,
                                 lead_opt_output_dir=self.root / "nonexistent",
                                 active_task_ids=set(), dry_run=False)

        self.assertFalse(boundary.exists())
        self.assertTrue(just_within.exists())

    def test_backend_middle_tree(self) -> None:
        old_tree = self.root / "boltz" / _make_task_uuid()
        fresh_tree = self.root / "protenix" / _make_task_uuid()
        old_file = old_tree / "output.sdf"
        fresh_file = fresh_tree / "output.sdf"
        old_file.parent.mkdir(parents=True)
        fresh_file.parent.mkdir(parents=True)
        old_file.write_text("old")
        fresh_file.write_text("fresh")
        _set_mtime(old_tree, 200)
        _set_mtime(fresh_tree, 5)

        stats = purge_stale_task_results(retention_days=90, results_base_dir=self.root,
                                         lead_opt_output_dir=self.root / "nonexistent",
                                         active_task_ids=set(), dry_run=False)

        self.assertFalse(old_tree.exists(), "超龄中间结果树应整体删除")
        self.assertTrue(fresh_tree.exists())
        self.assertEqual(stats.deleted_dirs, 1)

    def test_exports_and_other_names_skipped(self) -> None:
        exports = self.root / "exports"
        exports.mkdir()
        (exports / "some_export.xlsx").write_bytes(b"")
        _set_mtime(exports, 200)

        scratch = self.root / "scratch_dir"
        scratch.mkdir()
        _set_mtime(scratch, 200)

        loose_file = self.root / "readme.txt"
        loose_file.write_bytes(b"")
        _set_mtime(loose_file, 200)

        non_task_zip = self.root / "results_backup.zip"
        non_task_zip.write_bytes(b"")
        _set_mtime(non_task_zip, 200)

        purge_stale_task_results(retention_days=90, results_base_dir=self.root,
                                 lead_opt_output_dir=self.root / "nonexistent",
                                 active_task_ids=set(), dry_run=False)

        self.assertTrue(exports.exists(), "exports 目录自身应跳过（已有自身的 TTL 机制）")
        self.assertTrue(scratch.exists(), "非 UUID 命名的目录不应被删除")
        self.assertTrue(loose_file.exists())
        self.assertTrue(non_task_zip.exists(), "非 <task_id>_*.zip 的根级文件不应被删除")

    def test_runtime_tmp_subdirs_by_mtime(self) -> None:
        runtime_tmp = self.root / "_runtime_tmp"
        leaked_old = runtime_tmp / f"p2d_task_{_make_task_uuid()}"
        leaked_fresh = runtime_tmp / f"boltz_task_{_make_task_uuid()}_abcdef"
        leaked_old.mkdir(parents=True)
        leaked_fresh.mkdir(parents=True)
        (leaked_old / "a.txt").write_text("a")
        _set_mtime(leaked_old, 120)
        _set_mtime(leaked_fresh, 3)

        stats = purge_stale_task_results(retention_days=90, results_base_dir=self.root,
                                         lead_opt_output_dir=self.root / "nonexistent",
                                         active_task_ids=set(), dry_run=False)

        self.assertFalse(leaked_old.exists())
        self.assertTrue(leaked_fresh.exists())
        self.assertTrue(runtime_tmp.exists(), "_runtime_tmp 容器目录自身应保留")
        self.assertEqual(stats.deleted_dirs, 1)

    def test_lead_opt_output_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as lead_tmp:
            lead_root = Path(lead_tmp) / "lead_opt"
            old_dir = lead_root / _make_task_uuid()
            fresh_dir = lead_root / "keep_me"  # 非 UUID
            old_dir.mkdir(parents=True)
            fresh_dir.mkdir(parents=True)
            _set_mtime(old_dir, 150)
            _set_mtime(fresh_dir, 150)

            stats = purge_stale_task_results(retention_days=90, results_base_dir=self.root,
                                             lead_opt_output_dir=lead_root,
                                             active_task_ids=set(), dry_run=False)

            self.assertFalse(old_dir.exists())
            self.assertTrue(fresh_dir.exists(), "lead_opt 下非 UUID 目录不应删除")
            self.assertEqual(stats.deleted_dirs, 1)

    def test_active_task_protection(self) -> None:
        active_id = _make_task_uuid()
        old_zip = self.root / f"{active_id}_results.zip"
        old_tree = self.root / "boltz" / active_id
        old_zip.write_bytes(b"x" * 100)
        old_tree.mkdir(parents=True)
        _set_mtime(old_zip, 200)
        _set_mtime(old_tree, 200)

        stats = purge_stale_task_results(retention_days=90, results_base_dir=self.root,
                                         lead_opt_output_dir=self.root / "nonexistent",
                                         active_task_ids={active_id}, dry_run=False)

        self.assertTrue(old_zip.exists(), "排队/运行中任务的结果不应被删除")
        self.assertTrue(old_tree.exists())
        self.assertEqual(stats.deleted_files, 0)
        self.assertEqual(stats.deleted_dirs, 0)

    def test_dry_run_counts_without_deleting(self) -> None:
        task_id = _make_task_uuid()
        old_zip = self.root / f"{task_id}_results.zip"
        old_zip.write_bytes(b"z" * 4096)
        _set_mtime(old_zip, 200)

        stats = purge_stale_task_results(retention_days=90, results_base_dir=self.root,
                                         lead_opt_output_dir=self.root / "nonexistent",
                                         active_task_ids=set(), dry_run=True)

        self.assertTrue(old_zip.exists(), "dry-run 不应删除任何文件")
        # dry-run 的 deleted_* 是"将删除"的候选计数，freed_bytes 是预计释放量
        self.assertEqual(stats.deleted_files, 1)
        self.assertGreaterEqual(stats.freed_bytes, 4096)

    def test_missing_roots_no_error(self) -> None:
        stats = purge_stale_task_results(retention_days=90,
                                         results_base_dir=self.root / "missing_base",
                                         lead_opt_output_dir=self.root / "missing_lead",
                                         active_task_ids=set(), dry_run=False)
        self.assertEqual(stats.deleted_files, 0)
        self.assertEqual(stats.deleted_dirs, 0)

    def test_active_task_protection_leaks_tmp(self) -> None:
        # _runtime_tmp 下属于活动任务的泄漏目录也应受保护
        active_id = _make_task_uuid()
        leaked = self.root / "_runtime_tmp" / f"p2d_task_{active_id}"
        leaked.mkdir(parents=True)
        _set_mtime(leaked, 200)

        purge_stale_task_results(retention_days=90, results_base_dir=self.root,
                                 lead_opt_output_dir=self.root / "nonexistent",
                                 active_task_ids={active_id}, dry_run=False)

        self.assertTrue(leaked.exists())


class UuidHelpersTest(unittest.TestCase):
    def test_is_uuid_text(self) -> None:
        self.assertTrue(_is_uuid_text(str(uuid.uuid4())))
        self.assertFalse(_is_uuid_text("not-a-uuid"))
        self.assertFalse(_is_uuid_text(""))
        self.assertFalse(_is_uuid_text(f"{uuid.uuid4()}extra"))

    def test_looks_like_task_id(self) -> None:
        mid = str(uuid.uuid4())
        self.assertEqual(_looks_like_task_id(f"p2d_task_{mid}"), mid)
        self.assertEqual(_looks_like_task_id(f"boltz_task_{mid}_abcdef"), mid)
        self.assertIsNone(_looks_like_task_id("random-directory"))
        self.assertIsNone(_looks_like_task_id(""))


class MsaCacheCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_file(self, name: str, days_ago: float, size: int = 2048) -> Path:
        path = self.cache / name
        path.write_bytes(b"m" * size)
        _set_mtime(path, days_ago)
        return path

    def test_stale_files_deleted_fresh_kept(self) -> None:
        stale = self._make_file("msa_00000000000000000000000000000001.a3m", 120)
        fresh = self._make_file("msa_00000000000000000000000000000002.a3m", 10)

        stats = purge_stale_msa_cache(retention_days=90, cache_dir=self.cache, dry_run=False)

        self.assertFalse(stale.exists(), "超过保留期的 MSA 缓存文件应删除")
        self.assertTrue(fresh.exists())
        self.assertEqual(stats.deleted_files, 1)
        self.assertEqual(stats.scanned_files, 1)

    def test_dry_run_leaves_files_and_counts(self) -> None:
        stale = self._make_file("msa_00000000000000000000000000000003.a3m", 200)

        stats = purge_stale_msa_cache(retention_days=90, cache_dir=self.cache, dry_run=True)

        self.assertTrue(stale.exists(), "dry-run 不应删除 MSA 缓存文件")
        self.assertEqual(stats.deleted_files, 1)
        self.assertEqual(stats.freed_bytes, 2048)

    def test_missing_cache_dir_no_error_and_subdirs_skipped(self) -> None:
        stats = purge_stale_msa_cache(retention_days=90,
                                      cache_dir=self.cache / "missing", dry_run=False)
        self.assertEqual(stats.deleted_files, 0)

        subdir = self.cache / "subdir"
        subdir.mkdir()
        _set_mtime(subdir, 300)
        stats = purge_stale_msa_cache(retention_days=90, cache_dir=self.cache, dry_run=False)
        self.assertTrue(subdir.exists(), "MSA 缓存中的子目录不应删除（仅平铺文件）")
        self.assertEqual(stats.deleted_files, 0)


if __name__ == "__main__":
    unittest.main()