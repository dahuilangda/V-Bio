from __future__ import annotations

import json
import os
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional, Tuple

from celery.result import AsyncResult
from flask import jsonify, request, send_file, send_from_directory
from werkzeug.utils import secure_filename

from backend.runtime import peptide_archive_layout

# A 200-compound ranking is a few hundred KB; 32 MB bounds decompression of a crafted member.
SCREENING_JSON_MAX_BYTES = 32 * 1024 * 1024


def register_task_routes(
    app,
    *,
    require_api_token,
    celery_app,
    task_monitor,
    predict_task,
    logger,
    find_result_archive: Callable[[str], str | None],
    resolve_result_archive_path: Callable[[str], tuple[str, str]],
    build_or_get_view_archive: Callable[..., str],
    get_tracker_status: Callable[[str], tuple[Dict[str, Any] | None, str | None]],
    get_compact_prediction_metrics: Callable[[str], Dict[str, Any] | None],
    list_known_queues: Callable[[], list[str]],
    get_worker_capability_snapshot: Callable[[], Dict[str, Any]],
    **extra,
) -> None:
    def _as_record(value: Any) -> Dict[str, Any]:
        return value if isinstance(value, dict) else {}

    def _merge_tracker_payload(response: Dict[str, Any], tracker_status: Dict[str, Any] | None) -> None:
        tracker_payload = _as_record(_as_record(tracker_status).get('payload'))
        if not tracker_payload:
            return
        info = _as_record(response.get('info'))
        info.update(tracker_payload)
        status_text = str(tracker_payload.get('status') or '').strip()
        if status_text:
            info['status'] = status_text
            info['message'] = status_text
        response['info'] = info

    # Completed-task archives are read on every status poll; the frontend task
    # list batches up to 256 task IDs per poll. Cache the parsed summary and
    # member list keyed on (path, mtime_ns, size) so repeated polls are memory
    # reads, and a re-uploaded archive invalidates itself via its new stat.
    # Eviction is FIFO (a bounded cache that over-evicts under scan pressure is
    # still correct). SNAPSHOTS ARE SHARED ACROSS REQUESTS: treat the returned
    # design_summary/names as read-only — in-place edits would leak between
    # callers.
    _archive_snapshot_cache: Dict[Tuple[str, int, int], Dict[str, Any]] = {}
    _archive_snapshot_cache_max = 256

    def _archive_snapshot(archive_name: str) -> Optional[Dict[str, Any]]:
        """(design_summary, zip member names, chain-pLDDT cache) per archive.

        The snapshot is shared across request threads (see the eviction note
        below): consumers must treat it as read-only. `_chain_means_by_name`
        is the one lazily-filled entry — worst case two threads compute the
        same structure's means concurrently and one result overwrites the
        other with an identical value.
        """
        directory = app.config['UPLOAD_FOLDER']
        filepath = os.path.join(directory, secure_filename(archive_name))
        try:
            stat = os.stat(filepath)
            key = (filepath, stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None
        cached = _archive_snapshot_cache.get(key)
        if cached is not None:
            return cached
        try:
            with zipfile.ZipFile(filepath) as src_zip:
                names = src_zip.namelist()
                summary_name, _results_name = peptide_archive_layout.summary_member_names(names)
                design_summary = json.loads(src_zip.read(summary_name)) if summary_name else None
        except Exception:
            return None
        if not isinstance(design_summary, dict):
            return None
        snapshot = {'design_summary': design_summary, 'names': names, '_chain_means_by_name': {}}
        if len(_archive_snapshot_cache) >= _archive_snapshot_cache_max:
            # /status/batch reads this cache from a thread pool: a concurrent
            # evictor can pop the same first key (KeyError) or resize the dict
            # mid-iteration (RuntimeError). Skipping one eviction is harmless —
            # once at max+1 every insert retries eviction, so the cache stays
            # bounded — while an unhandled error here 500s single-task status.
            try:
                _archive_snapshot_cache.pop(next(iter(_archive_snapshot_cache)), None)
            except RuntimeError:
                pass
        _archive_snapshot_cache[key] = snapshot
        return snapshot

    def _read_archive_result_payloads(archive_name: str) -> tuple[Dict[str, Any] | None, Dict[str, Any] | None]:
        """Read results_summary.json from a result archive (cached per stat).

        Returns (design_summary, None); (None, None) when the archive carries
        no completion summary. design_results.json is no longer materialized
        here — no caller consumes it and it is by far the largest member.
        """
        snapshot = _archive_snapshot(archive_name)
        if snapshot is None:
            return None, None
        return snapshot['design_summary'], None

    def _archive_chain_mean_plddt_cached(
        snapshot: Dict[str, Any], filepath: str, structure_name: str
    ) -> Dict[str, float]:
        """Per-chain mean pLDDT of the archive's display structure.

        Cached per snapshot so repeated status polls of the same completed
        task stop re-opening the zip and re-scanning B-factors (the hot path
        for the task list's periodic status batch)."""
        cache = snapshot.setdefault('_chain_means_by_name', {})
        cached = cache.get(structure_name)
        if cached is not None:
            return cached
        means: Dict[str, float] = {}
        try:
            with zipfile.ZipFile(filepath) as src_zip:
                member = next(
                    (n for n in src_zip.namelist()
                     if os.path.basename(n) == structure_name or n.endswith('/' + structure_name)),
                    None
                )
                if member:
                    means = peptide_archive_layout.chain_mean_plddt_from_structure_text(
                        src_zip.read(member).decode('utf-8', 'replace'))
        except Exception:
            means = {}
        cache[structure_name] = means
        return means

    def _archive_completed_info(task_id: str, archive_name: str) -> Dict[str, Any]:
        """Build the completed-task info block from durable storage.

        The on-disk archive is the source of truth so /status stays independent
        of redis/celery metadata retention. The archive is read through the
        stat-keyed snapshot cache; one zip open serves every field.
        """
        info: Dict[str, Any] = {
            'status': 'Task completed successfully.',
            'result_file': archive_name,
        }
        snapshot = _archive_snapshot(archive_name)
        design_summary = snapshot['design_summary'] if snapshot else None
        if isinstance(design_summary, dict):
            summary = _as_record(design_summary.get('summary'))
            top_results = design_summary.get('top_results')
            if not isinstance(top_results, list) or not top_results:
                candidate_list = design_summary.get('best_sequences')
                top_results = candidate_list if isinstance(candidate_list, list) else []
            best_row = _as_record(top_results[0]) if isinstance(top_results, list) and top_results else {}
            # The rank-1 row can ship structureless (checkpoint-resumed exports
            # legitimately lose early-gen structures to tmp cleanup); resolve a
            # structure name that ACTUALLY exists in the archive.
            names = snapshot['names'] if snapshot else []
            structure_name = peptide_archive_layout.pick_display_structure(top_results, names)
            iterations = summary.get('iterations')
            info['peptide_design'] = {
                'best_score': summary.get('best_score'),
                'best_sequences': top_results,
                'candidate_count': len(top_results) if isinstance(top_results, list) else None,
                'completed_tasks': summary.get('completed_tasks'),
                'total_tasks': summary.get('total_tasks'),
                'current_status': 'completed',
                'current_generation': iterations,
                'total_generations': iterations,
                'progress_percent': 100,
                'status_message': 'Task completed.',
            }
            # Top-level confidences describe the best-ranked candidate so the
            # UI never shows placeholder zeros.
            for src_key, dst_key in (
                ('iptm', 'iptm'),
                ('pair_iptm', 'pair_iptm'),
                ('plddt', 'ligand_plddt'),
                ('ligand_ipsae_max', 'ligand_ipsae_max'),
                ('binder_chain_id', 'binder_chain_id'),
                ('target_chain_id', 'target_chain_id'),
            ):
                value = best_row.get(src_key)
                if value is not None:
                    info[dst_key] = value
            if structure_name:
                info['structure_name'] = structure_name
                directory = app.config['UPLOAD_FOLDER']
                filepath = os.path.join(directory, secure_filename(archive_name))
                chain_means = _archive_chain_mean_plddt_cached(snapshot, filepath, structure_name)
                if chain_means:
                    info['chain_mean_plddt'] = chain_means
        compact_metrics = get_compact_prediction_metrics(task_id)
        if isinstance(compact_metrics, dict) and compact_metrics:
            info['compact_metrics'] = compact_metrics
            info['lead_opt_metrics'] = compact_metrics
        return info

    def build_task_status_response(task_id: str) -> Dict[str, Any]:
        # Finished tasks are served from the on-disk archive (source of truth);
        # Celery is consulted only for tasks without one.
        archive_name = find_result_archive(task_id)
        if archive_name:
            design_summary, _ = _read_archive_result_payloads(archive_name)
            if isinstance(design_summary, dict):
                response: Dict[str, Any] = {
                    'task_id': task_id,
                    'state': 'SUCCESS',
                    'info': _archive_completed_info(task_id, archive_name),
                }
                logger.debug(
                    "Task %s served from result archive '%s' (durable storage).",
                    task_id, archive_name
                )
                return response

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
                    logger.debug("Task %s marked SUCCESS via result archive '%s'.", task_id, archive_name)
            else:
                tracker_status, heartbeat = get_tracker_status(task_id)
                if tracker_status or heartbeat:
                    tracker_status_token = str((tracker_status or {}).get('status') or '').strip().lower()
                    response['state'] = 'PROGRESS'
                    status_message = (
                        (tracker_status or {}).get('details')
                        or (tracker_status or {}).get('status')
                        or 'Task is running'
                    )
                    if tracker_status_token in {'completed', 'complete', 'success', 'succeeded'}:
                        response['state'] = 'SUCCESS'
                    elif tracker_status_token in {'failed', 'failure', 'timeout', 'error'}:
                        response['state'] = 'FAILURE'
                    response['info'] = {
                        'status': status_message,
                        'message': status_message,
                        'tracker': tracker_status or {},
                        'heartbeat': heartbeat,
                    }
                    _merge_tracker_payload(response, tracker_status)
                    if response['state'] == 'SUCCESS':
                        archive_name_from_tracker = find_result_archive(task_id)
                        if archive_name_from_tracker:
                            response['info']['result_file'] = archive_name_from_tracker
                            compact_metrics = get_compact_prediction_metrics(task_id)
                            if isinstance(compact_metrics, dict) and compact_metrics:
                                response['info']['compact_metrics'] = compact_metrics
                                response['info']['lead_opt_metrics'] = compact_metrics
                        logger.debug('Task %s inferred SUCCESS from tracker status while Celery state PENDING.', task_id)
                    elif response['state'] == 'FAILURE':
                        logger.warning('Task %s inferred FAILURE from tracker status while Celery state PENDING.', task_id)
                    else:
                        logger.debug('Task %s is running per tracker; Celery state PENDING.', task_id)
                else:
                    waiting_message = 'Task is waiting in the queue.'
                    response['info']['status'] = waiting_message
                    response['info']['message'] = waiting_message
                    logger.debug('Task %s is PENDING and waiting for worker pickup.', task_id)
        elif task_result.state == 'SUCCESS':
            response['info'] = info if isinstance(info, dict) else {'result': str(info)}
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
            logger.debug('Task %s is SUCCESS.', task_id)
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
            archive_name = find_result_archive(task_id)
            if archive_name:
                response['state'] = 'SUCCESS'
                response['info'] = info if isinstance(info, dict) else {'result': str(info)}
                if not isinstance(response['info'], dict):
                    response['info'] = {'result': str(response['info'])}
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
                logger.debug("Task %s marked SUCCESS via result archive '%s' from state %s.", task_id, archive_name, task_result.state)
            else:
                tracker_status, heartbeat = get_tracker_status(task_id)
                tracker_message = ''
                if isinstance(tracker_status, dict):
                    tracker_message = str(tracker_status.get('details') or tracker_status.get('status') or '').strip()
                info_payload = info if isinstance(info, dict) else {'message': str(info)}
                candidate_message = str(
                    info_payload.get('status')
                    or info_payload.get('message')
                    or tracker_message
                    or ''
                ).strip()
                lowered_message = candidate_message.lower()
                failure_hint = (
                    lowered_message and (
                        'failed' in lowered_message
                        or 'error' in lowered_message
                        or 'timeout' in lowered_message
                        or 'terminated' in lowered_message
                    )
                )
                if failure_hint:
                    response['state'] = 'FAILURE'
                    response['info'] = {
                        **info_payload,
                        'message': candidate_message or 'Task failed.',
                    }
                    if tracker_status:
                        response['info']['tracker'] = tracker_status
                    if heartbeat:
                        response['info']['heartbeat'] = heartbeat
                    logger.warning('Task %s inferred FAILURE from runtime status text while state=%s.', task_id, task_result.state)
                else:
                    response['info'] = info_payload
                    if tracker_status:
                        response['info']['tracker'] = tracker_status
                    if heartbeat:
                        response['info']['heartbeat'] = heartbeat
                    _merge_tracker_payload(response, tracker_status)
                    if tracker_message and not str(_as_record(response.get('info')).get('status') or '').strip():
                        response['info']['status'] = tracker_message
                        response['info']['message'] = tracker_message
                    logger.debug('Task %s is in state: %s.', task_id, task_result.state)

        return response

    @app.route('/status/<task_id>', methods=['GET'])
    @require_api_token
    def get_task_status(task_id):
        logger.debug('Received status request for task ID: %s', task_id)
        return jsonify(build_task_status_response(task_id))

    @app.route('/status/batch', methods=['POST'])
    @require_api_token
    def get_task_status_batch():
        payload = request.get_json(silent=True) or {}
        task_ids_input = payload.get('task_ids')
        task_ids = []
        if isinstance(task_ids_input, list):
            task_ids = [str(item or '').strip() for item in task_ids_input if str(item or '').strip()]
        unique_task_ids = list(dict.fromkeys(task_ids))
        if not unique_task_ids:
            return jsonify({'statuses': []}), 200
        limit = min(len(unique_task_ids), 2000)
        trimmed_task_ids = unique_task_ids[:limit]
        logger.debug('Received batch status request for %s task IDs.', len(trimmed_task_ids))

        # Per-task degradation contract: a task whose status read fails comes
        # back as state='UNKNOWN' instead of failing the whole batch — callers
        # treat unknown states like the pre-existing non-terminal buckets.
        def _status_or_error(task_id: str) -> Dict[str, Any]:
            try:
                return build_task_status_response(task_id)
            except Exception:
                logger.exception('Batch status failed for task %s.', task_id)
                return {'task_id': task_id, 'state': 'UNKNOWN', 'info': {}}

        def _compact(status: Dict[str, Any]) -> Dict[str, Any]:
            """Compact a batch status entry for the polling hot path.

            NOTE: the frontend's getTaskStatus(single) also routes through
            this endpoint as a length-1 batch, so compacting here applies to
            every frontend status read. That is safe today — candidate rows
            reach the UI from result-bundle hydration, the polling merge
            strips candidate rows, and RUNNING tasks carry their candidates
            in progress.* (untouched here) — but any future consumer that
            needs the FULL candidate list must call this endpoint with the
            compaction disabled or read the result bundle. A 24-row peptide
            candidate list is ~35KB of the ~41KB per-task payload; the batch
            keeps 1 row for opportunistic previews. lead_opt_metrics is a
            byte-identical duplicate of compact_metrics — readers fall back
            to compact_metrics, so the duplicate is dropped here too.
            """
            info = status.get('info')
            if not isinstance(info, dict):
                return status
            compact_info = dict(info)
            compact_info.pop('lead_opt_metrics', None)
            design = info.get('peptide_design')
            if isinstance(design, dict) and isinstance(design.get('best_sequences'), list):
                compact = dict(design)
                compact['best_sequences'] = design['best_sequences'][:1]
                compact_info['peptide_design'] = compact
            return {**status, 'info': compact_info}

        # Each status read is I/O-bound (archive zip stat/reads, redis, tracker).
        # The task list polls batches of 32-256 IDs, so fan the reads out
        # instead of serializing them; failures degrade to a per-task error
        # entry instead of failing the whole batch.
        if len(trimmed_task_ids) == 1:
            statuses = [_compact(_status_or_error(trimmed_task_ids[0]))]
        else:
            with ThreadPoolExecutor(max_workers=min(16, len(trimmed_task_ids))) as pool:
                statuses = [_compact(s) for s in pool.map(_status_or_error, trimmed_task_ids)]
        return jsonify({'statuses': statuses})

    @app.route('/results/<task_id>', methods=['GET'])
    @require_api_token
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
    @require_api_token
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

        preferred_structure_name = str(request.args.get('structure_name') or '').strip()
        try:
            view_path = build_or_get_view_archive(filepath, preferred_structure_name=preferred_structure_name or None)
        except Exception as exc:
            logger.warning('Failed to build view archive for task %s from %s: %s', task_id, filepath, exc)
            return jsonify({'error': f'Failed to build view archive: {exc}'}), 500

        download_name = f'{task_id}_view_results.zip'
        logger.info('Serving view result archive for task %s: %s', task_id, view_path)
        # View archives are re-requested whenever the user reopens a task or
        # revisits a candidate: honor conditional requests so an unchanged
        # view archive answers 304 instead of a full re-download. The view
        # cache keys on the source archive's stat, so its mtime changes when
        # results change — a stale client cache self-invalidates.
        return send_file(
            view_path,
            as_attachment=True,
            download_name=download_name,
            conditional=True,
            etag=True,
            max_age=60,
            mimetype='application/zip',
        )

    @app.route('/results/<task_id>/screening', methods=['GET'])
    @require_api_token
    def get_screening_results(task_id):
        logger.info('Received screening-results request for task ID: %s', task_id)
        try:
            _, filepath = resolve_result_archive_path(task_id)
        except FileNotFoundError as exc:
            task_result = AsyncResult(task_id, app=celery_app)
            logger.warning('Failed to resolve results for task %s: %s', task_id, exc)
            return jsonify({'error': str(exc), 'state': task_result.state}), 404
        except PermissionError as exc:
            logger.error('Invalid result path for task %s: %s', task_id, exc)
            return jsonify({'error': 'Invalid file path detected.'}), 400
        except Exception as exc:
            logger.exception('Unexpected error while resolving results for task %s: %s', task_id, exc)
            return jsonify({'error': f'Failed to resolve result archive: {exc}'}), 500

        try:
            with zipfile.ZipFile(filepath) as archive:
                member = next(
                    (name for name in archive.namelist() if name.lower() == 'nesso/screening.json'),
                    None,
                )
                if member is None:
                    return jsonify({
                        'error': 'Result archive does not contain nesso/screening.json; only virtual-screening tasks produce a screening ranking.',
                    }), 404
                # Cap decompressed member size: a crafted member could otherwise
                # decompress to gigabytes in memory.
                if archive.getinfo(member).file_size > SCREENING_JSON_MAX_BYTES:
                    logger.error(
                        'nesso/screening.json for task %s exceeds %d bytes.',
                        task_id,
                        SCREENING_JSON_MAX_BYTES,
                    )
                    return jsonify({'error': 'nesso/screening.json is too large to serve.'}), 500
                screening = json.loads(archive.read(member).decode('utf-8'))
        except zipfile.BadZipFile as exc:
            logger.error('Result archive for task %s is not a valid ZIP: %s', task_id, exc)
            return jsonify({'error': 'Result archive is not a valid ZIP file.'}), 500
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.error('Invalid screening JSON in results for task %s: %s', task_id, exc)
            return jsonify({'error': f'Failed to parse nesso/screening.json: {exc}'}), 500
        except Exception as exc:
            logger.exception('Unexpected error while reading screening results for task %s: %s', task_id, exc)
            return jsonify({'error': f'Failed to read screening results: {exc}'}), 500

        if not isinstance(screening, dict) or not isinstance(screening.get('compounds'), list):
            return jsonify({'error': 'nesso/screening.json must be an object with a compounds array.'}), 500

        response = jsonify(screening)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.route('/upload_result/<task_id>', methods=['POST'])
    @require_api_token
    def upload_result_from_worker(task_id):
        # Worker-authenticated only: an unauthenticated caller could forge
        # '<task_id>_results.zip' and flip a QUEUED task to SUCCESS.
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

    @app.route('/tasks/runtime_index', methods=['GET'])
    @require_api_token
    def list_task_runtime_index():
        logger.debug('Received request to list runtime task index.')
        inspector = celery_app.control.inspect(timeout=1.0)

        def collect_task_ids(payload):
            task_ids = []
            if not isinstance(payload, dict):
                return task_ids
            for worker_tasks in payload.values():
                if not isinstance(worker_tasks, list):
                    continue
                for task in worker_tasks:
                    task_id = str((task or {}).get('id') or '').strip()
                    if task_id:
                        task_ids.append(task_id)
            return list(dict.fromkeys(task_ids))

        def normalize_runtime_bucket(raw_bucket: str, status_response: Dict[str, Any]) -> str | None:
            state = str(status_response.get('state') or '').strip().upper()
            if state in {'PROGRESS', 'RUNNING', 'STARTED'}:
                return 'active'
            if state in {'PENDING', 'RECEIVED', 'RETRY', 'QUEUED'}:
                return 'reserved' if raw_bucket != 'scheduled' else 'scheduled'
            return None

        try:
            active = inspector.active() or {}
            reserved = inspector.reserved() or {}
            scheduled = inspector.scheduled() or {}
            raw_payload = {
                'active': collect_task_ids(active),
                'reserved': collect_task_ids(reserved),
                'scheduled': collect_task_ids(scheduled),
            }
            payload = {
                'active_task_ids': [],
                'reserved_task_ids': [],
                'scheduled_task_ids': [],
            }
            seen_task_ids = set()
            for raw_bucket, task_ids in raw_payload.items():
                for task_id in task_ids:
                    if task_id in seen_task_ids:
                        continue
                    seen_task_ids.add(task_id)
                    bucket = normalize_runtime_bucket(raw_bucket, build_task_status_response(task_id))
                    if bucket == 'active':
                        payload['active_task_ids'].append(task_id)
                    elif bucket == 'reserved':
                        payload['reserved_task_ids'].append(task_id)
                    elif bucket == 'scheduled':
                        payload['scheduled_task_ids'].append(task_id)
            logger.debug(
                'Runtime task index collected. Active: %s, Reserved: %s, Scheduled: %s',
                len(payload['active_task_ids']),
                len(payload['reserved_task_ids']),
                len(payload['scheduled_task_ids']),
            )
            return jsonify(payload)
        except Exception as exc:
            logger.exception('Error collecting runtime task index: %s', exc)
            return jsonify({
                'error': 'Could not collect runtime task index.',
                'details': str(exc),
            }), 500

    @app.route('/workers/capabilities', methods=['GET'])
    @app.route('/workers/cluster_status', methods=['GET'])
    @require_api_token
    def list_worker_capabilities():
        logger.info('Received worker capability snapshot request.')
        try:
            payload = get_worker_capability_snapshot()
            return jsonify(payload)
        except Exception as exc:
            logger.exception('Failed to collect worker capability snapshot: %s', exc)
            return jsonify({'error': 'Failed to collect worker capability snapshot.', 'details': str(exc)}), 500

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

    