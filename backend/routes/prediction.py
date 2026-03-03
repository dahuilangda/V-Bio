from __future__ import annotations

import base64
import json
from typing import Any, Callable, Dict, Optional

from flask import jsonify, request


def register_prediction_routes(
    app,
    *,
    require_api_token,
    logger,
    config_module,
    predict_task,
    parse_bool: Callable[[Optional[str], bool], bool],
    parse_int: Callable[[Optional[str], Optional[int]], Optional[int]],
    infer_use_msa_server_from_yaml_text: Callable[[str], bool],
    extract_template_meta_from_yaml: Callable[[str], Dict[str, Dict]],
    normalize_chain_id_list: Callable[[Any], list[str]],
    select_queue_for_capability: Callable[[str, str], Dict[str, Any]],
    capability_from_prediction_backend: Callable[[str], str],
) -> None:
    @app.route('/predict', methods=['POST'])
    @require_api_token
    def handle_predict():
        logger.info('Received prediction request.')

        if 'yaml_file' not in request.files:
            logger.error("Missing 'yaml_file' in prediction request. Client IP: %s", request.remote_addr)
            return jsonify({'error': "Request form must contain a 'yaml_file' part"}), 400

        yaml_file = request.files['yaml_file']
        if yaml_file.filename == '':
            logger.error("No selected file for 'yaml_file' in prediction request.")
            return jsonify({'error': 'No selected file for yaml_file'}), 400

        try:
            yaml_content = yaml_file.read().decode('utf-8')
        except UnicodeDecodeError:
            logger.error('Failed to decode yaml_file as UTF-8. Client IP: %s', request.remote_addr)
            return jsonify({'error': "Failed to decode yaml_file. Ensure it's a valid UTF-8 text file."}), 400
        except IOError as exc:
            logger.exception('Failed to read yaml_file from request: %s. Client IP: %s', exc, request.remote_addr)
            return jsonify({'error': f'Failed to read yaml_file: {exc}'}), 400

        use_msa_server_raw = request.form.get('use_msa_server')
        if use_msa_server_raw is None or not str(use_msa_server_raw).strip():
            use_msa_server = infer_use_msa_server_from_yaml_text(yaml_content)
            logger.info(
                'use_msa_server missing in form; inferred as %s from YAML for client %s.',
                use_msa_server,
                request.remote_addr,
            )
        else:
            use_msa_server = parse_bool(use_msa_server_raw, False)
            logger.info('use_msa_server parameter received: %s for client %s.', use_msa_server, request.remote_addr)

        model_name = request.form.get('model', None)
        if model_name:
            logger.info('model parameter received: %s for client %s.', model_name, request.remote_addr)

        backend_raw = request.form.get('backend', 'boltz')
        backend = str(backend_raw).strip().lower()
        if backend not in ['boltz', 'alphafold3', 'protenix']:
            logger.warning("Invalid backend '%s' provided by client %s. Defaulting to 'boltz'.", backend, request.remote_addr)
            backend = 'boltz'
        logger.info('backend parameter received: %s for client %s.', backend, request.remote_addr)

        if backend in {'boltz', 'alphafold3', 'protenix'}:
            msa_server_url = str(getattr(config_module, 'MSA_SERVER_URL', '') or '').strip()
            if not msa_server_url:
                return jsonify({
                    'error': 'MSA_SERVER_URL is required for backend execution.',
                    'backend': backend,
                }), 503
            if not use_msa_server:
                logger.info(
                    'Force use_msa_server=True for backend=%s (client=%s).',
                    backend,
                    request.remote_addr,
                )
            use_msa_server = True

        workflow_raw = request.form.get('workflow', 'prediction')
        workflow = str(workflow_raw).strip().lower()
        if workflow in {'peptide', 'peptide_designer', 'designer'}:
            workflow = 'peptide_design'
        if workflow not in {'prediction', 'peptide_design'}:
            logger.warning("Invalid workflow '%s' provided by client %s. Defaulting to 'prediction'.", workflow, request.remote_addr)
            workflow = 'prediction'

        peptide_design_options = {}
        if workflow == 'peptide_design':
            peptide_opts_raw = request.form.get('peptide_design_options')
            if peptide_opts_raw:
                try:
                    parsed = json.loads(peptide_opts_raw)
                    if isinstance(parsed, dict):
                        peptide_design_options = parsed
                except json.JSONDecodeError:
                    logger.warning('Invalid peptide_design_options JSON provided; ignoring design options.')

        priority = request.form.get('priority', 'default').lower()
        if priority not in ['high', 'default']:
            logger.warning("Invalid priority '%s' provided by client %s. Defaulting to 'default'.", priority, request.remote_addr)
            priority = 'default'
        requested_capability = capability_from_prediction_backend(backend)
        queue_selection = select_queue_for_capability(requested_capability, priority)
        if not bool(queue_selection.get('online', False)):
            return jsonify({
                'error': 'No online workers available for requested capability.',
                'capability': requested_capability,
                'queue_selection': queue_selection,
            }), 503
        target_queue = str(queue_selection.get('queue') or '').strip()
        if not target_queue:
            return jsonify({'error': 'Resolved queue is empty for requested capability.', 'queue_selection': queue_selection}), 500
        parent_queue_selection = None
        if workflow == 'peptide_design':
            # Parent peptide task is orchestration-only; keep GPU workers free for candidate subtasks.
            parent_queue_selection = select_queue_for_capability('peptide_design', 'default')
            if not bool(parent_queue_selection.get('online', False)):
                return jsonify({
                    'error': 'No online CPU orchestration workers available for peptide workflow.',
                    'capability': 'peptide_design',
                    'queue_selection': parent_queue_selection,
                }), 503
            target_queue = str(parent_queue_selection.get('queue') or '').strip()
            if not target_queue:
                return jsonify({'error': 'Resolved queue is empty for peptide parent workflow.', 'queue_selection': parent_queue_selection}), 500
        logger.info(
            'Prediction priority: %s, capability: %s, targeting queue: %s for client %s.',
            priority,
            requested_capability,
            target_queue,
            request.remote_addr,
        )

        seed_value = parse_int(request.form.get('seed'), None)
        if seed_value is None and backend == 'protenix':
            seed_value = 42
            logger.info('seed parameter missing for backend=protenix; defaulting to %s for client %s.', seed_value, request.remote_addr)

        template_inputs = []
        template_meta_raw = request.form.get('template_meta')
        template_meta = []
        if template_meta_raw:
            try:
                template_meta = json.loads(template_meta_raw)
            except json.JSONDecodeError:
                logger.warning('Invalid template_meta JSON provided; ignoring template metadata.')
        yaml_template_meta_map = extract_template_meta_from_yaml(yaml_content)

        meta_map = {
            entry.get('file_name'): entry
            for entry in template_meta
            if isinstance(entry, dict) and entry.get('file_name')
        }

        template_files = request.files.getlist('template_files')
        for uploaded in template_files:
            if not uploaded or not uploaded.filename:
                continue
            filename = uploaded.filename
            content_bytes = uploaded.read()
            meta = meta_map.get(filename) or yaml_template_meta_map.get(filename, {})
            fmt = meta.get('format')
            if not fmt:
                lower_name = filename.lower()
                fmt = 'pdb' if lower_name.endswith('.pdb') else 'cif'
            target_chain_ids = normalize_chain_id_list(meta.get('target_chain_ids') or meta.get('chain_id'))
            template_inputs.append({
                'file_name': filename,
                'format': fmt,
                'template_chain_id': meta.get('template_chain_id'),
                'target_chain_ids': target_chain_ids,
                'content_base64': base64.b64encode(content_bytes).decode('utf-8'),
            })

        predict_args = {
            'yaml_content': yaml_content,
            'use_msa_server': use_msa_server,
            'model_name': model_name,
            'backend': backend,
            'seed': seed_value,
            'workflow': workflow,
        }
        if workflow == 'peptide_design':
            predict_args['peptide_design_options'] = peptide_design_options
            peptide_target_chain = str(request.form.get('peptide_design_target_chain', '')).strip()
            if peptide_target_chain:
                predict_args['peptide_design_target_chain'] = peptide_target_chain
            # Candidate subtasks are dispatched as independent Celery GPU jobs.
            predict_args['peptide_subtask_queue'] = str(queue_selection.get('queue') or '').strip()
        if template_inputs:
            predict_args['template_inputs'] = template_inputs

        try:
            task = predict_task.apply_async(args=[predict_args], queue=target_queue)
            logger.info(
                'Task %s dispatched to queue: %s with use_msa_server=%s, backend=%s.',
                task.id,
                target_queue,
                use_msa_server,
                backend,
            )
        except Exception as exc:
            logger.exception('Failed to dispatch Celery task for prediction request from %s: %s', request.remote_addr, exc)
            return jsonify({'error': 'Failed to dispatch prediction task.', 'details': str(exc)}), 500
        response_payload = {
            'task_id': task.id,
            'queue': target_queue,
            'capability': requested_capability,
            'queue_selection': queue_selection,
        }
        if isinstance(parent_queue_selection, dict):
            response_payload['parent_queue_selection'] = parent_queue_selection
        return jsonify(response_payload), 202
