from __future__ import annotations

import json
import logging
import threading
import zipfile

from backend.services.result_archive import ResultArchiveService

_CIF_TAGS = (
    'group_PDB', 'label_atom_id', 'label_comp_id', 'label_asym_id', 'label_seq_id',
    'auth_asym_id', 'type_symbol', 'B_iso_or_equiv', 'label_entity_id', 'Cartn_x', 'Cartn_y', 'Cartn_z',
)

_ENTITIES = (('1', 'polymer'), ('2', 'non-polymer'))

_SUMMARY = {
    'confidence_score': 0.72, 'ptm': 0.87, 'iptm': 0.87, 'ligand_iptm': 0.88, 'protein_iptm': 0.86,
    'complex_plddt': 0.69, 'complex_iplddt': 0.61, 'complex_pde': 0.75, 'complex_ipde': 1.5,
    'chains_ptm': {'0': 0.88}, 'pair_chains_iptm': {'0': {'0': 0.88}},
}


def _cif(residues, entities=()) -> str:
    lines = ['data_test']
    if entities:
        lines += ['loop_', '_entity.id', '_entity.type', *(f'{entity} {kind}' for entity, kind in entities), '#']
    lines += ['loop_', *(f'_atom_site.{tag}' for tag in _CIF_TAGS)]
    for record, comp, chain, seq, plddt, entity in residues:
        for name in ('N', 'C'):
            lines.append(f'{record} {name} {comp} {chain} {seq} {chain} C {plddt} {entity} {seq}.0 0.0 0.0')
    return '\n'.join(lines) + '\n'


def _service() -> ResultArchiveService:
    service = ResultArchiveService.__new__(ResultArchiveService)
    service.logger = logging.getLogger('test')
    service._compact_metrics_cache = {}
    service._compact_metrics_lock = threading.Lock()
    service._extract_pair_ipsae = lambda *_args, **_kwargs: None
    service._extract_interface_sc = lambda *_args, **_kwargs: None
    return service


def _archive(tmp_path, cif) -> str:
    path = tmp_path / 'result.zip'
    with zipfile.ZipFile(path, 'w') as zf:
        zf.writestr('data_model_0.cif', cif)
        zf.writestr('pae_data_model_0.npz', b'pae')
        zf.writestr('confidence_data_model_0.json', json.dumps(_SUMMARY))
    return str(path)


def test_chain_atoms_carry_confidence_and_entity_class():
    atoms = ResultArchiveService._cif_chain_atoms(_cif([
        ('ATOM', 'CYS', 'B', 1, 90.0, '1'),
        ('HETATM', 'NLE', 'B', 2, 88.0, '1'),
        ('ATOM', 'LIG', 'L', 1, 80.0, '2'),
        ('ATOM', 'ALA', 'D', 1, '?', '1'),
    ], entities=_ENTITIES))
    assert [(atom[6], atom[7]) for atom in atoms['B']] == [(90.0, True), (90.0, True), (88.0, True), (88.0, True)]
    assert [(atom[6], atom[7]) for atom in atoms['L']] == [(80.0, False), (80.0, False)]
    assert [atom[6] for atom in atoms['D']] == [None, None]


def test_residue_plddt_follows_polymer_entities(tmp_path):
    path = _archive(tmp_path, _cif([
        ('ATOM', 'CYS', 'B', 1, 90.0, '1'),
        ('HETATM', 'NLE', 'B', 2, 88.0, '1'),
        ('HETATM', 'ZBR', 'C', 1, 70.0, '2'),
        ('ATOM', 'LIG', 'L', 1, 80.0, '2'),
        ('ATOM', 'ALA', 'D', 1, '?', '1'),
    ], entities=_ENTITIES))
    compact = _service()._extract_compact_prediction_metrics_from_zip(path)
    assert compact['residue_plddt_by_chain'] == {'B': [90.0, 88.0], 'L': [80.0]}
    assert compact['chain_mean_plddt'] == {'B': 89.0, 'C': 70.0, 'L': 80.0}


def test_residue_plddt_without_entity_table_uses_record_type(tmp_path):
    path = _archive(tmp_path, _cif([
        ('ATOM', 'CYS', 'B', 1, 90.0, '1'),
        ('HETATM', 'NLE', 'B', 2, 88.0, '1'),
        ('ATOM', 'LIG', 'L', 1, 80.0, '2'),
    ]))
    compact = _service()._extract_compact_prediction_metrics_from_zip(path)
    assert compact['residue_plddt_by_chain'] == {'B': [90.0], 'L': [80.0]}
    assert compact['chain_mean_plddt'] == {'B': 89.0, 'L': 80.0}


def test_residue_plddt_averages_residue_atoms(tmp_path):
    cif = _cif([('ATOM', 'CYS', 'B', 1, 90.0, '1')], entities=_ENTITIES).replace(
        'ATOM N CYS B 1 B C 90.0', 'ATOM N CYS B 1 B C 91.0')
    compact = _service()._extract_compact_prediction_metrics_from_zip(_archive(tmp_path, cif))
    assert compact['residue_plddt_by_chain'] == {'B': [90.5]}
    assert compact['chain_mean_plddt'] == {'B': 90.5}
