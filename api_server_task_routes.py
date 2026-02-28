from __future__ import annotations

import os
import uuid
from typing import Any, Callable, Dict

from celery.result import AsyncResult
from flask import jsonify, request, send_file, send_from_directory
from werkzeug.utils import secure_filename


def register_task_routes(
    app,
    *,
    require_api_token,
    celery_app,
    task_monitor,
    predict_task,
    config_module,
    logger,
    find_result_archive: Callable[[str], str | None],
    resolve_result_archive_path: Callable[[str], tuple[str, str]],
    build_or_get_view_archive: Callable[[str], str],
    get_tracker_status: Callable[[str], tuple[Dict[str, Any] | None, str | None]],
    get_compact_prediction_metrics: Callable[[str], Dict[str, Any] | None],
) -> None:
    @app.route('/status/<task_id>', methods=['GET'])
    def get_task_status(task_id):
        logger.info('Received status request for task ID: %s', task_id)
        task_result = AsyncResult(task_id, app=celery_app)

        response: Dict[str, Any] = {'task_id': task_id, 'state': task_result.state, 'info': {}}
        info = task_result.info

        if task_result.state == 'PENDING':
            archive_name = find_result_archive(task_id)
            if archive_name:
                response['state'] = 'SUCCESS'
                response['info'] = {
                    'status': 'Task completed (result file found on server).',
                    'result_file': archive_name,
                }
                compact_metrics = get_compact_prediction_metrics(task_id)
                if isinstance(compact_metrics, dict) and compact_metrics:
                    response['info']['compact_metrics'] = compact_metrics
                    response['info']['lead_opt_metrics'] = compact_metrics
                logger.info("Task %s marked SUCCESS via result archive '%s'.", task_id, archive_name)
            else:
                tracker_status, heartbeat = get_tracker_status(task_id)
                if tracker_status or heartbeat:
                    response['state'] = 'PROGRESS'
                    status_message = (
                        (tracker_status or {}).get('details')
                        or (tracker_status or {}).get('status')
                        or 'Task is running'
                    )
                    response['info'] = {
                        'status': status_message,
                        'message': status_message,
                        'tracker': tracker_status or {},
                        'heartbeat': heartbeat,
                    }
                    tracker_payload = (tracker_status or {}).get('payload')
                    if isinstance(tracker_payload, dict) and tracker_payload:
                        response['info'].update(tracker_payload)
                    logger.info('Task %s is running per tracker; Celery state PENDING.', task_id)
                else:
                    response['info']['status'] = 'Task is waiting in the queue or the task ID does not exist.'
                    logger.info('Task %s is PENDING or non-existent.', task_id)
        elif task_result.state == 'SUCCESS':
            response['info'] = info if isinstance(info, dict) else {'result': str(info)}
            if not isinstance(response['info'], dict):
                response['info'] = {'result': str(response['info'])}
            if not response['info'].get('result_file'):
                archive_name = find_result_archive(task_id)
                if archive_name:
                    response['info']['result_file'] = archive_name
            compact_metrics = None
            if isinstance(response['info'].get('compact_metrics'), dict):
                compact_metrics = response['info']['compact_metrics']
            elif isinstance(response['info'].get('lead_opt_metrics'), dict):
                compact_metrics = response['info']['lead_opt_metrics']
            else:
                compact_metrics = get_compact_prediction_metrics(task_id)
            if isinstance(compact_metrics, dict) and compact_metrics:
                response['info']['compact_metrics'] = compact_metrics
                response['info']['lead_opt_metrics'] = compact_metrics
            response['info']['status'] = 'Task completed successfully.'
            logger.info('Task %s is SUCCESS.', task_id)
        elif task_result.state == 'FAILURE':
            response['info'] = (
                {'exc_type': type(info).__name__, 'exc_message': str(info)}
                if isinstance(info, Exception)
                else (info if isinstance(info, dict) else {'message': str(info)})
            )
            logger.error('Task %s is in FAILURE state. Info: %s', task_id, response['info'])
        elif task_result.state == 'REVOKED':
            runtime_processes = task_monitor._find_task_processes(task_id)
            runtime_container_snapshot = task_monitor._discover_task_containers(task_id)
            runtime_containers = runtime_container_snapshot.get('containers') or []
            running_containers = [container for container in runtime_containers if container.get('running')]
            if runtime_processes or running_containers:
                response['state'] = 'PROGRESS'
                response['info'] = {
                    'status': 'Termination in progress; runtime still active.',
                    'message': 'Task revoke acknowledged but runtime process/container is still active.',
                    'process_count': len(runtime_processes),
                    'container_count': len(running_containers),
                }
                logger.warning('Task %s marked REVOKED but runtime is still active.', task_id)
            else:
                response['info']['status'] = 'Task was revoked.'
                logger.warning('Task %s was REVOKED.', task_id)
        else:
            response['info'] = info if isinstance(info, dict) else {'message': str(info)}
            logger.info('Task %s is in state: %s.', task_id, task_result.state)

        return jsonify(response)

    @app.route('/results/<task_id>', methods=['GET'])
    def download_results(task_id):
        logger.info('Received download request for task ID: %s', task_id)
        try:
            filename, filepath = resolve_result_archive_path(task_id)
        except FileNotFoundError as exc:
            task_result = AsyncResult(task_id, app=celery_app)
            logger.warning('Failed to resolve results for task %s: %s', task_id, exc)
            return jsonify({'error': str(exc), 'state': task_result.state}), 404
        except PermissionError as exc:
            logger.error('Invalid result path for task %s: %s', task_id, exc)
            return jsonify({'error': 'Invalid file path detected.'}), 400
        except Exception as exc:
            logger.exception('Unexpected error while resolving full results for task %s: %s', task_id, exc)
            return jsonify({'error': f'Failed to resolve full result archive: {exc}'}), 500

        directory = app.config['UPLOAD_FOLDER']
        logger.info('Serving full result file %s for task %s from %s.', filename, task_id, filepath)
        return send_from_directory(
            directory,
            filename,
            as_attachment=True,
            conditional=False,
            etag=False,
            max_age=0,
        )

    @app.route('/results/<task_id>/view', methods=['GET'])
    def download_results_view(task_id):
        logger.info('Received view download request for task ID: %s', task_id)
        try:
            _, filepath = resolve_result_archive_path(task_id)
        except FileNotFoundError as exc:
            task_result = AsyncResult(task_id, app=celery_app)
            logger.warning('Failed to resolve view results for task %s: %s', task_id, exc)
            return jsonify({'error': str(exc), 'state': task_result.state}), 404
        except PermissionError as exc:
            logger.error('Invalid view result path for task %s: %s', task_id, exc)
            return jsonify({'error': 'Invalid file path detected.'}), 400
        except Exception as exc:
            logger.exception('Unexpected error while resolving view source archive for task %s: %s', task_id, exc)
            return jsonify({'error': f'Failed to resolve source result archive for view: {exc}'}), 500

        try:
            view_path = build_or_get_view_archive(filepath)
        except Exception as exc:
            logger.warning('Failed to build view archive for task %s from %s: %s', task_id, filepath, exc)
            return jsonify({'error': f'Failed to build view archive: {exc}'}), 500

        download_name = f'{task_id}_view_results.zip'
        logger.info('Serving view result archive for task %s: %s', task_id, view_path)
        return send_file(
            view_path,
            as_attachment=True,
            download_name=download_name,
            conditional=False,
            etag=False,
            max_age=0,
            mimetype='application/zip',
        )

    @app.route('/upload_result/<task_id>', methods=['POST'])
    def upload_result_from_worker(task_id):
        logger.info('Received file upload request from worker for task ID: %s', task_id)

        if 'file' not in request.files:
            logger.error('No file part in the upload request for task %s.', task_id)
            return jsonify({'error': 'No file part in the request'}), 400

        file = request.files['file']
        if file.filename == '':
            logger.error('No selected file for upload for task %s.', task_id)
            return jsonify({'error': 'No selected file'}), 400

        try:
            filename = secure_filename(file.filename)
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            temp_save_path = f"{save_path}.upload-{uuid.uuid4().hex}.tmp"
            file.save(temp_save_path)
            os.replace(temp_save_path, save_path)

            lower_name = filename.lower()
            should_prebuild_view = (
                lower_name.endswith('.zip')
                and 'virtual_screening' not in lower_name
                and 'lead_optimization' not in lower_name
            )
            if should_prebuild_view:
                try:
                    build_or_get_view_archive(save_path)
                except Exception as view_exc:
                    logger.warning('Failed to prebuild view archive for %s (task %s): %s', filename, task_id, view_exc)

            logger.info("Result file '%s' for task %s received and saved to %s.", filename, task_id, save_path)
            return jsonify({'message': f"File '{filename}' uploaded successfully for task {task_id}"}), 200
        except IOError as exc:
            try:
                if 'temp_save_path' in locals() and os.path.exists(temp_save_path):
                    os.remove(temp_save_path)
            except Exception:
                pass
            logger.exception("Failed to save uploaded file '%s' for task %s: %s", filename, task_id, exc)
            return jsonify({'error': f'Failed to save file: {exc}'}), 500
        except Exception as exc:
            try:
                if 'temp_save_path' in locals() and os.path.exists(temp_save_path):
                    os.remove(temp_save_path)
            except Exception:
                pass
            logger.exception('An unexpected error occurred during file upload for task %s: %s', task_id, exc)
            return jsonify({'error': f'An unexpected error occurred: {exc}'}), 500

    @app.route('/tasks', methods=['GET'])
    @require_api_token
    def list_tasks():
        logger.info('Received request to list all tasks.')
        inspector = celery_app.control.inspect()

        try:
            active = inspector.active() or {}
            reserved = inspector.reserved() or {}
            scheduled = inspector.scheduled() or {}

            all_tasks = {
                'active': [task for worker_tasks in active.values() for task in worker_tasks],
                'reserved': [task for worker_tasks in reserved.values() for task in worker_tasks],
                'scheduled': [task for worker_tasks in scheduled.values() for task in worker_tasks],
            }
            logger.info(
                'Successfully listed tasks. Active: %s, Reserved: %s, Scheduled: %s',
                len(all_tasks['active']),
                len(all_tasks['reserved']),
                len(all_tasks['scheduled']),
            )
            return jsonify(all_tasks)
        except Exception as exc:
            logger.exception('Error inspecting Celery workers: %s. Ensure workers are running and reachable.', exc)
            return jsonify({
                'error': 'Could not inspect Celery workers. Ensure workers are running and reachable.',
                'details': str(exc),
            }), 500

    @app.route('/tasks/<task_id>', methods=['DELETE'])
    @require_api_token
    def terminate_task(task_id):
        logger.info('Received request to terminate task ID: %s', task_id)
        try:
            termination = task_monitor.terminate_task_runtime(task_id, force=True)
            if not termination.get('ok'):
                logger.error('Task %s runtime termination failed: %s', task_id, termination)
                return jsonify({
                    'status': 'Task termination failed; runtime is still active.',
                    'task_id': task_id,
                    'terminated': False,
                    'details': termination,
                }), 409

            logger.info('Task %s runtime terminated successfully.', task_id)
            return jsonify({
                'status': 'Task terminated successfully.',
                'task_id': task_id,
                'terminated': True,
                'details': termination,
            }), 200
        except Exception as exc:
            logger.exception('Failed to terminate task %s: %s', task_id, exc)
            return jsonify({'error': 'Failed to terminate task runtime.', 'details': str(exc)}), 500

    @app.route('/tasks/<task_id>/move', methods=['POST'])
    @require_api_token
    def move_task(task_id):
        logger.info('Received request to move task ID: %s', task_id)
        data = request.get_json()
        if not data or 'target_queue' not in data:
            logger.error("Invalid request to move task %s: missing 'target_queue'.", task_id)
            return jsonify({'error': "Request body must be JSON and contain 'target_queue'."}), 400

        target_queue = data['target_queue']
        valid_queues = [config_module.HIGH_PRIORITY_QUEUE, config_module.DEFAULT_QUEUE, config_module.CPU_QUEUE]
        if target_queue not in valid_queues:
            logger.error("Invalid target_queue '%s' for task %s. Allowed: %s", target_queue, task_id, valid_queues)
            return jsonify({'error': f"Invalid 'target_queue'. Must be one of: {', '.join(valid_queues)}."}), 400

        inspector = celery_app.control.inspect()
        try:
            reserved_tasks_by_worker = inspector.reserved() or {}
            task_info = None
            for _, tasks in reserved_tasks_by_worker.items():
                for task in tasks:
                    if task['id'] == task_id:
                        task_info = task
                        break
                if task_info:
                    break

            if not task_info:
                logger.warning('Task %s not found in reserved queue. It may be running, completed, or non-existent.', task_id)
                return jsonify({'error': 'Task not found in reserved queue. It may be running, completed, or non-existent.'}), 404

            celery_app.control.revoke(task_id, terminate=False, send_event=True)
            logger.info('Revoked original task %s for moving.', task_id)

            original_args = task_info.get('args', [])
            original_kwargs = task_info.get('kwargs', {})
            new_task = predict_task.apply_async(args=original_args, kwargs=original_kwargs, queue=target_queue)
            logger.info('Task %s successfully moved to new task ID: %s in queue: %s.', task_id, new_task.id, target_queue)

            return jsonify({
                'status': 'moved',
                'original_task_id': task_id,
                'new_task_id': new_task.id,
                'target_queue': target_queue,
                'message': f'Task {task_id} was moved to a new task {new_task.id} in queue {target_queue}.',
            }), 200
        except Exception as exc:
            logger.exception('Failed to move task %s: %s', task_id, exc)
            return jsonify({'error': 'Failed to move task.', 'details': str(exc)}), 500
