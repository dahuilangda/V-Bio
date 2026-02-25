from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

from flask import jsonify, request
from werkzeug.utils import secure_filename


def _parse_ligand_smiles_map(raw: Optional[str]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    if not raw:
        return mapping
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        return mapping
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        normalized_key = key.strip()
        normalized_value = value.strip()
        if normalized_key and normalized_value:
            mapping[normalized_key] = normalized_value
    return mapping


def register_affinity_routes(
    app,
    *,
    require_api_token,
    logger,
    config_module,
    affinity_task,
    boltz2score_task,
    build_affinity_preview,
    affinity_preview_error_cls,
    parse_bool: Callable[[Optional[str], bool], bool],
    parse_int: Callable[[Optional[str], Optional[int]], Optional[int]],
) -> None:
    @app.route('/api/affinity/preview', methods=['POST'])
    @require_api_token
    def preview_affinity_complex():
        logger.info('Received affinity preview request.')

        if 'protein_file' not in request.files:
            return jsonify({'error': "Request form must contain 'protein_file' part"}), 400

        protein_file = request.files['protein_file']
        ligand_file = request.files.get('ligand_file')

        if protein_file.filename == '':
            return jsonify({'error': 'protein_file must be selected'}), 400

        try:
            protein_text = protein_file.read().decode('utf-8')
        except UnicodeDecodeError:
            return jsonify({'error': 'Failed to decode protein_file as UTF-8 text.'}), 400
        except IOError as exc:
            logger.exception('Failed to read protein_file for affinity preview: %s', exc)
            return jsonify({'error': f'Failed to read protein_file: {exc}'}), 400

        ligand_text = ''
        ligand_filename = ''
        if ligand_file is not None and ligand_file.filename != '':
            try:
                ligand_file.seek(0)
                try:
                    ligand_text = ligand_file.read().decode('utf-8')
                except UnicodeDecodeError:
                    ligand_file.seek(0)
                    ligand_text = ligand_file.read().decode('utf-8', errors='replace')
                ligand_filename = secure_filename(ligand_file.filename)
            except IOError as exc:
                logger.exception('Failed to read ligand_file for affinity preview: %s', exc)
                return jsonify({'error': f'Failed to read ligand_file: {exc}'}), 400

        protein_filename = secure_filename(protein_file.filename)

        try:
            preview = build_affinity_preview(
                protein_text=protein_text,
                protein_filename=protein_filename,
                ligand_text=ligand_text,
                ligand_filename=ligand_filename,
            )
        except affinity_preview_error_cls as exc:
            return jsonify({'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('Failed to build affinity preview: %s', exc)
            return jsonify({'error': 'Failed to generate affinity preview.', 'details': str(exc)}), 500

        return jsonify(
            {
                'structure_text': preview.structure_text,
                'structure_format': preview.structure_format,
                'structure_name': preview.structure_name,
                'target_structure_text': preview.target_structure_text,
                'target_structure_format': preview.target_structure_format,
                'ligand_structure_text': preview.ligand_structure_text,
                'ligand_structure_format': preview.ligand_structure_format,
                'ligand_smiles': preview.ligand_smiles,
                'target_chain_ids': preview.target_chain_ids,
                'ligand_chain_id': preview.ligand_chain_id,
                'has_ligand': preview.has_ligand,
                'ligand_is_small_molecule': preview.ligand_is_small_molecule,
                'supports_activity': preview.supports_activity,
                'protein_filename': protein_filename,
                'ligand_filename': ligand_filename,
            }
        )

    @app.route('/api/affinity', methods=['POST'])
    @require_api_token
    def handle_affinity():
        logger.info('Received affinity prediction request.')

        if 'input_file' not in request.files:
            logger.error("Missing 'input_file' in affinity prediction request. Client IP: %s", request.remote_addr)
            return jsonify({'error': "Request form must contain a 'input_file' part"}), 400

        input_file = request.files['input_file']
        if input_file.filename == '':
            logger.error("No selected file for 'input_file' in affinity prediction request.")
            return jsonify({'error': 'No selected file for input_file'}), 400

        try:
            input_file_content = input_file.read().decode('utf-8')
            logger.debug('Input file successfully read and decoded.')
        except UnicodeDecodeError:
            logger.error('Failed to decode input_file as UTF-8. Client IP: %s', request.remote_addr)
            return jsonify({'error': "Failed to decode input_file. Ensure it's a valid UTF-8 text file."}), 400
        except IOError as exc:
            logger.exception('Failed to read input_file from request: %s. Client IP: %s', exc, request.remote_addr)
            return jsonify({'error': f'Failed to read input_file: {exc}'}), 400

        ligand_resname = request.form.get('ligand_resname', 'LIG')
        logger.info('ligand_resname parameter received: %s for client %s.', ligand_resname, request.remote_addr)

        priority = request.form.get('priority', 'default').lower()
        if priority not in ['high', 'default']:
            logger.warning("Invalid priority '%s' provided by client %s. Defaulting to 'default'.", priority, request.remote_addr)
            priority = 'default'

        target_queue = config_module.HIGH_PRIORITY_QUEUE if priority == 'high' else config_module.DEFAULT_QUEUE
        logger.info('Affinity prediction priority: %s, targeting queue: %s for client %s.', priority, target_queue, request.remote_addr)

        affinity_args: Dict[str, Any] = {
            'input_file_content': input_file_content,
            'input_filename': secure_filename(input_file.filename),
            'ligand_resname': ligand_resname,
        }

        try:
            task = affinity_task.apply_async(args=[affinity_args], queue=target_queue)
            logger.info('Affinity task %s dispatched to queue: %s.', task.id, target_queue)
        except Exception as exc:
            logger.exception('Failed to dispatch Celery task for affinity prediction request from %s: %s', request.remote_addr, exc)
            return jsonify({'error': 'Failed to dispatch affinity prediction task.', 'details': str(exc)}), 500

        return jsonify({'task_id': task.id}), 202

    @app.route('/api/affinity_separate', methods=['POST'])
    @require_api_token
    def handle_affinity_separate():
        logger.info('Received separate affinity prediction request.')

        if 'protein_file' not in request.files or 'ligand_file' not in request.files:
            logger.error('Missing required files in separate affinity prediction request. Client IP: %s', request.remote_addr)
            return jsonify({'error': "Request form must contain both 'protein_file' and 'ligand_file' parts"}), 400

        protein_file = request.files['protein_file']
        ligand_file = request.files['ligand_file']

        if protein_file.filename == '' or ligand_file.filename == '':
            logger.error('No selected files for separate affinity prediction request.')
            return jsonify({'error': 'Both protein_file and ligand_file must be selected'}), 400

        try:
            protein_file_content = protein_file.read().decode('utf-8')
            ligand_file.seek(0)
            try:
                ligand_file_content = ligand_file.read().decode('utf-8')
            except UnicodeDecodeError:
                ligand_file.seek(0)
                ligand_file_content = ligand_file.read().decode('utf-8', errors='replace')
            logger.debug('Protein and ligand files successfully read.')
        except UnicodeDecodeError:
            logger.error('Failed to decode files as UTF-8. Client IP: %s', request.remote_addr)
            return jsonify({'error': 'Failed to decode files. Ensure they are valid text files.'}), 400
        except IOError as exc:
            logger.exception('Failed to read files from request: %s. Client IP: %s', exc, request.remote_addr)
            return jsonify({'error': f'Failed to read files: {exc}'}), 400

        ligand_resname = request.form.get('ligand_resname', 'LIG')
        output_prefix = request.form.get('output_prefix', 'complex')

        priority = request.form.get('priority', 'default').lower()
        if priority not in ['high', 'default']:
            logger.warning("Invalid priority '%s' provided by client %s. Defaulting to 'default'.", priority, request.remote_addr)
            priority = 'default'

        target_queue = config_module.HIGH_PRIORITY_QUEUE if priority == 'high' else config_module.DEFAULT_QUEUE
        logger.info('Separate affinity prediction priority: %s, targeting queue: %s for client %s.', priority, target_queue, request.remote_addr)

        affinity_args = {
            'protein_file_content': protein_file_content,
            'ligand_file_content': ligand_file_content,
            'protein_filename': secure_filename(protein_file.filename),
            'ligand_filename': secure_filename(ligand_file.filename),
            'ligand_resname': ligand_resname,
            'output_prefix': output_prefix,
        }

        try:
            task = affinity_task.apply_async(args=[affinity_args], queue=target_queue)
            logger.info('Separate affinity task %s dispatched to queue: %s.', task.id, target_queue)
        except Exception as exc:
            logger.exception('Failed to dispatch Celery task for separate affinity prediction request from %s: %s', request.remote_addr, exc)
            return jsonify({'error': 'Failed to dispatch separate affinity prediction task.', 'details': str(exc)}), 500

        return jsonify({'task_id': task.id}), 202

    @app.route('/api/boltz2score', methods=['POST'])
    @require_api_token
    def handle_boltz2score():
        logger.info('Received Boltz2Score request.')

        target_chain = request.form.get('target_chain')
        ligand_chain = request.form.get('ligand_chain')
        requested_recycling_steps = parse_int(request.form.get('recycling_steps'), None)
        requested_sampling_steps = parse_int(request.form.get('sampling_steps'), None)
        requested_diffusion_samples = parse_int(request.form.get('diffusion_samples'), None)
        requested_max_parallel_samples = parse_int(request.form.get('max_parallel_samples'), None)
        requested_seed = parse_int(request.form.get('seed'), None)
        requested_structure_refine = parse_bool(request.form.get('structure_refine'), False)
        requested_use_msa_server = parse_bool(request.form.get('use_msa_server'), False)

        try:
            ligand_smiles_map = _parse_ligand_smiles_map(request.form.get('ligand_smiles_map'))
        except Exception as exc:
            logger.error('Invalid ligand_smiles_map JSON from %s: %s', request.remote_addr, exc)
            return jsonify({'error': "Invalid 'ligand_smiles_map' JSON format."}), 400

        score_args: Dict[str, Any] = {}

        if 'input_file' in request.files:
            input_file = request.files['input_file']
            if input_file.filename == '':
                logger.error("No selected file for 'input_file' in Boltz2Score request.")
                return jsonify({'error': 'No selected file for input_file'}), 400

            try:
                input_file_content = input_file.read().decode('utf-8')
            except UnicodeDecodeError:
                logger.error('Failed to decode input_file as UTF-8. Client IP: %s', request.remote_addr)
                return jsonify({'error': "Failed to decode input_file. Ensure it's a valid UTF-8 text file."}), 400
            except IOError as exc:
                logger.exception('Failed to read input_file from request: %s. Client IP: %s', exc, request.remote_addr)
                return jsonify({'error': f'Failed to read input_file: {exc}'}), 400

            score_args = {
                'input_file_content': input_file_content,
                'input_filename': secure_filename(input_file.filename),
                'target_chain': target_chain,
                'ligand_chain': ligand_chain,
            }
            if ligand_smiles_map:
                score_args['ligand_smiles_map'] = ligand_smiles_map
            score_args['affinity_refine'] = parse_bool(request.form.get('affinity_refine'), False)
            score_args['enable_affinity'] = parse_bool(request.form.get('enable_affinity'), False)
            score_args['auto_enable_affinity'] = parse_bool(request.form.get('auto_enable_affinity'), False)
        elif (
            'protein_file' in request.files
            or 'ligand_file' in request.files
            or request.form.get('ligand_smiles')
        ):
            if 'protein_file' not in request.files:
                logger.error('Missing protein_file in Boltz2Score separate-input request. Client IP: %s', request.remote_addr)
                return jsonify({'error': "Request form must contain 'protein_file'."}), 400

            protein_file = request.files['protein_file']
            ligand_smiles = (request.form.get('ligand_smiles') or '').strip()
            ligand_file = request.files.get('ligand_file')
            has_ligand_file = ligand_file is not None and ligand_file.filename != ''
            has_ligand_smiles = bool(ligand_smiles)

            if protein_file.filename == '':
                logger.error('No selected protein file for Boltz2Score separate-input request.')
                return jsonify({'error': 'protein_file must be selected'}), 400
            if not has_ligand_file and not has_ligand_smiles:
                logger.error('Missing ligand input in Boltz2Score separate-input request.')
                return jsonify({'error': "Provide either 'ligand_file' or non-empty 'ligand_smiles'."}), 400

            try:
                protein_file_content = protein_file.read().decode('utf-8')
            except UnicodeDecodeError:
                logger.error('Failed to decode protein_file as UTF-8. Client IP: %s', request.remote_addr)
                return jsonify({'error': "Failed to decode protein_file. Ensure it's a valid text file."}), 400
            except IOError as exc:
                logger.exception('Failed to read protein_file from request: %s. Client IP: %s', exc, request.remote_addr)
                return jsonify({'error': f'Failed to read protein_file: {exc}'}), 400

            output_prefix = request.form.get('output_prefix', 'complex')
            score_args = {
                'protein_file_content': protein_file_content,
                'protein_filename': secure_filename(protein_file.filename),
                'output_prefix': output_prefix,
            }

            if has_ligand_file:
                try:
                    ligand_file.seek(0)
                    try:
                        ligand_file_content = ligand_file.read().decode('utf-8')
                    except UnicodeDecodeError:
                        ligand_file.seek(0)
                        ligand_file_content = ligand_file.read().decode('utf-8', errors='replace')
                except IOError as exc:
                    logger.exception('Failed to read ligand_file from request: %s. Client IP: %s', exc, request.remote_addr)
                    return jsonify({'error': f'Failed to read ligand_file: {exc}'}), 400

                score_args.update({
                    'ligand_file_content': ligand_file_content,
                    'ligand_filename': secure_filename(ligand_file.filename),
                })
            else:
                score_args.update({
                    'ligand_smiles': ligand_smiles,
                    'ligand_filename': secure_filename(request.form.get('ligand_filename', 'ligand_from_smiles.sdf')),
                })
            if ligand_smiles_map:
                score_args['ligand_smiles_map'] = ligand_smiles_map

            score_args['affinity_refine'] = parse_bool(request.form.get('affinity_refine'), False)
            score_args['enable_affinity'] = parse_bool(request.form.get('enable_affinity'), False)
            score_args['auto_enable_affinity'] = parse_bool(request.form.get('auto_enable_affinity'), False)
            if target_chain:
                score_args['target_chain'] = target_chain
            if ligand_chain:
                score_args['ligand_chain'] = ligand_chain
        else:
            logger.error('Missing input for Boltz2Score request. Client IP: %s', request.remote_addr)
            return jsonify({
                'error': "Request form must contain 'input_file' or 'protein_file' with ('ligand_file' or 'ligand_smiles')."
            }), 400

        if requested_recycling_steps is not None:
            score_args['recycling_steps'] = requested_recycling_steps
        if requested_sampling_steps is not None:
            score_args['sampling_steps'] = requested_sampling_steps
        if requested_diffusion_samples is not None:
            score_args['diffusion_samples'] = requested_diffusion_samples
        if requested_max_parallel_samples is not None:
            score_args['max_parallel_samples'] = requested_max_parallel_samples
        if requested_seed is not None:
            score_args['seed'] = requested_seed
        score_args['structure_refine'] = requested_structure_refine
        score_args['use_msa_server'] = requested_use_msa_server

        priority = request.form.get('priority', 'default').lower()
        if priority not in ['high', 'default']:
            logger.warning("Invalid priority '%s' provided by client %s. Defaulting to 'default'.", priority, request.remote_addr)
            priority = 'default'

        target_queue = config_module.HIGH_PRIORITY_QUEUE if priority == 'high' else config_module.DEFAULT_QUEUE
        logger.info('Boltz2Score priority: %s, targeting queue: %s for client %s.', priority, target_queue, request.remote_addr)

        try:
            task = boltz2score_task.apply_async(args=[score_args], queue=target_queue)
            logger.info('Boltz2Score task %s dispatched to queue: %s.', task.id, target_queue)
            if isinstance(score_args.get('ligand_smiles_map'), dict) and score_args['ligand_smiles_map']:
                logger.info('Boltz2Score task %s received ligand_smiles_map keys: %s', task.id, sorted(score_args['ligand_smiles_map'].keys()))
        except Exception as exc:
            logger.exception('Failed to dispatch Boltz2Score task from %s: %s', request.remote_addr, exc)
            return jsonify({'error': 'Failed to dispatch Boltz2Score task.', 'details': str(exc)}), 500

        return jsonify({'task_id': task.id}), 202
