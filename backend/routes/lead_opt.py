from __future__ import annotations

import math
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import gemmi
from flask import jsonify, request


def register_lead_opt_routes(
    app,
    *,
    require_api_token,
    logger,
    build_affinity_preview,
    affinity_preview_error_cls,
    attachment_fragment_smiles_from_atom_indices: Callable[[Any, List[int]], str],
    decode_smiles_atom_index_from_name: Callable[[str], Optional[int]],
) -> None:
    @app.route('/api/lead_optimization/submit', methods=['POST'])
    @require_api_token
    def submit_lead_optimization():
        logger.info('Received lead optimization submission request.')
        return jsonify({
            'error': (
                'Legacy /api/lead_optimization/submit pipeline is disabled. '
                'Use Lead Optimization MMP workflow APIs instead '
                '(/fragment_preview, /reference_preview, /mmp_query, /mmp_enumerate, /predict_candidate).'
            )
        }), 410

    @app.route('/api/lead_optimization/fragment_preview', methods=['POST'])
    @require_api_token
    def lead_optimization_fragment_preview():
        payload = request.get_json(silent=True) or {}
        smiles = str(payload.get('smiles') or request.form.get('smiles') or '').strip()
        if not smiles:
            return jsonify({'error': "'smiles' is required."}), 400

        try:
            from rdkit import Chem
            from rdkit.Chem import BRICS
        except Exception as exc:
            return jsonify({'error': f'RDKit is required for fragment preview: {exc}'}), 500

        input_mol = Chem.MolFromSmiles(smiles)
        if not input_mol:
            return jsonify({'error': 'Invalid SMILES for fragment preview.'}), 400
        # Keep the original parsed graph to preserve atom-index consistency with
        # fragment atom selections and downstream variable_spec atom indices.
        mol = input_mol
        atom_bonds: List[List[int]] = []
        seen_bonds: set[tuple[int, int]] = set()
        for bond in mol.GetBonds():
            a = int(bond.GetBeginAtomIdx())
            b = int(bond.GetEndAtomIdx())
            if a == b:
                continue
            left = min(a, b)
            right = max(a, b)
            key = (left, right)
            if key in seen_bonds:
                continue
            seen_bonds.add(key)
            atom_bonds.append([left, right])

        def _normalize_attachment_smiles(query: str) -> str:
            text = str(query or '').strip()
            if not text:
                return ''
            parsed = Chem.MolFromSmiles(text)
            if parsed is None:
                parsed = Chem.MolFromSmarts(text)
            if parsed is None:
                return ''
            dummy_atoms = [atom for atom in parsed.GetAtoms() if atom.GetAtomicNum() == 0]
            if not dummy_atoms:
                return ''
            for index, atom in enumerate(sorted(dummy_atoms, key=lambda item: int(item.GetIdx())), start=1):
                atom.SetAtomMapNum(index)
                atom.SetIsotope(0)
            try:
                normalized = Chem.MolToSmiles(parsed, canonical=True)
            except Exception:
                return ''
            if not normalized or '*' not in normalized:
                return ''
            return normalized

        def _size_score(heavy_atoms: int) -> float:
            if heavy_atoms <= 0:
                return 0.0
            return max(0.0, 1.0 - min(1.0, abs(heavy_atoms - 8.0) / 12.0))

        def _attachment_score(attachment_count: int) -> float:
            if attachment_count <= 0:
                return 0.0
            if attachment_count == 1:
                return 0.75
            if attachment_count == 2:
                return 1.0
            return 0.85

        def _variable_rank(heavy_atoms: int, attachment_count: int) -> float:
            return 0.62 * _attachment_score(attachment_count) + 0.38 * _size_score(heavy_atoms)

        palette = [
            '#f39c12',
            '#3498db',
            '#16a085',
            '#e67e22',
            '#2980b9',
            '#2ecc71',
            '#d35400',
            '#1abc9c',
            '#27ae60',
            '#8e44ad',
            '#c0392b',
            '#7f8c8d',
        ]

        fragments: List[Dict[str, Any]] = []
        seen_keys: set[tuple[str, tuple[int, ...]]] = set()
        brics_bond_indices: List[int] = []
        for bond_data in BRICS.FindBRICSBonds(mol):
            atom_pair = bond_data[0]
            bond = mol.GetBondBetweenAtoms(int(atom_pair[0]), int(atom_pair[1]))
            if bond is None:
                continue
            brics_bond_indices.append(int(bond.GetIdx()))

        def _is_mmp_style_cut_bond(bond: Any) -> bool:
            if bond is None:
                return False
            if bond.GetBondType() != Chem.BondType.SINGLE:
                return False
            if bond.IsInRing():
                return False
            begin_atom = bond.GetBeginAtom()
            end_atom = bond.GetEndAtom()
            if int(begin_atom.GetAtomicNum()) <= 1 or int(end_atom.GetAtomicNum()) <= 1:
                return False
            begin_heavy_degree = sum(1 for nei in begin_atom.GetNeighbors() if int(nei.GetAtomicNum()) > 1)
            end_heavy_degree = sum(1 for nei in end_atom.GetNeighbors() if int(nei.GetAtomicNum()) > 1)
            if begin_heavy_degree <= 1 or end_heavy_degree <= 1:
                return False

            begin_aromatic = bool(begin_atom.GetIsAromatic())
            end_aromatic = bool(end_atom.GetIsAromatic())
            begin_in_ring = bool(begin_atom.IsInRing())
            end_in_ring = bool(end_atom.IsInRing())
            conjugated = bool(bond.GetIsConjugated())

            begin_is_sp3_carbon = (
                int(begin_atom.GetAtomicNum()) == 6
                and begin_atom.GetHybridization() == Chem.rdchem.HybridizationType.SP3
                and not begin_aromatic
                and not begin_in_ring
            )
            end_is_sp3_carbon = (
                int(end_atom.GetAtomicNum()) == 6
                and end_atom.GetHybridization() == Chem.rdchem.HybridizationType.SP3
                and not end_aromatic
                and not end_in_ring
            )
            if begin_is_sp3_carbon and end_is_sp3_carbon:
                return False
            if begin_aromatic or end_aromatic or begin_in_ring or end_in_ring or conjugated:
                return True
            if int(begin_atom.GetAtomicNum()) != 6 or int(end_atom.GetAtomicNum()) != 6:
                return True
            return False

        mmp_bond_indices: List[int] = []
        for bond in mol.GetBonds():
            if _is_mmp_style_cut_bond(bond):
                mmp_bond_indices.append(int(bond.GetIdx()))

        atom_groups: List[tuple[int, ...]] = []
        fragmented_for_query = mol
        cut_bond_indices = sorted(set(brics_bond_indices + mmp_bond_indices))
        if cut_bond_indices:
            fragmented = Chem.FragmentOnBonds(mol, cut_bond_indices, addDummies=True)
            fragmented_for_query = fragmented
            raw_groups = Chem.GetMolFrags(fragmented, asMols=False, sanitizeFrags=False)
            atom_groups = [tuple(sorted(int(atom) for atom in group)) for group in raw_groups]
        else:
            atom_groups = [tuple(range(mol.GetNumAtoms()))]

        for atom_indices in atom_groups:
            if not atom_indices:
                continue
            parent_atom_indices = sorted(int(idx) for idx in atom_indices if int(idx) < mol.GetNumAtoms())
            if not parent_atom_indices:
                continue
            frag_smiles = Chem.MolFragmentToSmiles(mol, atomsToUse=list(parent_atom_indices), canonical=True)
            if not frag_smiles:
                continue
            key = (frag_smiles, tuple(parent_atom_indices))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            query_smiles = ''
            try:
                query_smiles = attachment_fragment_smiles_from_atom_indices(mol, list(parent_atom_indices))
            except Exception:
                query_smiles = ''
            if not query_smiles:
                raw_query_smiles = Chem.MolFragmentToSmiles(
                    fragmented_for_query,
                    atomsToUse=list(atom_indices),
                    canonical=True
                )
                query_smiles = _normalize_attachment_smiles(raw_query_smiles) or ''
            heavy_atoms = sum(1 for idx in parent_atom_indices if mol.GetAtomWithIdx(idx).GetAtomicNum() > 1)
            if heavy_atoms <= 0:
                continue
            attachment_count = int(query_smiles.count('*'))
            fragments.append({
                'fragment_id': f'frag_{len(fragments)+1}',
                'smiles': frag_smiles,
                'query_smiles': query_smiles,
                'display_smiles': frag_smiles,
                'atom_indices': list(parent_atom_indices),
                'heavy_atoms': heavy_atoms,
                'attachment_count': attachment_count,
                'num_frags': attachment_count,
                'variable_rank': _variable_rank(heavy_atoms, attachment_count),
            })

        if not fragments:
            fallback_smiles = Chem.MolToSmiles(mol, canonical=True)
            fragments.append({
                'fragment_id': 'frag_1',
                'smiles': fallback_smiles,
                'query_smiles': '',
                'display_smiles': fallback_smiles,
                'atom_indices': list(range(mol.GetNumAtoms())),
                'heavy_atoms': mol.GetNumHeavyAtoms(),
                'attachment_count': 0,
                'num_frags': 1,
                'variable_rank': 0.0,
            })

        # Keep meaningful fragments for clickable map and MMP variable selection.
        fragments = [
            item
            for item in fragments
            if int(item.get('heavy_atoms', 0) or 0) >= 2
            or int(item.get('attachment_count', 0) or 0) > 0
        ]
        if not fragments:
            fallback_smiles = Chem.MolToSmiles(mol, canonical=True)
            fragments = [{
                'fragment_id': 'frag_1',
                'smiles': fallback_smiles,
                'query_smiles': '',
                'display_smiles': fallback_smiles,
                'atom_indices': list(range(mol.GetNumAtoms())),
                'heavy_atoms': mol.GetNumHeavyAtoms(),
                'attachment_count': 0,
                'num_frags': 1,
                'variable_rank': 0.0,
            }]

        max_heavy = max(int(item.get('heavy_atoms', 0) or 0) for item in fragments) if fragments else 0
        fragments.sort(
            key=lambda item: (
                -float(item.get('variable_rank', 0.0) or 0.0),
                -int(item.get('attachment_count', 0) or 0),
                -int(item.get('heavy_atoms', 0) or 0),
            )
        )

        for idx, fragment in enumerate(fragments):
            heavy_atoms = int(fragment.get('heavy_atoms', 0) or 0)
            attachment_count = int(fragment.get('attachment_count', 0) or 0)
            size_component = _size_score(heavy_atoms)
            attach_component = _attachment_score(attachment_count)
            quality = max(0.0, min(1.0, 0.55 * attach_component + 0.45 * size_component))
            coverage = max(
                0.0,
                min(
                    1.0,
                    0.25 + 0.45 * attach_component + 0.30 * (1.0 / (1.0 + math.exp((heavy_atoms - 12.0) / 3.0))),
                ),
            )
            if attachment_count > 0 and quality >= 0.45:
                action = 'variable'
            elif heavy_atoms >= max(3, int(0.72 * max_heavy)):
                action = 'core'
            else:
                action = 'unassigned'
            fragment['recommended_action'] = action
            fragment['color'] = palette[idx % len(palette)]
            fragment['rule_coverage'] = round(coverage, 4)
            fragment['quality_score'] = round(quality, 4)
            fragment['fragment_id'] = f'frag_{idx + 1}'

        recommended_variable_ids = [
            str(item.get('fragment_id') or '')
            for item in fragments
            if str(item.get('recommended_action') or '') == 'variable'
        ][:3]
        if not recommended_variable_ids and fragments:
            recommended_variable_ids = [str(fragments[0].get('fragment_id') or '')]

        return jsonify({
            'smiles': smiles,
            'fragments': fragments,
            'atom_bonds': atom_bonds,
            'recommended_variable_fragment_ids': recommended_variable_ids,
            'auto_generated_rules': {
                'variable_smarts': ';;'.join(
                    str(item.get('query_smiles') or '')
                    for item in fragments
                    if str(item.get('fragment_id') or '') in set(recommended_variable_ids)
                    and '*' in str(item.get('query_smiles') or '')
                ),
                'variable_const_smarts': ';;'.join(
                    str(item.get('query_smiles') or '')
                    for item in fragments
                    if str(item.get('recommended_action') or '') == 'core'
                    and '*' in str(item.get('query_smiles') or '')
                ),
            },
        })

    @app.route('/api/lead_optimization/reference_preview', methods=['POST'])
    @require_api_token
    def lead_optimization_reference_preview():
        if 'reference_target_file' not in request.files:
            return jsonify({'error': "Request must include 'reference_target_file'."}), 400
        target_file = request.files['reference_target_file']
        ligand_file = request.files.get('reference_ligand_file')
        if not target_file.filename:
            return jsonify({'error': 'reference_target_file is empty.'}), 400
        if not ligand_file or not ligand_file.filename:
            return jsonify({'error': "Request must include 'reference_ligand_file'."}), 400

        try:
            target_text = target_file.read().decode('utf-8')
            ligand_text = ligand_file.read().decode('utf-8')
        except UnicodeDecodeError:
            return jsonify({'error': 'Failed to decode reference files as UTF-8 text.'}), 400

        try:
            preview = build_affinity_preview(
                target_text,
                target_file.filename,
                ligand_text,
                ligand_file.filename,
            )
        except affinity_preview_error_cls as exc:
            return jsonify({'error': str(exc)}), 400

        pocket_residues: List[Dict[str, Any]] = []
        ligand_atom_contacts: List[Dict[str, Any]] = []
        ligand_atoms: List[Dict[str, Any]] = []
        target_chain_sequences: Dict[str, str] = {}
        try:
            ligand_chain_ids = [token.strip() for token in str(preview.ligand_chain_id or '').split(',') if token.strip()]
            aa3_to1 = {
                'ALA': 'A', 'ARG': 'R', 'ASN': 'N', 'ASP': 'D', 'CYS': 'C', 'GLN': 'Q', 'GLU': 'E', 'GLY': 'G',
                'HIS': 'H', 'ILE': 'I', 'LEU': 'L', 'LYS': 'K', 'MET': 'M', 'PHE': 'F', 'PRO': 'P', 'SER': 'S',
                'THR': 'T', 'TRP': 'W', 'TYR': 'Y', 'VAL': 'V', 'SEC': 'U', 'PYL': 'O',
            }
            with tempfile.TemporaryDirectory(prefix='lead_opt_ref_preview_') as temp_dir:
                temp_path = Path(temp_dir) / f'preview.{preview.structure_format}'
                temp_path.write_text(preview.structure_text, encoding='utf-8')
                structure = gemmi.read_structure(str(temp_path))
                structure.setup_entities()
                model = structure[0]
                for chain in model:
                    chain_id = str(chain.name).strip()
                    if not chain_id or chain_id in ligand_chain_ids:
                        continue
                    residues_seen = set()
                    seq_chars: List[str] = []
                    for residue in chain:
                        if residue.het_flag != 'A':
                            continue
                        residue_key = (int(residue.seqid.num), str(residue.seqid.icode or '').strip())
                        if residue_key in residues_seen:
                            continue
                        residues_seen.add(residue_key)
                        residue_name = str(residue.name or '').strip().upper()
                        seq_chars.append(aa3_to1.get(residue_name, 'X'))
                    sequence = ''.join(seq_chars).strip()
                    if sequence:
                        target_chain_sequences[chain_id] = sequence
                ligand_positions: List[gemmi.Position] = []
                ligand_atoms = []
                missing_encoded_atom_count = 0
                for chain in model:
                    if str(chain.name).strip() not in ligand_chain_ids:
                        continue
                    chain_id = str(chain.name).strip()
                    for residue in chain:
                        residue_name = str(residue.name).strip()
                        residue_number = int(residue.seqid.num)
                        for atom in residue:
                            try:
                                if atom.element.is_hydrogen():
                                    continue
                            except Exception:
                                pass
                            ligand_positions.append(atom.pos)
                            atom_name = str(atom.name).strip()
                            decoded_atom_index = decode_smiles_atom_index_from_name(atom_name)
                            if decoded_atom_index is None or int(decoded_atom_index) < 0:
                                missing_encoded_atom_count += 1
                                continue
                            mapped_atom_index = int(decoded_atom_index)
                            ligand_atoms.append({
                                'atom_index': int(mapped_atom_index),
                                'chain_id': chain_id,
                                'residue_name': residue_name,
                                'residue_number': residue_number,
                                'atom_name': atom_name,
                                'position': atom.pos,
                            })
                if missing_encoded_atom_count > 0 and preview.supports_activity:
                    raise RuntimeError(
                        f'Reference ligand atom mapping failed: {missing_encoded_atom_count} atoms missing encoded names.'
                    )

                protein_residue_records: List[Dict[str, Any]] = []
                for chain in model:
                    chain_id = str(chain.name).strip()
                    if chain_id in ligand_chain_ids:
                        continue
                    for residue in chain:
                        if residue.het_flag != 'A':
                            continue
                        atom_positions = [atom.pos for atom in residue]
                        if not atom_positions:
                            continue
                        protein_residue_records.append({
                            'chain_id': chain_id,
                            'residue_name': str(residue.name).strip(),
                            'residue_number': int(residue.seqid.num),
                            'atom_positions': atom_positions,
                        })

                if ligand_positions:
                    for record in protein_residue_records:
                        min_distance = None
                        for atom_pos in record['atom_positions']:
                            for lig_pos in ligand_positions:
                                distance = atom_pos.dist(lig_pos)
                                if min_distance is None or distance < min_distance:
                                    min_distance = distance
                        if min_distance is None or min_distance > 5.0:
                            continue
                        interaction_types = []
                        if min_distance <= 3.5:
                            interaction_types.append('hbond_like')
                        if min_distance <= 4.5:
                            interaction_types.append('hydrophobic_like')
                        pocket_residues.append({
                            'chain_id': record['chain_id'],
                            'residue_name': record['residue_name'],
                            'residue_number': record['residue_number'],
                            'min_distance': round(float(min_distance), 3),
                            'interaction_types': interaction_types or ['contact'],
                        })

                    pocket_key_set = {
                        (
                            str(item.get('chain_id', '')),
                            int(item.get('residue_number', 0) or 0),
                        )
                        for item in pocket_residues
                    }
                    pocket_records = [
                        record
                        for record in protein_residue_records
                        if (record['chain_id'], record['residue_number']) in pocket_key_set
                    ]
                    for ligand_atom in ligand_atoms:
                        contacts: List[Dict[str, Any]] = []
                        lig_pos = ligand_atom['position']
                        for record in pocket_records:
                            min_distance = None
                            for atom_pos in record['atom_positions']:
                                distance = atom_pos.dist(lig_pos)
                                if min_distance is None or distance < min_distance:
                                    min_distance = distance
                            if min_distance is None or min_distance > 5.0:
                                continue
                            contacts.append({
                                'chain_id': record['chain_id'],
                                'residue_name': record['residue_name'],
                                'residue_number': record['residue_number'],
                                'min_distance': round(float(min_distance), 3),
                            })
                        contacts.sort(key=lambda item: item['min_distance'])
                        if contacts:
                            ligand_atom_contacts.append({
                                'atom_index': int(ligand_atom['atom_index']),
                                'chain_id': str(ligand_atom.get('chain_id') or ''),
                                'residue_name': str(ligand_atom.get('residue_name') or ''),
                                'residue_number': int(ligand_atom.get('residue_number') or 0),
                                'atom_name': str(ligand_atom.get('atom_name') or ''),
                                'residues': contacts[:8],
                            })
        except Exception as exc:
            logger.warning('Failed to extract reference interactions: %s', exc)

        pocket_residues.sort(key=lambda item: item['min_distance'])
        pocket_residues = pocket_residues[:80]

        return jsonify({
            'target_chain_ids': preview.target_chain_ids,
            'ligand_chain_id': preview.ligand_chain_id,
            'ligand_smiles': preview.ligand_smiles,
            'supports_activity': preview.supports_activity,
            'target_chain_sequences': target_chain_sequences,
            'complex_structure_text': preview.structure_text,
            'complex_structure_format': preview.structure_format,
            'structure_text': preview.target_structure_text,
            'structure_format': preview.target_structure_format,
            'overlay_structure_text': preview.ligand_structure_text,
            'overlay_structure_format': preview.ligand_structure_format,
            'pocket_residues': pocket_residues,
            'ligand_atom_contacts': ligand_atom_contacts,
            'ligand_atom_map': [
                {
                    'atom_index': int(item.get('atom_index') or 0),
                    'chain_id': str(item.get('chain_id') or ''),
                    'residue_name': str(item.get('residue_name') or ''),
                    'residue_number': int(item.get('residue_number') or 0),
                    'atom_name': str(item.get('atom_name') or ''),
                }
                for item in ligand_atoms
            ],
            'key_interaction_fragments': pocket_residues[:10],
        })
