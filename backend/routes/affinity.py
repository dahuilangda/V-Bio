from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

from flask import jsonify, request
from werkzeug.utils import secure_filename

VALID_BOLTZ2SCORE_MODES = {"score", "pose", "refine", "interface"}


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
    boltz2score_task,
    build_affinity_preview,
    affinity_preview_error_cls,
    parse_bool: Callable[[Optional[str], bool], bool],
    parse_int: Callable[[Optional[str], Optional[int]], Optional[int]],
    select_queue_for_capability: Callable[[str, str], Dict[str, Any]],
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
        requested_compute_ipsae = parse_bool(request.form.get('compute_ipsae'), False)
        requested_use_msa_server = parse_bool(request.form.get('use_msa_server'), True)
        requested_mode = str(request.form.get('mode') or 'score').strip().lower()
        if requested_mode not in VALID_BOLTZ2SCORE_MODES:
            return jsonify({'error': f"Unsupported mode '{requested_mode}'."}), 400

        msa_server_url = str(getattr(config_module, 'MSA_SERVER_URL', '') or '').strip()
        if not msa_server_url:
            return jsonify({
                'error': 'MSA_SERVER_URL is required for boltz2score execution.',
                'capability': 'boltz2score',
            }), 503
        if not requested_use_msa_server:
            logger.info('Force use_msa_server=True for boltz2score request from %s.', request.remote_addr)
        requested_use_msa_server = True

        try:
            ligand_smiles_map = _parse_ligand_smiles_map(request.form.get('ligand_smiles_map'))
        except Exception as exc:
            logger.error('Invalid ligand_smiles_map JSON from %s: %s', request.remote_addr, exc)
            return jsonify({'error': "Invalid 'ligand_smiles_map' JSON format."}), 400

        score_args: Dict[str, Any]

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

            if requested_mode != 'score':
                return jsonify({
                    'error': f"Mode '{requested_mode}' requires 'protein_file' and 'ligand_file'."
                }), 400

            score_args = {
                'input_file_content': input_file_content,
                'input_filename': secure_filename(input_file.filename),
                'mode': requested_mode,
                'compute_ipsae': requested_compute_ipsae,
                'target_chain': target_chain,
                'ligand_chain': ligand_chain,
                'affinity_refine': parse_bool(request.form.get('affinity_refine'), False),
                'enable_affinity': parse_bool(request.form.get('enable_affinity'), False),
            }
            if ligand_smiles_map:
                score_args['ligand_smiles_map'] = ligand_smiles_map

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
            if requested_mode != 'score' and not has_ligand_file:
                return jsonify({'error': f"Mode '{requested_mode}' requires an uploaded 'ligand_file'."}), 400

            try:
                protein_file_content = protein_file.read().decode('utf-8')
            except UnicodeDecodeError:
                logger.error('Failed to decode protein_file as UTF-8. Client IP: %s', request.remote_addr)
                return jsonify({'error': "Failed to decode protein_file. Ensure it's a valid text file."}), 400
            except IOError as exc:
                logger.exception('Failed to read protein_file from request: %s. Client IP: %s', exc, request.remote_addr)
                return jsonify({'error': f'Failed to read protein_file: {exc}'}), 400

            score_args = {
                'protein_file_content': protein_file_content,
                'protein_filename': secure_filename(protein_file.filename),
                'mode': requested_mode,
                'compute_ipsae': requested_compute_ipsae,
                'target_chain': target_chain,
                'ligand_chain': ligand_chain,
                'affinity_refine': parse_bool(request.form.get('affinity_refine'), False),
                'enable_affinity': parse_bool(request.form.get('enable_affinity'), False),
            }

            if has_ligand_file and ligand_file is not None:
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

        queue_selection = select_queue_for_capability('boltz2score', priority)
        if not bool(queue_selection.get('online', False)):
            return jsonify({
                'error': 'No online workers available for requested capability.',
                'capability': 'boltz2score',
                'queue_selection': queue_selection,
            }), 503
        target_queue = str(queue_selection.get('queue') or '').strip()
        if not target_queue:
            return jsonify({'error': 'Resolved queue is empty for requested capability.', 'queue_selection': queue_selection}), 500
        logger.info('Boltz2Score priority: %s, mode=%s, targeting queue: %s for client %s.', priority, requested_mode, target_queue, request.remote_addr)

        try:
            task = boltz2score_task.apply_async(args=[score_args], queue=target_queue)
            logger.info('Boltz2Score task %s dispatched to queue: %s.', task.id, target_queue)
            if isinstance(score_args.get('ligand_smiles_map'), dict) and score_args['ligand_smiles_map']:
                logger.info('Boltz2Score task %s received ligand_smiles_map keys: %s', task.id, sorted(score_args['ligand_smiles_map'].keys()))
        except Exception as exc:
            logger.exception('Failed to dispatch Boltz2Score task from %s: %s', request.remote_addr, exc)
            return jsonify({'error': 'Failed to dispatch Boltz2Score task.', 'details': str(exc)}), 500

        response_payload = {
            'task_id': task.id,
            'queue': target_queue,
            'capability': 'boltz2score',
            'queue_selection': queue_selection,
        }
        return jsonify(response_payload), 202
