"""HALO generative lead-optimization API.

Three optimization modes over one engine: de novo, fragment replacement,
scaffold hopping. Candidate scoring runs on V-Bio's own prediction engines —
protenix2dock (default), boltz2dock, alphafold3 — via the native prediction
oracle.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Callable

from flask import jsonify, request

MODES = ("denovo", "fragment", "scaffold_hop")


def _predict_oracle_meta() -> dict:
    """Import the halo module lazily: the API process must not depend on
    import-order side effects for capabilities/ to be on sys.path."""
    from halo.oracle.predict_oracle import PredictOracle

    return {
        "supported": PredictOracle.SUPPORTED_BACKENDS,
        "default": PredictOracle.DEFAULT_BACKEND,
    }


def register_lead_opt_halo_routes(
    app,
    require_api_token: Callable,
    logger: logging.Logger,
    celery_app,
    lead_optimization_halo_task,
    select_queue_for_capability: Callable,
    has_worker_for_queue: Callable,
    load_progress: Callable,
):
    @app.route('/api/lead_optimization/halo_backends', methods=['GET'])
    @require_api_token
    def halo_backends():
        meta = _predict_oracle_meta()
        return jsonify({
            'backends': [
                {'id': b, 'label': b, 'default': b == meta['default']}
                for b in meta['supported']
            ],
            'default': meta['default'],
            'modes': list(MODES),
        })

    @app.route('/api/lead_optimization/halo_optimize', methods=['POST'])
    @require_api_token
    def halo_optimize():
        try:
            payload = request.get_json(silent=True) or {}
        except Exception:
            return jsonify({'error': 'Invalid JSON body.'}), 400

        mode = str(payload.get('mode') or 'fragment').strip().lower()
        if mode not in MODES:
            return jsonify({'error': f"mode must be one of {', '.join(MODES)}."}), 400

        meta = _predict_oracle_meta()
        backend = str(payload.get('backend') or meta['default']).strip().lower()
        if backend not in meta['supported']:
            return jsonify({
                'error': f"backend must be one of {', '.join(meta['supported'])}."
            }), 400

        protein_upload = payload.get('protein_upload')
        protein_path = str(payload.get('protein_path') or '').strip()
        has_protein = (
            isinstance(protein_upload, dict)
            and str(protein_upload.get('content_base64') or '').strip()
        ) or protein_path
        if not has_protein:
            return jsonify({'error': 'protein structure (protein_upload or protein_path) is required.'}), 400
        if isinstance(protein_upload, dict) and protein_upload.get('content_base64'):
            try:
                base64.b64decode(protein_upload['content_base64'], validate=True)
            except Exception:
                return jsonify({'error': 'protein_upload.content_base64 is not valid base64.'}), 400

        reference_smiles = str(payload.get('reference_smiles') or '').strip()
        keep_fragment = str(payload.get('keep_fragment_smiles') or '').strip()
        pocket = str(payload.get('pocket') or '').strip()
        if mode in ('fragment', 'scaffold_hop') and not (reference_smiles or keep_fragment):
            return jsonify({'error': f"{mode} mode requires reference_smiles or keep_fragment_smiles."}), 400
        if mode == 'denovo' and not (pocket or reference_smiles or keep_fragment):
            return jsonify({'error': "denovo mode requires pocket ('x,y,z') or a reference for placement."}), 400
        if pocket:
            parts = pocket.split(',')
            if len(parts) != 3 or not all(_is_float(part) for part in parts):
                return jsonify({'error': "pocket must be 'x,y,z'."}), 400

        try:
            rounds = int(payload.get('rounds') or 6)
            budget = int(payload.get('budget_per_round') or 48)
        except (TypeError, ValueError):
            return jsonify({'error': 'rounds and budget_per_round must be integers.'}), 400
        if not 1 <= rounds <= 100:
            return jsonify({'error': 'rounds must be within 1-100.'}), 400
        if not 1 <= budget <= 512:
            return jsonify({'error': 'budget_per_round must be within 1-512.'}), 400

        queue_info = select_queue_for_capability('lead_opt', priority=str(payload.get('priority') or 'default'))
        if not queue_info.get('queue'):
            return jsonify({
                'error': 'No CPU worker is online for the lead_opt capability.',
                'detail': queue_info,
            }), 503

        task_args = {
            'mode': mode,
            'backend': backend,
            'reference_smiles': reference_smiles,
            'keep_fragment_smiles': keep_fragment,
            'edit_atom_indices': str(payload.get('edit_atom_indices') or ''),
            'pocket': pocket,
            'rounds': rounds,
            'budget_per_round': budget,
            'oracle_concurrency': payload.get('oracle_concurrency', 8),
            'oracle_timeout_s': payload.get('oracle_timeout_s', 7200),
            'seed': payload.get('seed', 0),
            'prior_dir': str(payload.get('prior_dir') or ''),
            'target_name': str(payload.get('target_name') or ''),
            'target_chain': str(payload.get('target_chain') or 'A'),
            'priority': str(payload.get('priority') or 'default'),
        }
        if isinstance(protein_upload, dict) and protein_upload.get('content_base64'):
            task_args['protein_upload'] = {
                'content_base64': protein_upload['content_base64'],
                'file_name': str(protein_upload.get('file_name') or 'target.pdb'),
            }
        elif protein_path:
            task_args['protein_path'] = protein_path
        reference_upload = payload.get('reference_upload')
        if isinstance(reference_upload, dict) and reference_upload.get('content_base64'):
            try:
                base64.b64decode(reference_upload['content_base64'], validate=True)
            except Exception:
                return jsonify({'error': 'reference_upload.content_base64 is not valid base64.'}), 400
            task_args['reference_sdf_upload'] = {
                'content_base64': reference_upload['content_base64'],
                'file_name': str(reference_upload.get('file_name') or 'reference.sdf'),
            }

        try:
            hop_ratio = float(payload.get('scaffold_hop_ratio') or 0.4)
        except (TypeError, ValueError):
            return jsonify({'error': 'scaffold_hop_ratio must be a number.'}), 400
        if not 0.0 <= hop_ratio <= 1.0:
            return jsonify({'error': 'scaffold_hop_ratio must be within 0-1.'}), 400
        task_args['scaffold_hop_ratio'] = hop_ratio
        if payload.get('seed') is not None:
            try:
                task_args['seed'] = int(payload['seed'])
            except (TypeError, ValueError):
                return jsonify({'error': 'seed must be an integer.'}), 400

        result = lead_optimization_halo_task.apply_async(args=[task_args], queue=queue_info['queue'])
        logger.info('HALO optimize task %s submitted (mode=%s backend=%s queue=%s)', result.id, mode, backend, queue_info['queue'])
        return jsonify({'task_id': result.id, 'mode': mode, 'backend': backend, 'queue': queue_info['queue']}), 202

    @app.route('/api/lead_optimization/halo_status/<task_id>', methods=['GET'])
    @require_api_token
    def halo_status(task_id: str):
        normalized = str(task_id or '').strip()
        if not normalized:
            return jsonify({'error': 'task_id is required.'}), 400
        progress = load_progress(f"task_status:{normalized}")
        state = 'UNKNOWN'
        info: dict = {}
        if isinstance(progress, dict):
            state = str(progress.get('status') or '').upper()
            info = progress if isinstance(progress, dict) else {}
        try:
            async_result = celery_app.AsyncResult(normalized)
            celery_state = str(async_result.state or '').upper()
        except Exception:
            celery_state = ''
        # The worker writes 'failed' to the progress tracker and then raises
        # Ignore(), so Celery's durable terminal state is IGNORED, not FAILURE.
        terminal = ('SUCCESS', 'FAILURE', 'REVOKED', 'IGNORED')
        if state not in terminal and celery_state in terminal:
            state = celery_state
        if state == 'IGNORED':
            state = 'FAILURE'
        payload = {
            'task_id': normalized,
            'state': state,
            'status': state,
            'info': info,
        }
        if state == 'SUCCESS' and isinstance(async_result.result, dict):
            payload['result'] = async_result.result
        return jsonify(payload)


def _is_float(value) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False
