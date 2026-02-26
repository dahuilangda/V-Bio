from __future__ import annotations

import base64
import json
from typing import Any, Callable, Dict, Optional

from celery.result import AsyncResult
from flask import jsonify, request
from werkzeug.utils import secure_filename


def register_prediction_routes(
    app,
    *,
    require_api_token,
    logger,
    config_module,
    celery_app,
    predict_task,
    virtual_screening_task,
    parse_bool: Callable[[Optional[str], bool], bool],
    parse_int: Callable[[Optional[str], Optional[int]], Optional[int]],
    parse_float: Callable[[Optional[str], Optional[float]], Optional[float]],
    infer_use_msa_server_from_yaml_text: Callable[[str], bool],
    extract_template_meta_from_yaml: Callable[[str], Dict[str, Dict]],
    normalize_chain_id_list: Callable[[Any], list[str]],
    load_progress: Callable[[str], Optional[Dict[str, Any]]],
    download_results: Callable[[str], Any],
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

        target_queue = config_module.HIGH_PRIORITY_QUEUE if priority == 'high' else config_module.DEFAULT_QUEUE
        logger.info('Prediction priority: %s, targeting queue: %s for client %s.', priority, target_queue, request.remote_addr)

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

        return jsonify({'task_id': task.id}), 202

    @app.route('/api/virtual_screening/submit', methods=['POST'])
    @require_api_token
    def submit_virtual_screening():
        logger.info('Received virtual screening submission request.')

        if 'target_file' not in request.files or 'library_file' not in request.files:
            return jsonify({'error': "Request must include 'target_file' and 'library_file'."}), 400

        target_file = request.files['target_file']
        library_file = request.files['library_file']

        if target_file.filename == '' or library_file.filename == '':
            return jsonify({'error': 'Target file or library file is empty.'}), 400

        try:
            target_content = target_file.read().decode('utf-8')
        except UnicodeDecodeError:
            return jsonify({'error': 'Failed to decode target_file as UTF-8.'}), 400

        library_bytes = library_file.read()
        library_base64 = base64.b64encode(library_bytes).decode('ascii')

        options = {
            'library_type': request.form.get('library_type'),
            'max_molecules': parse_int(request.form.get('max_molecules'), None),
            'batch_size': parse_int(request.form.get('batch_size'), None),
            'max_workers': parse_int(request.form.get('max_workers'), None),
            'timeout': parse_int(request.form.get('timeout'), None),
            'retry_attempts': parse_int(request.form.get('retry_attempts'), None),
            'use_msa_server': parse_bool(request.form.get('use_msa_server'), False),
            'binding_affinity_weight': parse_float(request.form.get('binding_affinity_weight'), None),
            'structural_stability_weight': parse_float(request.form.get('structural_stability_weight'), None),
            'confidence_weight': parse_float(request.form.get('confidence_weight'), None),
            'min_binding_score': parse_float(request.form.get('min_binding_score'), None),
            'top_n': parse_int(request.form.get('top_n'), None),
            'report_only': parse_bool(request.form.get('report_only'), False),
            'auto_enable_affinity': parse_bool(request.form.get('auto_enable_affinity'), False),
            'enable_affinity': parse_bool(request.form.get('enable_affinity'), False),
            'log_level': request.form.get('log_level'),
            'force': parse_bool(request.form.get('force'), False),
            'dry_run': parse_bool(request.form.get('dry_run'), False),
            'task_timeout': parse_int(request.form.get('task_timeout'), None),
        }

        screening_args = {
            'target_filename': secure_filename(target_file.filename),
            'target_content': target_content,
            'library_filename': secure_filename(library_file.filename),
            'library_base64': library_base64,
            'options': options,
        }

        priority = request.form.get('priority', 'default').lower()
        if priority not in ['high', 'default']:
            priority = 'default'
        target_queue = config_module.HIGH_PRIORITY_QUEUE if priority == 'high' else config_module.DEFAULT_QUEUE

        try:
            task = virtual_screening_task.apply_async(args=[screening_args], queue=target_queue)
        except Exception as exc:
            logger.exception('Failed to dispatch virtual screening task: %s', exc)
            return jsonify({'error': 'Failed to dispatch virtual screening task.', 'details': str(exc)}), 500

        return jsonify({'task_id': task.id}), 202

    @app.route('/api/virtual_screening/status/<task_id>', methods=['GET'])
    @require_api_token
    def get_virtual_screening_status(task_id: str):
        task_result = AsyncResult(task_id, app=celery_app)
        progress = load_progress(f'virtual_screening:progress:{task_id}')

        response = {
            'task_id': task_id,
            'state': task_result.state,
            'progress': progress or {},
        }

        if task_result.state == 'FAILURE':
            info = task_result.info
            response['error'] = str(info)

        return jsonify(response)

    @app.route('/api/virtual_screening/results/<task_id>', methods=['GET'])
    @require_api_token
    def download_virtual_screening_results(task_id: str):
        return download_results(task_id)
