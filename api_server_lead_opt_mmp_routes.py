from __future__ import annotations

import base64
import tempfile
from pathlib import Path

from typing import Any, Callable, Dict, List, Optional

from celery.result import AsyncResult
from flask import jsonify, request
from lead_optimization.mmp_database_registry import (
    delete_mmp_database,
    get_mmp_database_catalog,
    patch_mmp_database,
    resolve_mmp_database,
)

try:
    import gemmi
except Exception:
    gemmi = None


def _normalize_mmp_catalog_status(raw: Any) -> str:
    token = str(raw or "").strip().lower()
    if token in {"ready", "building", "failed"}:
        return token
    return "building"


def _decorate_mmp_catalog_status(catalog: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(catalog or {}) if isinstance(catalog, dict) else {}
    rows: List[Dict[str, Any]] = []
    for item in payload.get("databases", []) if isinstance(payload.get("databases"), list) else []:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row["status"] = _normalize_mmp_catalog_status(row.get("status"))
        rows.append(row)
    payload["databases"] = rows
    return payload


def _filter_ready_mmp_catalog(catalog: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(catalog or {}) if isinstance(catalog, dict) else {}
    rows = []
    for item in payload.get("databases", []) if isinstance(payload.get("databases"), list) else []:
        if not isinstance(item, dict):
            continue
        status = _normalize_mmp_catalog_status(item.get("status"))
        if status == "ready":
            rows.append(dict(item))
    payload["databases"] = rows
    default_id = str(payload.get("default_database_id") or "").strip()
    if default_id and not any(str(item.get("id") or "").strip() == default_id for item in rows):
        payload["default_database_id"] = str(rows[0].get("id") or "").strip() if rows else ""
    payload["total"] = len(rows)
    payload["total_visible"] = len(rows)
    payload["total_all"] = len(rows)
    return payload


def _build_chain_residue_index_map(
    structure_text: str,
    structure_format: str,
) -> tuple[Dict[tuple[str, int], int], Dict[str, int], Dict[str, str]]:
    if gemmi is None:
        return {}, {}, {}
    text = str(structure_text or "").strip()
    fmt = str(structure_format or "cif").strip().lower()
    if not text:
        return {}, {}, {}
    suffix = ".pdb" if fmt == "pdb" else ".cif"
    residue_index_map: Dict[tuple[str, int], int] = {}
    chain_lengths: Dict[str, int] = {}
    chain_sequences: Dict[str, str] = {}
    aa3_to1 = {
        "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C", "GLN": "Q", "GLU": "E", "GLY": "G",
        "HIS": "H", "ILE": "I", "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P", "SER": "S",
        "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V", "SEC": "U", "PYL": "O",
    }
    try:
        with tempfile.TemporaryDirectory(prefix="leadopt_template_") as temp_dir:
            temp_path = Path(temp_dir) / f"template{suffix}"
            temp_path.write_text(text, encoding="utf-8")
            structure = gemmi.read_structure(str(temp_path))
            structure.setup_entities()
            if len(structure) == 0:
                return {}, {}, {}
            model = structure[0]
            for chain in model:
                chain_id = str(chain.name or "").strip()
                if not chain_id:
                    continue
                ordinal = 0
                seen_residues: set[tuple[int, str]] = set()
                sequence_chars: List[str] = []
                for residue in chain:
                    if residue.het_flag != "A":
                        continue
                    residue_key = (int(residue.seqid.num), str(residue.seqid.icode or "").strip())
                    if residue_key in seen_residues:
                        continue
                    seen_residues.add(residue_key)
                    ordinal += 1
                    base_key = (chain_id, residue_key[0])
                    if base_key not in residue_index_map:
                        residue_index_map[base_key] = ordinal
                    residue_name = str(residue.name or "").strip().upper()
                    sequence_chars.append(aa3_to1.get(residue_name, "X"))
                if ordinal > 0:
                    chain_lengths[chain_id] = ordinal
                    chain_sequences[chain_id] = "".join(sequence_chars)
    except Exception:
        return {}, {}, {}
    return residue_index_map, chain_lengths, chain_sequences


def _remap_pocket_residues_to_sequence_index(
    pocket_residues: List[Dict[str, Any]],
    residue_index_map: Dict[tuple[str, int], int],
    chain_lengths: Dict[str, int],
) -> tuple[List[Dict[str, Any]], int, int]:
    if not pocket_residues:
        return [], 0, 0
    remapped = 0
    dropped = 0
    output: List[Dict[str, Any]] = []
    for row in pocket_residues:
        if not isinstance(row, dict):
            continue
        chain_id = str(row.get("chain_id") or "").strip()
        residue_number_raw = row.get("residue_number")
        try:
            residue_number = int(residue_number_raw)
        except Exception:
            residue_number = 0
        if not chain_id or residue_number <= 0:
            continue
        mapped = residue_index_map.get((chain_id, residue_number))
        next_row = dict(row)
        if mapped is not None:
            if mapped != residue_number:
                remapped += 1
            next_row["residue_number"] = mapped
            output.append(next_row)
            continue
        chain_len = int(chain_lengths.get(chain_id) or 0)
        if chain_len > 0 and not (1 <= residue_number <= chain_len):
            dropped += 1
            continue
        output.append(next_row)
    return output, remapped, dropped


def _select_target_chain_for_template(
    requested_target_chain: str,
    chain_sequences: Dict[str, str],
    pocket_residues: List[Dict[str, Any]],
) -> str:
    requested = str(requested_target_chain or "").strip()
    if requested and requested in chain_sequences:
        return requested
    counts: Dict[str, int] = {}
    for row in pocket_residues:
        if not isinstance(row, dict):
            continue
        chain_id = str(row.get("chain_id") or "").strip()
        if not chain_id or chain_id not in chain_sequences:
            continue
        counts[chain_id] = int(counts.get(chain_id) or 0) + 1
    if counts:
        return max(counts.items(), key=lambda item: item[1])[0]
    if chain_sequences:
        return next(iter(chain_sequences.keys()))
    return requested or "A"


def _normalize_aggregation_type(raw: Any, *, query_mode: str) -> str:
    token = str(raw or "").strip().lower()
    if token in {"individual_transforms", "group_by_fragment"}:
        return token
    return "group_by_fragment" if str(query_mode or "").strip().lower() == "many-to-many" else "individual_transforms"


def _normalize_boolean(raw: Any, default: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    token = str(raw or "").strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def register_lead_opt_mmp_routes(
    app,
    *,
    require_api_token,
    logger,
    config_module,
    celery_app,
    predict_task,
    lead_optimization_mmp_query_task,
    parse_bool: Callable[[Optional[str], bool], bool],
    parse_int: Callable[[Optional[str], Optional[int]], Optional[int]],
    load_progress: Callable[[str], Optional[Dict[str, Any]]],
    has_worker_for_queue: Callable[[str], bool],
    run_mmp_query_service: Callable[[Dict[str, Any]], Dict[str, Any]],
    materialize_mmp_query_result_cache: Callable[..., Dict[str, Any]],
    get_cached_mmp_query_id_for_task: Callable[[str], str],
    get_cached_mmp_query_payload: Callable[[str], Optional[Dict[str, Any]]],
    get_cached_mmp_evidence_payload: Callable[[str], Optional[Dict[str, Any]]],
    build_mmp_clusters: Callable[..., List[Dict[str, Any]]],
    safe_json_object: Callable[[Any], Dict[str, Any]],
    compute_smiles_properties: Callable[[str], Dict[str, float]],
    passes_property_constraints_simple: Callable[[Dict[str, float], Dict[str, Any]], bool],
    build_lead_opt_prediction_yaml: Callable[..., str],
    download_results: Callable[[str], Any],
) -> None:
    @app.route('/api/lead_optimization/mmp_databases', methods=['GET'])
    @require_api_token
    def lead_optimization_mmp_databases():
        try:
            catalog = get_mmp_database_catalog(include_hidden=False, include_stats=False)
            catalog = _decorate_mmp_catalog_status(catalog)
            catalog = _filter_ready_mmp_catalog(catalog)
            return jsonify(catalog)
        except Exception as exc:
            logger.exception('Failed to list lead optimization MMP databases: %s', exc)
            return jsonify({'error': f'Failed to list MMP databases: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_databases', methods=['GET'])
    @require_api_token
    def admin_list_lead_optimization_mmp_databases():
        try:
            catalog = get_mmp_database_catalog(include_hidden=True, include_stats=True)
            catalog = _decorate_mmp_catalog_status(catalog)
            return jsonify(catalog)
        except Exception as exc:
            logger.exception('Admin failed to list lead optimization MMP databases: %s', exc)
            return jsonify({'error': f'Failed to list MMP databases: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_databases', methods=['POST'])
    @require_api_token
    def admin_update_lead_optimization_mmp_databases():
        return (
            jsonify(
                {
                    'error': (
                        "MMP database creation/registration via API is disabled. "
                        "Use lead_optimization.mmp_lifecycle to import/build databases."
                    )
                }
            ),
            405,
        )

    @app.route('/api/admin/lead_optimization/mmp_databases/<database_id>', methods=['PATCH'])
    @require_api_token
    def admin_patch_lead_optimization_mmp_database(database_id: str):
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({'error': 'patch payload must be a JSON object.'}), 400
        try:
            catalog = patch_mmp_database(
                database_id,
                visible=payload.get('visible') if 'visible' in payload else None,
                label=payload.get('label') if 'label' in payload else None,
                description=payload.get('description') if 'description' in payload else None,
                is_default=payload.get('is_default') if 'is_default' in payload else None,
                include_stats=True,
            )
            return jsonify(catalog)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('Admin failed to patch MMP database %s: %s', database_id, exc)
            return jsonify({'error': f'Failed to patch MMP database: {exc}'}), 500

    @app.route('/api/admin/lead_optimization/mmp_databases/<database_id>', methods=['DELETE'])
    @require_api_token
    def admin_delete_lead_optimization_mmp_database(database_id: str):
        drop_data = parse_bool(request.args.get('drop_data'), True)
        try:
            catalog = delete_mmp_database(database_id, drop_data=drop_data, include_stats=True)
            return jsonify(catalog)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('Admin failed to delete MMP database %s: %s', database_id, exc)
            return jsonify({'error': f'Failed to delete MMP database: {exc}'}), 500

    @app.route('/api/lead_optimization/mmp_query', methods=['POST'])
    @require_api_token
    def lead_optimization_mmp_query():
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({'error': 'mmp_query payload must be a JSON object.'}), 400
        request_payload = dict(payload)
        selected_database_id = str(request_payload.get('mmp_database_id') or '').strip()
        try:
            selected_database = resolve_mmp_database(selected_database_id, include_hidden=False)
            runtime_database = str(selected_database.get('runtime_database') or '').strip()
            if runtime_database:
                request_payload['mmp_database_runtime'] = runtime_database
            selected_schema = str(selected_database.get('schema') or '').strip()
            if selected_schema:
                request_payload['mmp_database_schema'] = selected_schema
            selected_label = str(selected_database.get('label') or '').strip()
            if selected_label:
                request_payload['mmp_database_label'] = selected_label
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        except Exception as exc:
            logger.exception('Failed to resolve selected MMP database before query: %s', exc)
            return jsonify({'error': f'Failed to resolve selected MMP database: {exc}'}), 500

        async_mode = parse_bool(str(request_payload.get('async') or 'true'), True)
        if async_mode:
            worker_ready = has_worker_for_queue(config_module.CPU_QUEUE)
            if not worker_ready:
                logger.warning(
                    "No worker detected for queue '%s' at enqueue time; task will be queued and executed when workers are available.",
                    config_module.CPU_QUEUE,
                )
            task = lead_optimization_mmp_query_task.apply_async(args=[request_payload], queue=config_module.CPU_QUEUE)
            response_payload = {
                'task_id': task.id,
                'state': 'QUEUED',
                'queue': config_module.CPU_QUEUE,
            }
            if not worker_ready:
                response_payload['warning'] = (
                    f"Queue '{config_module.CPU_QUEUE}' currently has no visible workers; "
                    "task is queued and will run once workers are up."
                )
            return jsonify(response_payload), 202

        try:
            result_payload = run_mmp_query_service(request_payload)
            response = materialize_mmp_query_result_cache(result_payload)
            return jsonify(response)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        except RuntimeError as exc:
            logger.warning('MMP query failed (runtime): %s', exc)
            message = str(exc or '').strip() or 'mmp_query failed'
            status_code = 400 if ('attachment' in message.lower() or 'query' in message.lower()) else 500
            return jsonify({'error': f'mmp_query failed: {message}'}), status_code
        except Exception as exc:
            logger.warning('MMP query failed: %s', exc)
            return jsonify({'error': f'mmp_query failed: {exc}'}), 500

    @app.route('/api/lead_optimization/mmp_query_status/<task_id>', methods=['GET'])
    @require_api_token
    def lead_optimization_mmp_query_status(task_id: str):
        task_result = AsyncResult(task_id, app=celery_app)
        state = str(task_result.state or 'PENDING')
        if state in {'PENDING', 'RECEIVED', 'STARTED', 'RETRY', 'PROGRESS'}:
            progress = load_progress(f'lead_optimization:mmp_query:progress:{task_id}') or {}
            return jsonify({
                'task_id': task_id,
                'state': state,
                'progress': progress,
            })
        if state == 'FAILURE':
            info = task_result.info
            return jsonify({
                'task_id': task_id,
                'state': state,
                'error': str(info),
            }), 500
        if state != 'SUCCESS':
            return jsonify({
                'task_id': task_id,
                'state': state,
            })

        cached_query_id = get_cached_mmp_query_id_for_task(task_id)
        cached_payload = get_cached_mmp_query_payload(cached_query_id) if cached_query_id else None
        if cached_payload:
            payload = cached_payload
            query_mode = str(payload.get('query_mode') or 'one-to-many')
            aggregation_type = _normalize_aggregation_type(payload.get('aggregation_type'), query_mode=query_mode)
            grouped_by_environment = _normalize_boolean(payload.get('grouped_by_environment'), False)
            return jsonify({
                'task_id': task_id,
                'state': 'SUCCESS',
                'result': {
                    'query_id': cached_query_id,
                    'query_mode': query_mode,
                    'aggregation_type': aggregation_type,
                    'grouped_by_environment': grouped_by_environment,
                    'mmp_database_id': payload.get('mmp_database_id', ''),
                    'mmp_database_label': payload.get('mmp_database_label', ''),
                    'mmp_database_schema': payload.get('mmp_database_schema', ''),
                    'variable_spec': payload.get('variable_spec', {}),
                    'constant_spec': payload.get('constant_spec', {}),
                    'rule_env_radius': payload.get('rule_env_radius'),
                    'min_pairs': int(payload.get('min_pairs', 1) or 1),
                    'transforms': payload.get('transforms', []),
                    'global_transforms': payload.get('global_transforms', []),
                    'clusters': payload.get('clusters', []),
                    'count': len(payload.get('transforms', [])),
                    'global_count': len(payload.get('global_transforms', [])),
                    'stats': {
                        'global_rows': int(payload.get('global_rows_count', 0) or 0),
                        'environment_rows': int(payload.get('environment_rows_count', 0) or 0),
                    },
                },
            })

        raw_result = task_result.result
        if not isinstance(raw_result, dict):
            return jsonify({
                'task_id': task_id,
                'state': 'SUCCESS',
                'result': {},
            })
        response_payload = materialize_mmp_query_result_cache(raw_result, task_id=task_id)
        return jsonify({
            'task_id': task_id,
            'state': 'SUCCESS',
            'result': response_payload,
        })

    @app.route('/api/lead_optimization/mmp_cluster', methods=['POST'])
    @require_api_token
    def lead_optimization_mmp_cluster():
        payload = request.get_json(silent=True) or {}
        query_id = str(payload.get('query_id') or '').strip()
        if not query_id:
            return jsonify({'error': "'query_id' is required."}), 400
        query_payload = get_cached_mmp_query_payload(query_id)
        if not query_payload:
            return jsonify({'error': 'query_id not found or expired.'}), 404

        group_by = str(payload.get('group_by') or 'to').strip().lower()
        if group_by not in {'to', 'from', 'rule_env_radius'}:
            group_by = 'to'
        min_pairs = max(1, int(payload.get('min_pairs') or 1))
        direction = str(query_payload.get('direction') or 'increase')
        query_mode = str(query_payload.get('query_mode') or 'one-to-many')
        aggregation_type = _normalize_aggregation_type(query_payload.get('aggregation_type'), query_mode=query_mode)
        grouped_by_environment = _normalize_boolean(query_payload.get('grouped_by_environment'), False)
        clusters = build_mmp_clusters(
            query_payload.get('transforms', []),
            group_by=group_by,
            min_pairs=min_pairs,
            direction=direction,
        )
        return jsonify({
            'query_id': query_id,
            'group_by': group_by,
            'aggregation_type': aggregation_type,
            'grouped_by_environment': grouped_by_environment,
            'min_pairs': min_pairs,
            'clusters': clusters,
        })

    @app.route('/api/lead_optimization/mmp_query_result/<query_id>', methods=['GET'])
    @require_api_token
    def lead_optimization_mmp_query_result(query_id: str):
        query_key = str(query_id or '').strip()
        if not query_key:
            return jsonify({'error': 'query_id is required.'}), 400
        query_payload = get_cached_mmp_query_payload(query_key)
        if not query_payload:
            return jsonify({'error': 'query_id not found or expired.'}), 404
        transforms = query_payload.get('transforms', []) if isinstance(query_payload.get('transforms'), list) else []
        global_transforms = (
            query_payload.get('global_transforms', [])
            if isinstance(query_payload.get('global_transforms'), list)
            else transforms
        )
        clusters = query_payload.get('clusters', []) if isinstance(query_payload.get('clusters'), list) else []
        query_mode = str(query_payload.get('query_mode') or 'one-to-many')
        aggregation_type = _normalize_aggregation_type(query_payload.get('aggregation_type'), query_mode=query_mode)
        grouped_by_environment = _normalize_boolean(query_payload.get('grouped_by_environment'), False)
        return jsonify({
            'query_id': query_key,
            'query_mode': query_mode,
            'aggregation_type': aggregation_type,
            'grouped_by_environment': grouped_by_environment,
            'mmp_database_id': str(query_payload.get('mmp_database_id') or ''),
            'mmp_database_label': str(query_payload.get('mmp_database_label') or ''),
            'mmp_database_schema': str(query_payload.get('mmp_database_schema') or ''),
            'variable_spec': query_payload.get('variable_spec', {}),
            'constant_spec': query_payload.get('constant_spec', {}),
            'rule_env_radius': query_payload.get('rule_env_radius'),
            'min_pairs': int(query_payload.get('min_pairs', 1) or 1),
            'transforms': transforms,
            'global_transforms': global_transforms,
            'clusters': clusters,
            'count': len(transforms),
            'global_count': len(global_transforms),
            'stats': {
                'global_rows': int(query_payload.get('global_rows_count', 0) or 0),
                'environment_rows': int(query_payload.get('environment_rows_count', 0) or 0),
            },
        })

    @app.route('/api/lead_optimization/mmp_evidence/<transform_id>', methods=['GET'])
    @require_api_token
    def lead_optimization_mmp_evidence(transform_id: str):
        transform_key = str(transform_id or '').strip()
        if not transform_key:
            return jsonify({'error': 'transform_id is required.'}), 400
        payload = get_cached_mmp_evidence_payload(transform_key)
        if not payload:
            return jsonify({'error': 'transform_id not found or expired.'}), 404
        return jsonify({
            'transform_id': transform_key,
            'transform': payload.get('transform', {}),
            'pairs': payload.get('rows', []),
            'n_pairs': len(payload.get('rows', [])),
        })

    @app.route('/api/lead_optimization/mmp_enumerate', methods=['POST'])
    @require_api_token
    def lead_optimization_mmp_enumerate():
        payload = request.get_json(silent=True) or {}
        query_id = str(payload.get('query_id') or '').strip()
        transform_ids = payload.get('transform_ids') if isinstance(payload.get('transform_ids'), list) else []
        cluster_ids = payload.get('cluster_ids') if isinstance(payload.get('cluster_ids'), list) else []
        property_constraints = safe_json_object(payload.get('property_constraints'))
        max_candidates = min(1000, max(1, int(payload.get('max_candidates') or 200)))

        query_payload = get_cached_mmp_query_payload(query_id) if query_id else None
        if query_id and not query_payload:
            return jsonify({'error': 'query_id not found or expired.'}), 404

        allowed_transform_ids: set[str] = set()
        for item in transform_ids:
            token = str(item or '').strip()
            if token:
                allowed_transform_ids.add(token)
        if cluster_ids and query_payload:
            selected_cluster_ids = {str(cid) for cid in cluster_ids}
            clusters = query_payload.get('clusters', [])
            for cluster in clusters:
                if str(cluster.get('cluster_id')) not in selected_cluster_ids:
                    continue
                for transform_id in cluster.get('transform_ids', []):
                    token = str(transform_id or '').strip()
                    if token:
                        allowed_transform_ids.add(token)

        rows: List[Dict[str, Any]] = []
        transform_cluster_map: Dict[str, Dict[str, Any]] = {}
        if query_payload:
            source_rows = query_payload.get('rows', [])
            rows = [
                row
                for row in source_rows
                if not allowed_transform_ids or str(row.get('transform_id') or '') in allowed_transform_ids
            ]
            clusters_payload = query_payload.get('clusters', [])
            if isinstance(clusters_payload, list):
                for cluster in clusters_payload:
                    if not isinstance(cluster, dict):
                        continue
                    cluster_id = str(cluster.get('cluster_id') or '').strip()
                    group_key = str(cluster.get('group_key') or '').strip()
                    for transform_id in cluster.get('transform_ids', []) if isinstance(cluster.get('transform_ids'), list) else []:
                        token = str(transform_id or '').strip()
                        if not token:
                            continue
                        if token not in transform_cluster_map:
                            transform_cluster_map[token] = {
                                'cluster_id': cluster_id,
                                'group_key': group_key,
                            }

        candidates: List[Dict[str, Any]] = []
        seen_candidate_keys: set[str] = set()
        reference_props = compute_smiles_properties(str(query_payload.get('query_mol') or '').strip()) if query_payload else {}

        def _delta(key: str, props: Dict[str, float]) -> Optional[float]:
            base = reference_props.get(key)
            value = props.get(key)
            if base is None or value is None:
                return None
            return float(value - base)

        for row in rows:
            smiles = str(row.get('final_smiles') or '').strip()
            if not smiles:
                continue
            highlight_indices_raw = row.get('final_highlight_atom_indices', [])
            highlight_indices = (
                sorted(
                    {
                        int(value)
                        for value in highlight_indices_raw
                        if isinstance(value, (int, float)) and int(value) >= 0
                    }
                )
                if isinstance(highlight_indices_raw, list)
                else []
            )
            dedupe_key = f"{smiles}||{','.join(str(idx) for idx in highlight_indices)}"
            if dedupe_key in seen_candidate_keys:
                continue
            seen_candidate_keys.add(dedupe_key)
            props = compute_smiles_properties(smiles)
            selected_property_key = str(row.get('selected_property') or '').strip()
            selected_property_value = row.get('selected_property_value')
            input_property_value = row.get('input_property_value')
            selected_property_delta: Optional[float] = None
            if selected_property_key:
                try:
                    selected_numeric = float(selected_property_value) if selected_property_value is not None else None
                except Exception:
                    selected_numeric = None
                if selected_numeric is not None:
                    props[selected_property_key] = selected_numeric
                    try:
                        input_numeric = float(input_property_value) if input_property_value is not None else None
                    except Exception:
                        input_numeric = None
                    if input_numeric is not None:
                        selected_property_delta = float(selected_numeric - input_numeric)
            transform_id = str(row.get('transform_id') or '').strip()
            cluster_meta = transform_cluster_map.get(transform_id, {})
            candidates.append({
                'smiles': smiles,
                'transform_id': transform_id,
                'from_smiles': row.get('from_smiles', ''),
                'to_smiles': row.get('to_smiles', ''),
                'to_highlight_smiles': row.get('to_highlight_smiles', ''),
                'selected_fragment_id': row.get('selected_fragment_id', ''),
                'query_variable_smiles': row.get('query_variable_smiles', ''),
                'final_highlight_atom_indices': highlight_indices,
                'n_pairs': row.get('n_pairs', 1),
                'median_delta': row.get('median_delta', 0.0),
                'selected_property': selected_property_key,
                'selected_property_value': selected_property_value,
                'input_property_value': input_property_value,
                'cluster_id': cluster_meta.get('cluster_id', ''),
                'group_key': cluster_meta.get('group_key', ''),
                'properties': props,
                'property_deltas': {
                    'mw': _delta('molecular_weight', props),
                    'logp': _delta('logp', props),
                    'tpsa': _delta('tpsa', props),
                    'selected': selected_property_delta,
                },
                'passes_constraints': passes_property_constraints_simple(props, property_constraints),
            })
            if len(candidates) >= max_candidates:
                break

        filtered = [item for item in candidates if item['passes_constraints']]
        return jsonify({
            'query_id': query_id,
            'requested_transform_ids': sorted(allowed_transform_ids),
            'candidate_count': len(filtered),
            'candidates': filtered,
        })

    @app.route('/api/lead_optimization/predict_candidate', methods=['POST'])
    @require_api_token
    def lead_optimization_predict_candidate():
        payload = request.get_json(silent=True) or {}
        candidate_smiles = str(payload.get("candidate_smiles") or "").strip()
        protein_sequence = str(payload.get("protein_sequence") or "").strip()
        backend = str(payload.get("backend") or "boltz").strip().lower()
        target_chain = str(payload.get("target_chain") or "A").strip() or "A"
        ligand_chain = str(payload.get("ligand_chain") or "L").strip() or "L"
        resolved_target_chain = target_chain
        pocket_residues = payload.get("pocket_residues") if isinstance(payload.get("pocket_residues"), list) else []
        reference_template_structure_text = str(payload.get("reference_template_structure_text") or "").strip()
        reference_template_structure_format = str(payload.get("reference_template_structure_format") or "cif").strip().lower()
        if reference_template_structure_format not in {"cif", "pdb"}:
            reference_template_structure_format = "cif"
        seed_value = payload.get("seed")
        use_msa_raw = payload.get("use_msa_server", None)
        if use_msa_raw is None:
            use_msa_server = True
        elif isinstance(use_msa_raw, bool):
            use_msa_server = use_msa_raw
        else:
            use_msa_server = parse_bool(str(use_msa_raw), True)
        priority = str(payload.get("priority") or "high").strip().lower()

        if backend not in ["boltz", "alphafold3", "protenix"]:
            backend = "boltz"
        if not candidate_smiles:
            return jsonify({"error": "'candidate_smiles' is required."}), 400

        has_reference_template = bool(reference_template_structure_text)
        if not protein_sequence and not has_reference_template:
            return jsonify({"error": "'protein_sequence' is required."}), 400

        normalized_protein_sequence = str(protein_sequence or "").replace("\n", "").replace(" ", "").strip()
        normalized_pocket_residues = pocket_residues

        if has_reference_template:
            residue_index_map, chain_lengths, chain_sequences = _build_chain_residue_index_map(
                reference_template_structure_text,
                reference_template_structure_format,
            )
            if chain_sequences:
                resolved_target_chain = _select_target_chain_for_template(
                    target_chain,
                    chain_sequences,
                    pocket_residues if isinstance(pocket_residues, list) else [],
                )
                if resolved_target_chain != target_chain:
                    logger.info(
                        "Lead-opt prediction target chain adjusted from %s to %s using template+pocket context.",
                        target_chain,
                        resolved_target_chain,
                    )
                template_chain_sequence = str(chain_sequences.get(resolved_target_chain) or "").strip()
                if template_chain_sequence:
                    if normalized_protein_sequence and normalized_protein_sequence != template_chain_sequence:
                        logger.info(
                            "Lead-opt prediction overrides provided protein sequence with template-derived sequence for chain %s.",
                            resolved_target_chain,
                        )
                    normalized_protein_sequence = template_chain_sequence
            if pocket_residues:
                normalized_pocket_residues, remapped_count, dropped_count = _remap_pocket_residues_to_sequence_index(
                    pocket_residues,
                    residue_index_map,
                    chain_lengths,
                )
                if remapped_count > 0 or dropped_count > 0:
                    logger.info(
                        "Lead-opt pocket constraints normalized against template sequence: remapped=%s dropped=%s kept=%s",
                        remapped_count,
                        dropped_count,
                        len(normalized_pocket_residues),
                    )

        if normalized_protein_sequence and normalized_pocket_residues:
            sequence_len = len(normalized_protein_sequence)
            valid_rows: List[Dict[str, Any]] = []
            invalid_examples: List[str] = []
            for row in normalized_pocket_residues:
                if not isinstance(row, dict):
                    continue
                chain_id = str(row.get("chain_id") or "").strip() or resolved_target_chain
                residue_number_raw = row.get("residue_number")
                try:
                    residue_number = int(residue_number_raw)
                except Exception:
                    residue_number = 0
                if chain_id != resolved_target_chain:
                    invalid_examples.append(f"{chain_id}:{residue_number}")
                    continue
                if not (1 <= residue_number <= sequence_len):
                    invalid_examples.append(f"{chain_id}:{residue_number}")
                    continue
                next_row = dict(row)
                next_row["chain_id"] = chain_id
                next_row["residue_number"] = residue_number
                valid_rows.append(next_row)
            normalized_pocket_residues = valid_rows
            if invalid_examples:
                preview = ", ".join(invalid_examples[:6])
                return jsonify(
                    {
                        "error": (
                            "Pocket residues are inconsistent with the resolved target chain/sequence. "
                            f"target_chain={resolved_target_chain}, sequence_len={sequence_len}, invalid={preview}"
                        )
                    }
                ), 400
        if pocket_residues and not normalized_pocket_residues:
            return jsonify({"error": "No valid pocket residues available after template mapping."}), 400

        ligand_chain = ligand_chain or "L"
        if ligand_chain == resolved_target_chain:
            return jsonify(
                {
                    "error": (
                        "target_chain and ligand_chain resolved to the same id. "
                        "Please provide distinct chain ids for target and ligand."
                    )
                }
            ), 400

        if backend == "protenix" and not normalized_protein_sequence:
            return jsonify({"error": "Protenix backend requires 'protein_sequence'."}), 400

        try:
            prediction_yaml = build_lead_opt_prediction_yaml(
                protein_sequence=normalized_protein_sequence,
                candidate_smiles=candidate_smiles,
                target_chain=resolved_target_chain,
                ligand_chain=ligand_chain,
                backend=backend,
                pocket_residues=normalized_pocket_residues,
            )
        except Exception as exc:
            return jsonify({"error": f"Failed to construct prediction yaml: {exc}"}), 400

        predict_args = {
            "yaml_content": prediction_yaml,
            "use_msa_server": use_msa_server,
            "backend": backend,
            "seed": parse_int(str(seed_value), None) if seed_value is not None else None,
        }
        if has_reference_template and backend in {"boltz", "alphafold3"}:
            template_file_name = f"leadopt_reference_template.{reference_template_structure_format}"
            predict_args["template_inputs"] = [
                {
                    "content_base64": base64.b64encode(reference_template_structure_text.encode("utf-8")).decode("ascii"),
                    "format": reference_template_structure_format,
                    "file_name": template_file_name,
                    "template_chain_id": resolved_target_chain,
                    "target_chain_ids": [resolved_target_chain],
                }
            ]
        target_queue = config_module.HIGH_PRIORITY_QUEUE if priority == "high" else config_module.DEFAULT_QUEUE
        try:
            task = predict_task.apply_async(args=[predict_args], queue=target_queue)
        except Exception as exc:
            logger.exception("Failed to dispatch lead optimization candidate prediction: %s", exc)
            return jsonify({"error": f"Failed to dispatch prediction task: {exc}"}), 500

        applied_constraint_type = "none"
        if normalized_pocket_residues:
            if backend in {"boltz", "protenix"}:
                applied_constraint_type = "pocket"

        return jsonify(
            {
                "task_id": task.id,
                "backend": backend,
                "target_chain": resolved_target_chain,
                "ligand_chain": ligand_chain,
                "applied_constraint_type": applied_constraint_type,
                "pocket_residue_count": len(normalized_pocket_residues),
            }
        ), 202

    @app.route('/api/lead_optimization/status/<task_id>', methods=['GET'])
    @require_api_token
    def get_lead_optimization_status(task_id: str):
        task_result = AsyncResult(task_id, app=celery_app)
        progress = load_progress(f'lead_optimization:progress:{task_id}')

        response = {
            'task_id': task_id,
            'state': task_result.state,
            'progress': progress or {},
        }

        if task_result.state == 'FAILURE':
            info = task_result.info
            response['error'] = str(info)

        return jsonify(response)

    @app.route('/api/lead_optimization/results/<task_id>', methods=['GET'])
    @require_api_token
    def download_lead_optimization_results(task_id: str):
        return download_results(task_id)
