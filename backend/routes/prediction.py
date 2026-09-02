from __future__ import annotations

import base64
import json
from typing import Any, Callable, Dict, Optional

import yaml
from flask import jsonify, request
from rdkit import Chem
from backend.runtime.nesso_backend import normalize_nesso_screening_input_yaml
from backend.runtime.screening_library import merge_screening_compounds_file_into_yaml


def _parse_prediction_properties(raw_value: Optional[str]) -> Optional[Dict[str, Any]]:
    if raw_value is None or not str(raw_value).strip():
        return None
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _normalize_prediction_property_entry(properties: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ligand = str(properties.get('ligand') or properties.get('binder') or '').strip()
    binder = str(properties.get('binder') or properties.get('ligand') or '').strip()
    if not ligand and not binder:
        return None

    entry: Dict[str, Any] = {
        'ligand': ligand or binder,
        'binder': binder or ligand,
    }
    target = str(properties.get('target') or '').strip()
    if target:
        entry['target'] = target
    return entry


def _merge_prediction_properties_into_yaml(
    yaml_content: str,
    properties: Optional[Dict[str, Any]],
) -> tuple[str, bool]:
    entry = _normalize_prediction_property_entry(properties or {})
    if not entry:
        return yaml_content, False

    data = yaml.safe_load(yaml_content) or {}
    if not isinstance(data, dict):
        return yaml_content, False

    raw_properties = data.get('properties')
    if isinstance(raw_properties, list):
        property_entries = [item for item in raw_properties if isinstance(item, dict)]
    elif isinstance(raw_properties, dict):
        property_entries = [raw_properties]
    else:
        property_entries = []

    merged = False
    for candidate in property_entries:
        if any(str(candidate.get(key) or '').strip() for key in ('ligand', 'binder', 'target')):
            candidate.update(entry)
            merged = True
            break
    if not merged:
        property_entries.insert(0, entry)

    # Boltz-2 的 schema 仅当 properties 中存在「首个 key 为 'affinity' 的独立 property」
    # (binder 指向配体链) 时才触发亲和力预测；与 ligand/binder 同级的 affinity 布尔值，
    # 因首个 key 不是 'affinity' 会被 boltz2 忽略，导致亲和力不计算、前端 affinity 面板无值。
    # AF3/Protenix 的 extract_affinity_config_from_yaml 与 _yaml_has_ligand_annotation
    # 同样以该 dict 写法为准，故将其作为 affinity 的唯一真相来源。
    if bool(properties.get('affinity')):
        ligand_chain = str(entry.get('ligand') or entry.get('binder') or '').strip()
        has_affinity_property = any(
            isinstance(prop, dict) and isinstance(prop.get('affinity'), dict)
            for prop in property_entries
        )
        if ligand_chain and not has_affinity_property:
            property_entries.insert(0, {'affinity': {'binder': ligand_chain}})

    data['properties'] = property_entries
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True), True


def _yaml_has_ligand_annotation(yaml_content: str) -> bool:
    try:
        data = yaml.safe_load(yaml_content) or {}
    except Exception:
        return False
    if not isinstance(data, dict):
        return False

    sequences = data.get('sequences')
    if isinstance(sequences, list):
        for item in sequences:
            if isinstance(item, dict) and isinstance(item.get('ligand'), dict):
                ligand_id = item['ligand'].get('id')
                if ligand_id:
                    return True

    raw_properties = data.get('properties')
    if isinstance(raw_properties, dict):
        property_entries = [raw_properties]
    elif isinstance(raw_properties, list):
        property_entries = [item for item in raw_properties if isinstance(item, dict)]
    else:
        property_entries = []
    for entry in property_entries:
        if str(entry.get('ligand') or entry.get('binder') or '').strip():
            return True
        affinity = entry.get('affinity')
        if isinstance(affinity, dict) and str(affinity.get('binder') or affinity.get('chain') or '').strip():
            return True
    return False


def _validate_yaml_ligands(yaml_content: str) -> Optional[str]:
    """Reject ligand SMILES that cannot be processed.

    Bare ions are translated to CCD before validation; remaining failures are
    unparseable SMILES or bondless multi-atom sets (disconnected salts), which
    are input errors.
    """
    data = None
    try:
        data = yaml.safe_load(yaml_content) or {}
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    sequences = data.get('sequences')
    if not isinstance(sequences, list):
        return None
    for item in sequences:
        if not (isinstance(item, dict) and isinstance(item.get('ligand'), dict)):
            continue
        smiles = str(item['ligand'].get('smiles') or '').strip()
        if not smiles:
            continue
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return f"配体 SMILES 无法解析: {smiles!r}"
        if mol.GetNumBonds() == 0 and mol.GetNumAtoms() > 1:
            return (
                f"配体 SMILES {smiles!r} 是 disconnected 组（无化学键的多个原子）。"
                "请输入完整的共价分子 SMILES。"
            )
    return None


