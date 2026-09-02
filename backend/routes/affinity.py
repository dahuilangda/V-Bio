from __future__ import annotations

import json
import math
import re
from typing import Any, Callable, Dict, Optional

from flask import jsonify, request
from werkzeug.utils import secure_filename

from backend.core import config

VALID_BOLTZ2SCORE_MODES = {"score", "pose", "refine", "interface", "dock"}


def _parse_dock_pocket_fields(request_form, request_files):
    """Parse and validate dock-mode pocket definition fields.

    Exactly one method must be supplied: explicit center axes (optionally
    with size axes), pocket residues, or a reference ligand file.
    Returns (dict of pocket args or None, error string or None).
    """
    # Coordinates travel into the GPU pipeline as floats; NaN/Inf parse fine but poison
    # every downstream computation silently, so finiteness and magnitude are entry gates.
    _COORD_LIMIT = 1e4

    def num(field):
        raw = (request_form.get(field) or '').strip()
        if raw == '':
            return None
        try:
            value = float(raw)
        except ValueError:
            raise ValueError(f"Field '{field}' must be numeric, got '{raw}'.")
        if not math.isfinite(value) or abs(value) > _COORD_LIMIT:
            raise ValueError(f"Field '{field}' must be a finite number within ±{_COORD_LIMIT:g}.")
        return value

    center_vals = [num('center_x'), num('center_y'), num('center_z')]
    has_any_center = any(v is not None for v in center_vals)
    has_all_center = all(v is not None for v in center_vals)
    if has_any_center and not has_all_center:
        raise ValueError("Provide all of center_x, center_y, center_z together.")

    size_vals = [num('size_x'), num('size_y'), num('size_z')]
    has_any_size = any(v is not None for v in size_vals)
    has_all_size = all(v is not None for v in size_vals)
    if has_any_size and not has_all_size:
        raise ValueError("Provide all of size_x, size_y, size_z together.")
    if has_all_size and any(v <= 0 for v in size_vals):
        raise ValueError("size_x/size_y/size_z must be positive.")

    pocket_residues = (request_form.get('pocket_residues') or '').strip()
    if pocket_residues:
        for token in pocket_residues.split(','):
            token = token.strip()
            if token and not re.match(r'^[A-Za-z]+:\d+$', token):
                raise ValueError(
                    f"Invalid residue spec {token!r} in pocket_residues. "
                    "Expected 'CHAIN:RESNUM' entries, e.g. 'A:100,A:101'."
                )
    pocket_ligand = request_files.get('pocket_ligand')
    has_pocket_ligand = (
        pocket_ligand is not None
        and hasattr(pocket_ligand, 'filename')
        and (getattr(pocket_ligand, 'filename', '') or '') != ''
    )

    methods = sum([has_all_center, bool(pocket_residues), bool(has_pocket_ligand)])
    if methods == 0:
        return None, ("dock mode requires a pocket definition: provide center_x/center_y/center_z "
                      "(with optional size_x/size_y/size_z), pocket_residues, or a pocket_ligand file.")
    if methods > 1:
        return None, "Provide exactly one pocket definition method (center coordinates, pocket_residues, or pocket_ligand)."

    pocket: Dict[str, Any] = {}
    if has_all_center:
        pocket['center_x'], pocket['center_y'], pocket['center_z'] = center_vals
    elif pocket_residues:
        pocket['pocket_residues'] = pocket_residues
    else:
        try:
            pocket_ligand_content = pocket_ligand.read().decode('utf-8')
        except UnicodeDecodeError:
            return None, "pocket_ligand is not valid UTF-8 text."
        except (IOError, OSError):
            return None, "Could not read the uploaded pocket_ligand file."
        pocket['pocket_ligand_content'] = pocket_ligand_content
        pocket['pocket_ligand_filename'] = secure_filename(pocket_ligand.filename) or 'pocket_ligand.sdf'
    if has_all_size:
        # Box sizes apply to every pocket method — the capability honors size_x/y/z with
        # residues/ligand pockets too; dropping them here would silently fall back to the
        # 7 Å radius default instead of the requested box.
        pocket['size_x'], pocket['size_y'], pocket['size_z'] = size_vals
    return pocket, None


