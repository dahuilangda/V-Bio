from __future__ import annotations

import glob
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict

from flask import jsonify, request


def _get_msa_cache_stats(msa_cache_config: Dict[str, Any]) -> Dict[str, Any]:
    cache_dir = msa_cache_config['cache_dir']
    if not os.path.exists(cache_dir):
        return {
            'total_files': 0,
            'total_size_mb': 0,
            'oldest_file': None,
            'newest_file': None,
        }

    msa_files = glob.glob(os.path.join(cache_dir, 'msa_*.a3m'))
    if not msa_files:
        return {
            'total_files': 0,
            'total_size_mb': 0,
            'oldest_file': None,
            'newest_file': None,
        }

    total_size = sum(os.path.getsize(file_path) for file_path in msa_files)
    file_times = [(file_path, os.path.getmtime(file_path)) for file_path in msa_files]
    file_times.sort(key=lambda item: item[1])

    oldest_file = datetime.fromtimestamp(file_times[0][1])
    newest_file = datetime.fromtimestamp(file_times[-1][1])

    return {
        'total_files': len(msa_files),
        'total_size_mb': round(total_size / (1024 * 1024), 2),
        'oldest_file': oldest_file.strftime('%Y-%m-%d %H:%M:%S'),
        'newest_file': newest_file.strftime('%Y-%m-%d %H:%M:%S'),
    }


def _cleanup_expired_msa_cache(msa_cache_config: Dict[str, Any], logger) -> Dict[str, Any]:
    cache_dir = msa_cache_config['cache_dir']
    if not os.path.exists(cache_dir):
        return {'removed_files': 0, 'freed_space_mb': 0}

    max_age_seconds = msa_cache_config['max_age_days'] * 24 * 3600
    current_time = time.time()

    msa_files = glob.glob(os.path.join(cache_dir, 'msa_*.a3m'))
    removed_files = 0
    freed_space = 0

    for file_path in msa_files:
        file_age = current_time - os.path.getmtime(file_path)
        if file_age <= max_age_seconds:
            continue
        file_size = os.path.getsize(file_path)
        try:
            os.remove(file_path)
            removed_files += 1
            freed_space += file_size
            logger.info('清理过期MSA缓存文件: %s', os.path.basename(file_path))
        except Exception as exc:
            logger.error('清理缓存文件失败 %s: %s', file_path, exc)

    return {
        'removed_files': removed_files,
        'freed_space_mb': round(freed_space / (1024 * 1024), 2),
    }


def _cleanup_oversized_msa_cache(msa_cache_config: Dict[str, Any], logger) -> Dict[str, Any]:
    cache_dir = msa_cache_config['cache_dir']
    if not os.path.exists(cache_dir):
        return {'removed_files': 0, 'freed_space_mb': 0}

    max_size_bytes = msa_cache_config['max_size_gb'] * 1024 * 1024 * 1024
    msa_files = glob.glob(os.path.join(cache_dir, 'msa_*.a3m'))
    if not msa_files:
        return {'removed_files': 0, 'freed_space_mb': 0}

    file_info = [(file_path, os.path.getsize(file_path), os.path.getatime(file_path)) for file_path in msa_files]
    current_size = sum(item[1] for item in file_info)
    if current_size <= max_size_bytes:
        return {'removed_files': 0, 'freed_space_mb': 0}

    file_info.sort(key=lambda item: item[2])
    removed_files = 0
    freed_space = 0

    for file_path, file_size, _ in file_info:
        if current_size <= max_size_bytes:
            break
        try:
            os.remove(file_path)
            removed_files += 1
            freed_space += file_size
            current_size -= file_size
            logger.info('清理超量MSA缓存文件: %s', os.path.basename(file_path))
        except Exception as exc:
            logger.error('清理缓存文件失败 %s: %s', file_path, exc)

    return {
        'removed_files': removed_files,
        'freed_space_mb': round(freed_space / (1024 * 1024), 2),
    }


def _calculate_directory_metrics(target: Path, logger) -> tuple[int, int]:
    total_size = 0
    total_files = 0

    if not target.exists():
        return total_size, total_files

    for root, _, files in os.walk(target):
        for file_name in files:
            file_path = Path(root) / file_name
            try:
                total_size += file_path.stat().st_size
                total_files += 1
            except OSError as exc:
                logger.warning('计算路径大小时忽略 %s: %s', file_path, exc)

    return total_size, total_files