def _parse_backbone_override(raw: Any) -> Optional[Dict[str, int]]:
    """Manual backbone atom assignment (0-based heavy-atom indices) for a custom residue.
    Returns the 5-slot dict {n,ca,c,o,oxt} or None if absent/malformed."""
    if not isinstance(raw, dict):
        return None
    parsed: Dict[str, int] = {}
    for slot in ('n', 'ca', 'c', 'o', 'oxt'):
        try:
            num = int(raw.get(slot))
        except (TypeError, ValueError):
            return None
        if num < 0:
            return None
        parsed[slot] = num
    return parsed


def _parse_custom_ccd_molecules(raw_value: Optional[str]) -> list[Dict[str, Any]]:
    if raw_value is None or not str(raw_value).strip():
        return []
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    molecules: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in parsed:
        if not isinstance(item, dict):
            continue
        ccd = str(item.get('ccd') or item.get('ccdCode') or '').strip().upper()
        smiles = str(item.get('smiles') or '').strip()
        if not ccd or not smiles or ccd in seen:
            continue
        seen.add(ccd)
        kind = str(item.get('kind') or 'residue').strip().lower()
        if kind not in {'residue', 'ligand'}:
            kind = 'residue'
        molecules.append({
            'ccd': ccd[:12],
            'smiles': smiles,
            'base_residue': str(item.get('baseResidue') or item.get('base_residue') or '').strip().upper()[:1],
            'label': str(item.get('label') or '').strip()[:80],
            'kind': kind,
            'backbone': _parse_backbone_override(item.get('backbone')),
            'cTerminalAmidated': bool(item.get('cTerminalAmidated')),
        })
    return molecules

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

        request_properties = _parse_prediction_properties(request.form.get('properties'))
        if request.form.get('properties') and request_properties is None:
            return jsonify({'error': "Invalid 'properties' form field. Expected a JSON object."}), 400
        if request_properties:
            try:
                yaml_content, properties_merged = _merge_prediction_properties_into_yaml(
                    yaml_content,
                    request_properties,
                )
                if properties_merged:
                    logger.info(
                        'Merged explicit prediction properties into yaml_file for client %s.',
                        request.remote_addr,
                    )
            except Exception as exc:
                logger.exception('Failed to merge prediction properties into YAML for %s: %s', request.remote_addr, exc)
                return jsonify({'error': f"Failed to merge 'properties' into yaml_file: {exc}"}), 400

        require_ipsae = parse_bool(request.form.get('require_ipsae'), False)
        if require_ipsae and not _yaml_has_ligand_annotation(yaml_content):
            return jsonify({
                'error': "IPSAE requested but yaml_file does not declare ligand/binder chain. Provide properties.target and properties.ligand/binder.",
            }), 400

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

        backend = str(request.form.get('backend', '')).strip().lower()
        requested_workflow = str(request.form.get('workflow', 'prediction')).strip().lower()
        if not backend:
            # Module defaults follow engine strength per workflow: prediction
            # runs on Protenix, peptide design on Protenix2Dock.
            backend = 'protenix2dock' if requested_workflow in {'peptide_design'} else 'protenix'
        if backend in {'nesso1', 'nesso-1'}:
            backend = 'nesso'
        if requested_workflow in {'peptide', 'peptide_designer', 'designer'}:
            requested_workflow = 'peptide_design'
        elif requested_workflow in {'virtual screening', 'virtual-screening', 'screening', 'vs'}:
            requested_workflow = 'virtual_screening'
        # Structure-based peptide docking engines (D-peptide capable). They
        # map onto the corresponding full predictors at the engine level but
        # carry docking semantics (target structure required; predicted first
        # via the full engine when the user did not upload one).
        # lead_optimization: the HALO oracle scores small-molecule candidates
        # with the docking engines; at the engine level these run as plain
        # complex predictions (run_single_prediction maps them onto the full
        # predictors), so only the workflow gating differs.
        if backend in {'boltz2dock', 'boltz-2-dock'}:
            if requested_workflow not in {'peptide_design', 'lead_optimization'}:
                return jsonify({'error': "Backend 'boltz2dock' is only available for the peptide_design and lead_optimization workflows."}), 400
            backend = 'boltz2dock'
        elif backend in {'protenix2dock', 'protenix-2-dock'}:
            if requested_workflow not in {'peptide_design', 'lead_optimization'}:
                return jsonify({'error': "Backend 'protenix2dock' is only available for the peptide_design and lead_optimization workflows."}), 400
            backend = 'protenix2dock'
        elif backend not in ['boltz', 'alphafold3', 'protenix', 'nesso']:
            return jsonify({'error': f"Invalid backend '{backend}'. Must be one of: boltz, alphafold3, protenix, nesso, boltz2dock, protenix2dock."}), 400
        logger.info('backend parameter received: %s for client %s.', backend, request.remote_addr)
        if requested_workflow not in {'prediction', 'peptide_design', 'virtual_screening', 'lead_optimization'}:
            return jsonify({
                'error': (
                    f"Invalid workflow '{requested_workflow}'. Must be one of: "
                    'prediction, peptide_design, virtual_screening, lead_optimization.'
                )
            }), 400
        if backend == 'nesso' and requested_workflow != 'virtual_screening':
            return jsonify({
                'error': 'Nesso is an independent Virtual Screening backend; use workflow=virtual_screening.',
                'backend': backend,
                'workflow': requested_workflow,
            }), 400
        if requested_workflow == 'virtual_screening' and backend != 'nesso':
            return jsonify({
                'error': 'Virtual Screening currently requires backend=nesso.',
                'backend': backend,
                'workflow': requested_workflow,
            }), 400

        compounds_upload = request.files.get('compounds_file')
        if compounds_upload is not None and str(compounds_upload.filename or '').strip():
            if requested_workflow != 'virtual_screening':
                return jsonify({
                    'error': 'compounds_file is only accepted for workflow=virtual_screening.',
                    'workflow': requested_workflow,
                }), 400
            try:
                compounds_text = compounds_upload.read().decode('utf-8')
            except (UnicodeDecodeError, IOError) as exc:
                logger.error("Failed to read compounds_file: %s. Client IP: %s", exc, request.remote_addr)
                return jsonify({'error': "Failed to read compounds_file. Ensure it's a valid UTF-8 text file."}), 400
            try:
                # Library files replace inline compounds at the API boundary; SMILES
                # validation and canonicalization stay inside the normalizer below.
                yaml_content = merge_screening_compounds_file_into_yaml(yaml_content, compounds_text)
            except ValueError as exc:
                return jsonify({'error': str(exc)}), 400

        ligand_error = _validate_yaml_ligands(yaml_content)
        if ligand_error:
            logger.warning("Rejected prediction submit: %s. Client IP: %s", ligand_error, request.remote_addr)
            return jsonify({'error': ligand_error}), 400

        low_vram = parse_bool(request.form.get('low_vram'), False)
        if backend == 'nesso':
            try:
                # Validate every compound at the API boundary while preserving the
                # original YAML for canonical preparation inside the worker.
                normalize_nesso_screening_input_yaml(yaml_content)
            except ValueError as exc:
                return jsonify({'error': str(exc), 'backend': backend, 'workflow': 'virtual_screening'}), 400
            if low_vram:
                return jsonify({'error': 'Nesso does not support low-VRAM mode.', 'backend': backend}), 400

        if backend in {'boltz', 'alphafold3', 'protenix', 'boltz2dock', 'protenix2dock'}:
            yaml_requires_msa = infer_use_msa_server_from_yaml_text(yaml_content)
            if yaml_requires_msa:
                msa_server_url = str(getattr(config_module, 'MSA_SERVER_URL', '') or '').strip()
                if not msa_server_url:
                    return jsonify({
                        'error': 'MSA_SERVER_URL is required for backend execution.',
                        'backend': backend,
                    }), 503
                if not use_msa_server:
                    logger.info(
                        'Set use_msa_server=True for backend=%s because the YAML requires external MSA generation (client=%s).',
                        backend,
                        request.remote_addr,
                    )
                use_msa_server = True
            else:
                if use_msa_server:
                    logger.info(
                        'Set use_msa_server=False for backend=%s because the YAML does not require external MSA generation (client=%s).',
                        backend,
                        request.remote_addr,
                    )
                use_msa_server = False
        else:
            # Nesso consumes protein sequences directly and has no MSA input contract.
            if use_msa_server:
                logger.info(
                    'Force use_msa_server=False for backend=nesso (client=%s).',
                    request.remote_addr,
                )
            use_msa_server = False

        workflow = requested_workflow

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
            design_mode_raw = (
                peptide_design_options.get('peptideDesignMode')
                or peptide_design_options.get('peptide_design_mode')
                or 'linear'
            )
            design_mode = str(design_mode_raw).strip().lower()
            if design_mode in {'cycle', 'ring'}:
                design_mode = 'cyclic'
            elif design_mode in {'bicycle', 'bi-cyclic'}:
                design_mode = 'bicyclic'
            elif design_mode != 'linear':
                design_mode = 'linear'

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
            # Parent peptide task is orchestration-only for BOTH chiralities:
            # all heavy steps (structure/conformer prediction, mirror-space
            # scoring) are dispatched as sub-steps, so the orchestrator stays
            # on the CPU queue where PeptideLM lives.
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
        if seed_value is None and backend in {'protenix', 'nesso', 'protenix2dock'}:
            seed_value = 42
            logger.info('seed parameter missing for backend=%s; defaulting to %s for client %s.', backend, seed_value, request.remote_addr)

        custom_ccd_molecules = _parse_custom_ccd_molecules(request.form.get('custom_ccd_molecules'))

        # Dry-run the exact production CCD builders so malformed custom chemistry fails
        # here (HTTP 400 naming the component) instead of inside a GPU task later.
        if custom_ccd_molecules:
            from backend.runtime.custom_ccd_builder import _build_custom_ccd_bundle
            from backend.runtime.ccd_contract import validate_ccd_additions
            try:
                ccd_bundle_text, _ = _build_custom_ccd_bundle(custom_ccd_molecules)
                validate_ccd_additions(ccd_bundle_text)
            except (ValueError, RuntimeError) as ccd_error:
                return jsonify({
                    'error': f'Custom CCD rejected: {ccd_error}',
                    'backend': backend,
                }), 400

        if backend == 'nesso' and custom_ccd_molecules:
            return jsonify({
                'error': 'Nesso does not support custom CCD residue uploads; use protein sequences and ligand SMILES or standard CCD.',
                'backend': backend,
            }), 400

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

        if backend == 'nesso' and template_inputs:
            return jsonify({
                'error': 'Nesso does not support structure template uploads.',
                'backend': backend,
            }), 400

        # Initial peptide structure for mode-anchored design (peptide_design):
        # uploaded in the same coordinate frame as the target structure; both
        # uploads together switch the D-route from generic pocket placement to
        # reference-pose anchoring.
        peptide_structure_input = None
        if workflow == 'peptide_design':
            uploaded_pep = request.files.get('peptide_structure_file')
            if uploaded_pep and uploaded_pep.filename:
                pep_meta_raw = request.form.get('peptide_structure_meta')
                pep_meta = {}
                if pep_meta_raw:
                    try:
                        parsed_pep_meta = json.loads(pep_meta_raw)
                        if isinstance(parsed_pep_meta, dict):
                            pep_meta = parsed_pep_meta
                    except json.JSONDecodeError:
                        logger.warning('Invalid peptide_structure_meta JSON; ignoring metadata.')
                pep_fmt = pep_meta.get('format')
                if not pep_fmt:
                    pep_fmt = 'pdb' if uploaded_pep.filename.lower().endswith('.pdb') else 'cif'
                peptide_structure_input = {
                    'file_name': uploaded_pep.filename,
                    'format': str(pep_fmt).lower(),
                    'chain_id': str(pep_meta.get('chain_id') or '').strip(),
                    'content_base64': base64.b64encode(uploaded_pep.read()).decode('utf-8'),
                }

        predict_args = {
            'yaml_content': yaml_content,
            'use_msa_server': use_msa_server,
            'model_name': model_name,
            'backend': backend,
            'seed': seed_value,
            'workflow': workflow,
            'low_vram': low_vram,
        }
        if workflow == 'peptide_design':
            predict_args['peptide_design_options'] = peptide_design_options
            peptide_target_chain = str(request.form.get('peptide_design_target_chain', '')).strip()
            if peptide_target_chain:
                predict_args['peptide_design_target_chain'] = peptide_target_chain
            # Candidate subtasks are dispatched as independent Celery GPU jobs.
            predict_args['peptide_subtask_queue'] = str(queue_selection.get('queue') or '').strip()
            if peptide_structure_input:
                predict_args['peptide_structure_input'] = peptide_structure_input
        if template_inputs:
            predict_args['template_inputs'] = template_inputs
        if custom_ccd_molecules:
            predict_args['custom_ccd_molecules'] = custom_ccd_molecules

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