def _parse_ligand_smiles_map(raw: Optional[str]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    if not raw:
        return mapping
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("ligand_smiles_map must be a JSON object mapping chain IDs to SMILES.")
    for key, value in parsed.items():
        if not isinstance(value, str):
            raise ValueError(f'ligand_smiles_map entry "{key}" must be a string, got {type(value).__name__}.')
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
            return jsonify({'error': 'Failed to read protein_file.', **({'details': str(exc)} if config.EXPOSE_ERROR_DETAILS else {})}), 400

        ligand_text = ''
        ligand_filename = ''
        if ligand_file is not None and ligand_file.filename != '':
            try:
                ligand_file.seek(0)
                ligand_text = ligand_file.read().decode('utf-8')
                ligand_filename = secure_filename(ligand_file.filename)
            except UnicodeDecodeError:
                return jsonify({'error': 'ligand_file is not valid UTF-8 text; provide an SDF/MOL block as UTF-8.'}), 400
            except IOError as exc:
                logger.exception('Failed to read ligand_file for affinity preview: %s', exc)
                return jsonify({'error': 'Failed to read ligand_file.', **({'details': str(exc)} if config.EXPOSE_ERROR_DETAILS else {})}), 400

        protein_filename = (secure_filename(protein_file.filename) or 'protein.pdb')

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
            return jsonify({'error': 'Failed to generate affinity preview.', **({'details': str(exc)} if config.EXPOSE_ERROR_DETAILS else {})}), 500

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

    @app.route('/api/affinity_train', methods=['POST'])
    @require_api_token
    def handle_affinity_train():
        """Dispatch a protenix2dock affinity-head training shard (long-running)."""
        logger.info('Received affinity-train request.')
        from backend.worker.affinity_train_task import affinity_train_task

        payload = request.form.to_dict() if request.form else (request.get_json(silent=True) or {})
        train_args = {
            'index_csv': str(payload.get('index_csv') or '').strip() or None,
            'val_csv': str(payload.get('val_csv') or '').strip() or None,
            'work_dir': str(payload.get('work_dir') or '').strip() or None,
            'epochs': int(payload.get('epochs') or 1),
            'rel_weight': float(payload.get('rel_weight') or 2.0),
            'msa_prob': float(payload.get('msa_prob') or 0.0),
            'lr': float(payload.get('lr') or 1e-4),
            'resume_ckpt': str(payload.get('resume_ckpt') or '').strip() or None,
            'ckpt_every': int(payload.get('ckpt_every') or 0),
            'num_blocks': int(payload.get('num_blocks') or 2),
            'dropout': float(payload.get('dropout') or 0.1),
            'timeout_seconds': int(payload.get('timeout_seconds') or 0),
        }
        priority = (payload.get('priority') or 'default').lower()
        if priority not in ('high', 'default'):
            priority = 'default'

        queue_selection = select_queue_for_capability('protenix2dock', priority)
        if not bool(queue_selection.get('online', False)):
            return jsonify({
                'error': 'No online workers for affinity training.',
                'queue_selection': queue_selection,
            }), 503
        target_queue = str(queue_selection.get('queue') or '').strip()
        try:
            task = affinity_train_task.apply_async(args=[train_args], queue=target_queue)
            logger.info('affinity-train task %s dispatched to %s.', task.id, target_queue)
        except Exception as exc:
            logger.exception('Failed to dispatch affinity-train task: %s', exc)
            return jsonify({'error': 'Failed to dispatch affinity-train task.'}), 500
        return jsonify({
            'task_id': task.id,
            'queue': target_queue,
            'capability': 'protenix2dock_affinity_train',
            'queue_selection': queue_selection,
        }), 202

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
        requested_mode = str(request.form.get('mode') or 'dock').strip().lower()
        if requested_mode not in VALID_BOLTZ2SCORE_MODES:
            return jsonify({'error': f"Unsupported mode '{requested_mode}'."}), 400
        requested_compute_interactions = parse_bool(request.form.get('compute_interactions'), True)

        dock_pocket = None
        if requested_mode == 'dock':
            try:
                dock_pocket, pocket_error = _parse_dock_pocket_fields(request.form, request.files)
            except ValueError as exc:
                return jsonify({'error': str(exc)}), 400
            if pocket_error:
                return jsonify({'error': pocket_error}), 400

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
            return jsonify({'error': "Invalid 'ligand_smiles_map' format."}), 400

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
                return jsonify({'error': 'Failed to read input_file.', **({'details': str(exc)} if config.EXPOSE_ERROR_DETAILS else {})}), 400

            if requested_mode != 'score':
                return jsonify({
                    'error': (
                        f"Mode '{requested_mode}' requires separate inputs: upload 'protein_file' "
                        "plus a ligand ('ligand_file' or 'ligand_smiles')."
                    )
                }), 400

            score_args = {
                'input_file_content': input_file_content,
                'input_filename': (secure_filename(input_file.filename) or 'input.cif'),
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
            if requested_mode not in ('score', 'dock') and not has_ligand_file:
                return jsonify({'error': f"Mode '{requested_mode}' requires an uploaded 'ligand_file'."}), 400

            try:
                protein_file_content = protein_file.read().decode('utf-8')
            except UnicodeDecodeError:
                logger.error('Failed to decode protein_file as UTF-8. Client IP: %s', request.remote_addr)
                return jsonify({'error': "Failed to decode protein_file. Ensure it's a valid text file."}), 400
            except IOError as exc:
                logger.exception('Failed to read protein_file from request: %s. Client IP: %s', exc, request.remote_addr)
                return jsonify({'error': 'Failed to read protein_file.', **({'details': str(exc)} if config.EXPOSE_ERROR_DETAILS else {})}), 400

            score_args = {
                'protein_file_content': protein_file_content,
                'protein_filename': (secure_filename(protein_file.filename) or 'protein.pdb'),
                'mode': requested_mode,
                'compute_ipsae': requested_compute_ipsae,
                'target_chain': target_chain,
                'ligand_chain': ligand_chain,
                'affinity_refine': parse_bool(request.form.get('affinity_refine'), False),
                'enable_affinity': parse_bool(request.form.get('enable_affinity'), False),
            }

            if requested_mode == 'dock' and not has_ligand_smiles:
                return jsonify({'error': "dock mode requires a ligand defined by SMILES (drawn or pasted)."}), 400
            if requested_mode == 'dock' and has_ligand_file:
                # The dock pipeline builds the ligand from SMILES; a staged ligand_file would be
                # silently discarded downstream and the job would fail after GPU allocation.
                return jsonify({'error': "dock mode takes the ligand as SMILES only; remove ligand_file."}), 400

            if has_ligand_file:
                try:
                    ligand_file.seek(0)
                    ligand_file_content = ligand_file.read().decode('utf-8')
                except UnicodeDecodeError:
                    return jsonify({'error': 'ligand_file is not valid UTF-8 text; provide an SDF/MOL block as UTF-8.'}), 400
                except IOError as exc:
                    logger.exception('Failed to read ligand_file from request: %s. Client IP: %s', exc, request.remote_addr)
                    return jsonify({'error': 'Failed to read ligand_file.', **({'details': str(exc)} if config.EXPOSE_ERROR_DETAILS else {})}), 400

                score_args.update({
                    'ligand_file_content': ligand_file_content,
                    'ligand_filename': (secure_filename(ligand_file.filename) or 'ligand.sdf'),
                })
            else:
                score_args.update({
                    'ligand_smiles': ligand_smiles,
                    'ligand_filename': secure_filename(request.form.get('ligand_filename', 'ligand_from_smiles.sdf')),
                })

            if dock_pocket:
                score_args.update(dock_pocket)
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
        score_args['compute_interactions'] = requested_compute_interactions

        priority = request.form.get('priority', 'default').lower()
        if priority not in ['high', 'default']:
            logger.warning("Invalid priority '%s' provided by client %s. Defaulting to 'default'.", priority, request.remote_addr)
            priority = 'default'

        # backend=protenix routes to the protenix2dock engine (same five-mode
        # semantics on the Protenix runtime); anything else stays boltz2score.
        requested_backend = (request.form.get('backend') or 'boltz').strip().lower()
        if requested_backend in ('protenix', 'protenix2dock', 'p2d'):
            from backend.worker.protenix2dock_task import protenix2dock_task

            # Surface options this engine genuinely ignores so a caller
            # cannot silently believe they took effect. sampling_steps and
            # diffusion_samples ARE forwarded to the CLI — never listed here.
            ignored_fields = [
                key for key, enabled in (
                    ('enable_affinity', parse_bool(request.form.get('enable_affinity'), False)),
                    ('affinity_refine', parse_bool(request.form.get('affinity_refine'), False)),
                    ('recycling_steps', requested_recycling_steps is not None),
                    ('max_parallel_samples', requested_max_parallel_samples is not None),
                    ('structure_refine', parse_bool(request.form.get('structure_refine'), False)),
                    ('compute_interactions', parse_bool(request.form.get('compute_interactions'), True)),
                    ('compute_ipsae', parse_bool(request.form.get('compute_ipsae'), True)),
                    ('ligand_chain', bool((request.form.get('ligand_chain') or '').strip())),
                    ('ligand_smiles_map', bool((request.form.get('ligand_smiles_map') or '').strip())),
                ) if enabled
            ]
            if ignored_fields:
                logger.warning(
                    'protenix2dock ignores boltz-only option(s) %s from %s.',
                    ', '.join(ignored_fields), request.remote_addr,
                )

            queue_selection = select_queue_for_capability('protenix2dock', priority)
            if not bool(queue_selection.get('online', False)):
                return jsonify({
                    'error': 'No online workers available for requested capability.',
                    'capability': 'protenix2dock',
                    'queue_selection': queue_selection,
                }), 503
            target_queue = str(queue_selection.get('queue') or '').strip()
            if not target_queue:
                return jsonify({'error': 'Resolved queue is empty for protenix2dock.', 'queue_selection': queue_selection}), 500
            try:
                task = protenix2dock_task.apply_async(args=[score_args], queue=target_queue)
                logger.info('protenix2dock task %s dispatched to queue: %s (mode=%s).', task.id, target_queue, requested_mode)
            except Exception as exc:
                logger.exception('Failed to dispatch protenix2dock task from %s: %s', request.remote_addr, exc)
                return jsonify({'error': 'Failed to dispatch protenix2dock task.', **({'details': str(exc)} if config.EXPOSE_ERROR_DETAILS else {})}), 500
            return jsonify({
                'task_id': task.id,
                'queue': target_queue,
                'capability': 'protenix2dock',
                'queue_selection': queue_selection,
                **({'ignored_fields': ignored_fields} if ignored_fields else {}),
            }), 202

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
            return jsonify({'error': 'Failed to dispatch Boltz2Score task.', **({'details': str(exc)} if config.EXPOSE_ERROR_DETAILS else {})}), 500

        response_payload = {
            'task_id': task.id,
            'queue': target_queue,
            'capability': 'boltz2score',
            'queue_selection': queue_selection,
        }
        return jsonify(response_payload), 202