def register_admin_routes(
    app,
    *,
    require_api_token,
    msa_cache_config: Dict[str, Any],
    colabfold_jobs_dir: str,
    logger,
    task_monitor,
    get_gpu_status_fn: Callable[[], Dict[str, Any]],
) -> None:
    @app.route('/api/msa/cache/stats', methods=['GET'])
    @require_api_token
    def get_msa_cache_stats_api():
        try:
            stats = _get_msa_cache_stats(msa_cache_config)
            return jsonify({'success': True, 'data': stats}), 200
        except Exception as exc:
            logger.exception('获取MSA缓存统计失败: %s', exc)
            return jsonify({'error': 'Failed to get MSA cache statistics', 'details': str(exc)}), 500

    @app.route('/api/msa/cache/cleanup', methods=['POST'])
    @require_api_token
    def cleanup_msa_cache_api():
        try:
            stats_before = _get_msa_cache_stats(msa_cache_config)
            expired_result = _cleanup_expired_msa_cache(msa_cache_config, logger)
            oversized_result = _cleanup_oversized_msa_cache(msa_cache_config, logger)
            stats_after = _get_msa_cache_stats(msa_cache_config)

            result = {
                'before': stats_before,
                'after': stats_after,
                'expired_cleanup': expired_result,
                'oversized_cleanup': oversized_result,
                'total_removed': expired_result['removed_files'] + oversized_result['removed_files'],
                'total_freed_mb': expired_result['freed_space_mb'] + oversized_result['freed_space_mb'],
            }
            logger.info(
                'MSA缓存清理完成: 删除 %s 个文件，释放 %s MB空间',
                result['total_removed'],
                result['total_freed_mb'],
            )
            return jsonify({'success': True, 'data': result}), 200
        except Exception as exc:
            logger.exception('MSA缓存清理失败: %s', exc)
            return jsonify({'error': 'Failed to cleanup MSA cache', 'details': str(exc)}), 500

    @app.route('/api/msa/cache/clear', methods=['POST'])
    @require_api_token
    def clear_all_msa_cache_api():
        try:
            cache_dir = msa_cache_config['cache_dir']
            if not os.path.exists(cache_dir):
                return jsonify({'success': True, 'data': {'removed_files': 0, 'freed_space_mb': 0.0}}), 200

            total_size = 0
            total_items = 0
            for root, _, files in os.walk(cache_dir):
                for file_name in files:
                    file_path = os.path.join(root, file_name)
                    try:
                        total_size += os.path.getsize(file_path)
                        total_items += 1
                    except OSError as exc:
                        logger.warning('计算缓存文件大小失败 %s: %s', file_path, exc)

            try:
                shutil.rmtree(cache_dir)
                logger.info('已删除整个MSA缓存目录: %s', cache_dir)
            except Exception as exc:
                logger.exception('清空MSA缓存目录失败: %s', exc)
                return jsonify({'error': 'Failed to clear MSA cache', 'details': str(exc)}), 500

            try:
                os.makedirs(cache_dir, exist_ok=True)
            except OSError as exc:
                logger.error('重建MSA缓存目录失败: %s', exc)

            result = {
                'removed_files': total_items,
                'freed_space_mb': round(total_size / (1024 * 1024), 2),
            }
            logger.info(
                'MSA缓存清空完成: 删除 %s 个文件，释放 %s MB空间',
                result['removed_files'],
                result['freed_space_mb'],
            )
            return jsonify({'success': True, 'data': result}), 200
        except Exception as exc:
            logger.exception('MSA缓存清空失败: %s', exc)
            return jsonify({'error': 'Failed to clear MSA cache', 'details': str(exc)}), 500

    @app.route('/api/colabfold/cache/clear', methods=['POST'])
    @require_api_token
    def clear_colabfold_cache_api():
        jobs_dir = Path(colabfold_jobs_dir).expanduser()
        if not jobs_dir.exists():
            logger.info('ColabFold jobs 目录不存在: %s', jobs_dir)
            return jsonify({'success': True, 'data': {'removed_items': 0, 'freed_space_mb': 0.0}}), 200

        total_size, _ = _calculate_directory_metrics(jobs_dir, logger)

        removed_items = 0
        failures: list[dict[str, str]] = []
        for entry in jobs_dir.iterdir():
            try:
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                removed_items += 1
            except Exception as exc:
                logger.error('清理 ColabFold 缓存条目失败 %s: %s', entry, exc)
                failures.append({'path': str(entry), 'error': str(exc)})

        result = {
            'removed_items': removed_items,
            'freed_space_mb': round(total_size / (1024 * 1024), 2),
            'failed_items': failures,
        }
        logger.info(
            'ColabFold 缓存清理完成: 删除 %s 个条目，释放 %.2f MB 空间',
            removed_items,
            result['freed_space_mb'],
        )

        status_code = 200 if not failures else 207
        return jsonify({'success': not failures, 'data': result}), status_code

    @app.route('/monitor/status', methods=['GET'])
    @require_api_token
    def get_monitor_status():
        try:
            gpu_status = get_gpu_status_fn()
            stuck_tasks = task_monitor.get_stuck_tasks()

            running_tasks = []
            for gpu_id, task_id in gpu_status['in_use'].items():
                task_info = task_monitor._analyze_task(task_id)
                if task_info:
                    task_info['gpu_id'] = gpu_id
                    running_tasks.append(task_info)

            result = {
                'gpu_status': {
                    'available_count': gpu_status['available_count'],
                    'available': gpu_status['available'],
                    'in_use_count': gpu_status['in_use_count'],
                    'in_use': gpu_status['in_use'],
                },
                'running_tasks': running_tasks,
                'stuck_tasks': stuck_tasks,
                'stuck_count': len(stuck_tasks),
                'timestamp': datetime.now().isoformat(),
            }
            logger.info('任务状态查询: %s 个运行中任务, %s 个卡死任务', len(running_tasks), len(stuck_tasks))
            return jsonify({'success': True, 'data': result}), 200
        except Exception as exc:
            logger.exception('获取任务状态失败: %s', exc)
            return jsonify({'error': 'Failed to get task status', 'details': str(exc)}), 500

    @app.route('/monitor/clean', methods=['POST'])
    @require_api_token
    def clean_stuck_tasks():
        try:
            payload = request.json if request.is_json else {}
            force = bool(payload.get('force', False))
            task_ids = payload.get('task_ids')

            if task_ids is None:
                clean_results = task_monitor.clean_completed_tasks()
                kill_results = task_monitor.kill_stuck_tasks(force=force)
            else:
                clean_results = {'cleaned_gpus': [], 'failed_to_clean': []}
                kill_results = task_monitor.kill_stuck_tasks(task_ids, force=force)

            result = {
                'cleaned_completed_tasks': clean_results,
                'killed_stuck_tasks': kill_results,
                'total_cleaned_gpus': len(clean_results['cleaned_gpus']) + len(kill_results['released_gpus']),
                'total_killed_tasks': len(kill_results['killed_tasks']),
            }
            logger.info(
                '任务清理完成: 清理了 %s 个GPU, 终止了 %s 个任务',
                result['total_cleaned_gpus'],
                result['total_killed_tasks'],
            )
            return jsonify({'success': True, 'data': result}), 200
        except Exception as exc:
            logger.exception('清理任务失败: %s', exc)
            return jsonify({'error': 'Failed to clean tasks', 'details': str(exc)}), 500

    @app.route('/monitor/kill-all', methods=['POST'])
    @require_api_token
    def kill_all_tasks():
        try:
            payload = request.json if request.is_json else {}
            force = bool(payload.get('force', True))

            gpu_status = get_gpu_status_fn()
            all_task_ids = list(gpu_status['in_use'].values())

            if not all_task_ids:
                return jsonify({
                    'success': True,
                    'data': {
                        'message': '没有找到正在运行的任务',
                        'killed_tasks': [],
                        'released_gpus': [],
                    },
                }), 200

            results = task_monitor.kill_stuck_tasks(all_task_ids, force=force)
            logger.warning(
                '紧急清理所有任务: 终止了 %s 个任务, 释放了 %s 个GPU',
                len(results['killed_tasks']),
                len(results['released_gpus']),
            )
            return jsonify({'success': True, 'data': results}), 200
        except Exception as exc:
            logger.exception('紧急清理失败: %s', exc)
            return jsonify({'error': 'Failed to kill all tasks', 'details': str(exc)}), 500

    @app.route('/monitor/health', methods=['GET'])
    def health_check():
        try:
            gpu_status = get_gpu_status_fn()
            stuck_tasks = task_monitor.get_stuck_tasks()

            is_healthy = len(stuck_tasks) == 0
            result = {
                'healthy': is_healthy,
                'gpu_available': gpu_status['available_count'],
                'gpu_in_use': gpu_status['in_use_count'],
                'stuck_tasks_count': len(stuck_tasks),
                'timestamp': datetime.now().isoformat(),
            }
            return jsonify(result), 200 if is_healthy else 503
        except Exception as exc:
            logger.exception('健康检查失败: %s', exc)
            return jsonify({
                'healthy': False,
                'error': str(exc),
                'timestamp': datetime.now().isoformat(),
            }), 503
