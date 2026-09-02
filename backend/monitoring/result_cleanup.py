"""任务结果过期清理器。

central 服务的任务结果会持续累积且没有按期清理机制：结果 zip 平铺在
RESULTS_BASE_DIR 根目录、每个任务还有 <RESULTS_BASE_DIR>/<backend>/<task_id>/ 的
中间结果树，lead_optimization 输出与泄漏的运行时临时目录同样只增不减。

本模块按统一规则清理这些任务产物：

- 仅识别 UUID 命名路径（<task_id>_*.zip、<backend>/<task_id>/、
  <LEAD_OPTIMIZATION_OUTPUT_DIR>/<task_id>/），避免误删无关文件；
- 到期判据为文件/目录 mtime 距今超过保留期（默认 90 天）；
- 运行/排队中的任务（monitor_tasks_current.state_bucket in ('queued','running')）
  一律跳过，由 monitor 服务传入活动 task_id 集合；
- 非任务产物目录（exports/、_runtime_tmp/ 自身）保留；_runtime_tmp 下的子目录
  按同样规则清理（泄漏的临时任务目录）。

由 monitor 服务按 RESULTS_CLEANUP_INTERVAL_SECONDS 定期执行，也可作为 CLI
手动触发（--dry-run 预览 / --run 删除）。
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Set, Union

from backend.core.config import (
    BOLTZ_MSA_CACHE_DIR,
    LEAD_OPTIMIZATION_OUTPUT_DIR,
    MSA_CACHE_RETENTION_DAYS,
    RESULTS_BASE_DIR,
    RESULTS_RETENTION_DAYS,
)

LOGGER = logging.getLogger(__name__)

# backend 结果根目录下的保留名目录：exports 已有自身的文件 TTL 清理，_runtime_tmp
# 作为容器目录跳过（其子目录仍会单独按 mtime 清理）。
_RESERVED_ROOT_DIRS = frozenset({"exports", "_runtime_tmp"})

_UUID_RE = uuid.UUID


# 临时目录/路径中提取 task id 用的 UUID 片段
_TASK_ID_FRAGMENT_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _is_uuid_text(name: str) -> bool:
    """目录名/zip 前缀是否为规范小写 UUID（4 个连字符、长度 36）。"""
    if len(name) != 36 or name.count("-") != 4:
        return False
    try:
        return str(_UUID_RE(name)) == name
    except ValueError:
        return False


def _looks_like_task_id(name: str) -> Optional[str]:
    """从名称中提取 UUID 片段（尽力而为），提取失败返回 None（不参与活动任务过滤）。

    临时目录命名形如 boltz_task_<uuid>_<rand> / p2d_task_<uuid>。
    """
    match = _TASK_ID_FRAGMENT_RE.search(name)
    if not match:
        return None
    try:
        return str(_UUID_RE(match.group(0)))
    except ValueError:
        return None


def _task_id_from_result_zip(filename: str) -> Optional[str]:
    """结果 zip 的 task id：<task_id>_*.zip。"""
    if not filename.lower().endswith(".zip"):
        return None
    prefix = filename.split("_", 1)[0]
    return prefix if _is_uuid_text(prefix) else None


@dataclass
class CleanupStats:
    scanned_files: int = 0
    scanned_dirs: int = 0
    deleted_files: int = 0
    deleted_dirs: int = 0
    freed_bytes: int = 0


def _dir_size(path: Path) -> int:
    """目录内文件大小总和（递归，忽略不可读子项）。"""
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        total += _dir_size(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat().st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def _path_collector(results_base: Path, lead_opt_root: Optional[Path], min_mtime: float, active: Set[str]):
    """产出 (path, is_dir, task_id) 的过期候选。"""
    now_ms = time.time()
    results_base = Path(results_base)
    if results_base.is_dir():
        try:
            with os.scandir(results_base) as it:
                for entry in it:
                    name = entry.name
                    # 根级结果 zip
                    if entry.is_file(follow_symlinks=False):
                        task_id = _task_id_from_result_zip(name)
                        if task_id is None:
                            continue
                        try:
                            if entry.stat().st_mtime <= min_mtime and task_id not in active:
                                yield Path(entry.path), False, task_id
                        except OSError:
                            continue
                        continue
                    # 非保留名的子目录：UUID 命名视为中间结果树（兼容平放），否则按
                    # backend 容器目录进入其下扫描 <task_id> 中间结果树
                    if not entry.is_dir(follow_symlinks=False) or name in _RESERVED_ROOT_DIRS:
                        continue
                    if _is_uuid_text(name):
                        try:
                            if entry.stat().st_mtime <= min_mtime and name not in active:
                                yield Path(entry.path), True, name
                        except OSError:
                            continue
                        continue
                    try:
                        with os.scandir(entry.path) as sub:
                            for sub_entry in sub:
                                if not sub_entry.is_dir(follow_symlinks=False) or not _is_uuid_text(sub_entry.name):
                                    continue
                                try:
                                    if sub_entry.stat().st_mtime <= min_mtime and sub_entry.name not in active:
                                        yield Path(sub_entry.path), True, sub_entry.name
                                except OSError:
                                    continue
                    except OSError:
                        continue
        except OSError:
            LOGGER.exception("扫描结果根目录失败: %s", results_base)

    # _runtime_tmp 泄漏的临时任务目录（不再要求 UUID 目录名，按 mtime 整体清理）
    runtime_tmp = results_base / "_runtime_tmp"
    if runtime_tmp.is_dir():
        try:
            with os.scandir(runtime_tmp) as it:
                for entry in it:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    try:
                        if entry.stat().st_mtime <= min_mtime:
                            task_id = _looks_like_task_id(entry.name)
                            if task_id is None or task_id not in active:
                                yield Path(entry.path), True, task_id
                    except OSError:
                        continue
        except OSError:
            LOGGER.exception("扫描运行时临时目录失败: %s", runtime_tmp)

    # lead optimization 输出目录
    lead = Path(lead_opt_root) if lead_opt_root is not None else Path(LEAD_OPTIMIZATION_OUTPUT_DIR)
    if lead.is_dir() and lead.resolve() != results_base.resolve():
        try:
            with os.scandir(lead) as it:
                for entry in it:
                    if not entry.is_dir(follow_symlinks=False) or not _is_uuid_text(entry.name):
                        continue
                    try:
                        if entry.stat().st_mtime <= min_mtime and entry.name not in active:
                            yield Path(entry.path), True, entry.name
                    except OSError:
                        continue
        except OSError:
            LOGGER.exception("扫描 lead optimization 输出目录失败: %s", lead)


def active_task_ids_from_monitor(db_url: Optional[str] = None, *, timeout: float = 5.0) -> Set[str]:
    """查询 monitor 库中排队/运行中的任务集合，失败时返回空集合并告警。

    调用方在 monitor 服务内运行时数据库必然可用；手动 CLI 在 DB 不可达时
    仅按 mtime 判定（运行中任务的目录 mtime 会持续更新，风险有限）。
    """
    import psycopg2

    url = os.environ.get("VBIO_MONITOR_DATABASE_URL") if db_url is None else db_url
    if not url:
        LOGGER.warning("VBIO_MONITOR_DATABASE_URL 未配置，跳过活动任务保护（仅按 mtime 判定）")
        return set()
    try:
        with psycopg2.connect(url, connect_timeout=int(timeout)) as conn:
            with conn.cursor() as cur:
                # 与 monitor_store 一致：authenticator 账户需切换到 service_role 才能读写
                cur.execute("set local role service_role")
                cur.execute("set local statement_timeout = '10s'")
                cur.execute(
                    "select task_id from public.monitor_tasks_current "
                    "where state_bucket in ('queued', 'running')"
                )
                return {str(row[0]) for row in cur.fetchall()}
    except Exception:
        LOGGER.exception("查询活动任务失败，仅按 mtime 判定")
        return set()


def purge_stale_msa_cache(
    retention_days: Optional[int] = None,
    *,
    cache_dir: Union[str, Path, None] = None,
    dry_run: bool = False,
) -> CleanupStats:
    """清理 MSA(a3m) 序列缓存中超过保留期的文件。

    缓存目录为平铺文件（msa_<hash>.a3m），按文件 mtime 删除。缓存可重建
    （MSA 服务会按序列重新生成），因此不做活动任务保护。
    """
    retention_days = MSA_CACHE_RETENTION_DAYS if retention_days is None else int(retention_days)
    cache = Path(cache_dir) if cache_dir is not None else Path(BOLTZ_MSA_CACHE_DIR)
    min_mtime = time.time() - retention_days * 86400.0
    stats = CleanupStats()
    if not cache.is_dir():
        return stats
    LOGGER.info("MSA 缓存清理开始（retention_days=%s, dry_run=%s）：%s", retention_days, dry_run, cache)
    try:
        with os.scandir(cache) as it:
            for entry in it:
                if not entry.is_file(follow_symlinks=False):
                    continue
                try:
                    if entry.stat().st_mtime > min_mtime:
                        continue
                    freed = entry.stat().st_size
                except OSError:
                    continue
                path = Path(entry.path)
                stats.scanned_files += 1
                if not dry_run:
                    try:
                        path.unlink()
                    except OSError:
                        LOGGER.warning("删除失败，已跳过: %s", path)
                        continue
                stats.deleted_files += 1
                stats.freed_bytes += freed
    except OSError:
        LOGGER.exception("扫描 MSA 缓存目录失败: %s", cache)
    action = "dry-run 统计" if dry_run else "清理完成"
    LOGGER.info(
        "%s：MSA 缓存文件 %d，%s %.2f GB（共扫描 %d 项）",
        action, stats.deleted_files,
        "将释放约" if dry_run else "实际释放约",
        stats.freed_bytes / (1024 ** 3),
        stats.scanned_files,
    )
    return stats


def purge_stale_task_results(
    retention_days: Optional[int] = None,
    *,
    results_base_dir: Union[str, Path, None] = None,
    lead_opt_output_dir: Union[str, Path, None] = None,
    active_task_ids: Optional[Set[str]] = None,
    dry_run: bool = False,
) -> CleanupStats:
    """清理超过保留期的任务结果文件。

    返回统计信息；dry_run=True 时只统计不删除。根目录不存在时静默跳过。
    """
    retention_days = RESULTS_RETENTION_DAYS if retention_days is None else int(retention_days)
    active = set(active_task_ids) if active_task_ids is not None else active_task_ids_from_monitor()
    results_base = Path(results_base_dir) if results_base_dir is not None else Path(RESULTS_BASE_DIR)
    lead_opt = Path(lead_opt_output_dir) if lead_opt_output_dir is not None else Path(LEAD_OPTIMIZATION_OUTPUT_DIR)

    min_mtime = time.time() - retention_days * 86400.0
    stats = CleanupStats()
    LOGGER.info(
        "任务结果清理开始（retention_days=%s, dry_run=%s, 活动任务=%d）：%s、%s",
        retention_days, dry_run, len(active), results_base, lead_opt,
    )

    seen: Set[Path] = set()
    candidates = []
    for path, is_dir, task_id in _path_collector(results_base, lead_opt, min_mtime, active):
        # 结果根目录与 lead_opt 目录在异常配置下可能重叠，按解析后的绝对路径去重
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        candidates.append((path, is_dir, task_id))
    for path, is_dir, _task_id in candidates:
        if is_dir:
            stats.scanned_dirs += 1
            freed = _dir_size(path)
        else:
            stats.scanned_files += 1
            try:
                freed = path.stat().st_size
            except OSError:
                freed = 0
        if not dry_run:
            try:
                if is_dir:
                    # 不静默吞掉权限/占用错误：删除失败必须可见，
                    # 计数也只在删除成功后累加（避免虚报释放）
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError:
                LOGGER.warning("删除失败，已跳过: %s", path)
                continue
        if is_dir:
            stats.deleted_dirs += 1
        else:
            stats.deleted_files += 1
        stats.freed_bytes += freed
        LOGGER.debug(
            "%s %s（约 %.1f MB）",
            "将删除" if dry_run else "已删除",
            path,
            freed / (1024 * 1024),
        )

    action = "dry-run 统计" if dry_run else "清理完成"
    LOGGER.info(
        "%s：zip %d、目录 %d，%s %.2f GB（共扫描 %d 项候选）",
        action, stats.deleted_files, stats.deleted_dirs,
        "将释放约" if dry_run else "实际释放约",
        stats.freed_bytes / (1024 ** 3),
        stats.scanned_files + stats.scanned_dirs,
    )
    return stats


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.monitoring.result_cleanup",
        description="清理超过保留期的 V-Bio 任务结果文件（zip / 中间树 / lead_opt 输出 / 泄漏临时目录）。",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="只统计可删除项与预计释放空间，不删除")
    group.add_argument("--run", action="store_true", help="实际执行清理")
    parser.add_argument(
        "--retention-days", type=int, default=None,
        help=f"保留天数（默认取环境变量 RESULTS_RETENTION_DAYS，当前为 {RESULTS_RETENTION_DAYS}）",
    )
    parser.add_argument("--verbose", action="store_true", help="逐条打印每个被扫描/删除的路径")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    task_stats = purge_stale_task_results(args.retention_days, dry_run=args.dry_run)
    msa_stats = purge_stale_msa_cache(dry_run=args.dry_run)
    LOGGER.info(
        "%s 汇总：任务结果 zip %d/目录 %d + MSA 缓存 %d，共 %s %.2f GB",
        "dry-run 统计" if args.dry_run else "清理完成",
        task_stats.deleted_files, task_stats.deleted_dirs, msa_stats.deleted_files,
        "将释放约" if args.dry_run else "实际释放约",
        (task_stats.freed_bytes + msa_stats.freed_bytes) / (1024 ** 3),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())