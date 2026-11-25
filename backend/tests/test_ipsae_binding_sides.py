from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest
import shape_complementarity

from backend.services.result_archive import ResultArchiveService, _binding_record

_CIF_TAGS = (
    'group_PDB', 'label_atom_id', 'label_comp_id', 'label_asym_id', 'label_seq_id',
    'auth_seq_id', 'auth_asym_id', 'type_symbol', 'Cartn_x', 'Cartn_y', 'Cartn_z',
)
_RESIDUE_ATOMS = {'ALA': (('N', 'N'), ('CA', 'C'), ('C', 'C')), 'LIG': (('C1', 'C'), ('C2', 'C'))}


def _cif(chains, auth_ids=None) -> str:
    lines = ['data_test', 'loop_'] + [f'_atom_site.{tag}' for tag in _CIF_TAGS]
    serial = 0
    for label, spec in chains:
        for seq, (comp, extra) in enumerate(spec, start=1):
            record = 'ATOM' if comp == 'ALA' else 'HETATM'
            atoms = [(name, symbol) for name, symbol in _RESIDUE_ATOMS.get(comp, (('O', 'O'),))] + list(extra)
            for name, symbol in atoms:
                serial += 1
                auth = (auth_ids or {}).get(label, label)
                lines.append(
                    f'{record} {name} {comp} {label} {seq} {seq} {auth} {symbol} {serial / 10:.1f} 0.0 0.0')
    return '\n'.join(lines) + '\n'


def _polymer(count):
    return [('ALA', ())] * count


def _ligand():
    return [('LIG', ())]


def _archive(directory, members: dict) -> zipfile.ZipFile:
    path = Path(directory) / f'{len(list(Path(directory).iterdir()))}.zip'
    with zipfile.ZipFile(path, 'w') as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
    return zipfile.ZipFile(path)


def _boltz_archive(directory, cif, yaml_text=None, metadata=None) -> zipfile.ZipFile:
    members = {
        'data_model_0.cif': cif,
        'pae_data_model_0.npz': b'pae',
        'confidence_data_model_0.json': json.dumps({'confidence_score': 0.5}),
    }
    if yaml_text is not None:
        members['boltz/input.yaml'] = yaml_text
    if metadata is not None:
        members['af3/metadata.json'] = json.dumps(metadata)
    return _archive(directory, members)


_IL17A_YAML = """version: 1
sequences:
  - protein:
      id: A
  - protein:
      id: B
  - protein:
      id: C
properties:
  - ligand: C
    binder: C
    affinity: false
    target: A
"""


def _value(aligned: str, scored: str) -> float:
    return round(0.1 * (sum(map(ord, aligned)) % 7 + 1) + 0.01 * (sum(map(ord, scored)) % 5 + 1), 6)


def _stub_ipsae_cli(monkeypatch) -> list:
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        cif = Path(command[command.index('-m') + 2])
        groups = [command[index + 1] for index, arg in enumerate(command) if arg == '--group']
        singles = [line.split()[3] for line in cif.read_text().splitlines()
                   if line.startswith(('ATOM ', 'HETATM '))]
        chains = list(dict.fromkeys(singles))
        entities = groups + chains
        rows = [(first, second) for index, first in enumerate(chains) for second in chains[index + 1:]]
        rows += [(first, second) for index, first in enumerate(entities[:len(groups)])
                 for second in entities[index + 1:]]
        text = ['chn1\tchn2\tipsae12\tipsae21']
        text += [f'{first}\t{second}\t{_value(first, second):.6f}\t{_value(second, first):.6f}'
                 for first, second in rows]
        Path(command[command.index('--output-file') + 1]).write_text('\n'.join(text) + '\n')
        return subprocess.CompletedProcess(command, 0, '', '')

    monkeypatch.setattr(subprocess, 'run', run)
    return commands


def test_binding_record_accepts_yaml_and_row_shapes():
    yaml_record = _binding_record(
        [{'ligand': 'C', 'binder': 'C', 'target': 'A'}],
        [{'bond': {'atom1': ['C', 1, 'SG'], 'atom2': ['L', 1, 'C1']}}, {'pocket': {'binder': 'C'}}])
    row_record = _binding_record(
        {'binder': 'C', 'ligand': 'C', 'target': None, 'affinity': True},
        [{'type': 'bond', 'atom1_chain': 'C', 'atom2_chain': 'L',
          'atom1_residue': 1, 'atom2_residue': 1},
         {'type': 'pocket', 'binder': 'C'}])
    assert yaml_record == {'properties': [{'ligand': 'C', 'binder': 'C', 'target': 'A'}], 'bonds': [('C', 'L')]}
    assert row_record == {'properties': [{'binder': 'C', 'ligand': 'C', 'target': None, 'affinity': True}],
                          'bonds': [('C', 'L')]}
    assert _binding_record(None, None) == {'properties': [], 'bonds': []}


def test_cif_chain_atoms_are_keyed_by_label_asym_id():
    cif = _cif([('A', _polymer(2)), ('C', _ligand())], auth_ids={'A': 'RECEPTOR', 'C': 'PEP'})
    atoms = ResultArchiveService._cif_chain_atoms(cif)
    assert list(atoms) == ['A', 'C']
    assert ResultArchiveService._chain_tokens(atoms) == {
        'A': {'residues': 2, 'atoms': 0}, 'C': {'residues': 0, 'atoms': 2}}


def test_cif_chain_atoms_drop_water_hydrogen_and_alternates():
    lines = _cif([('A', _polymer(1))]).splitlines()
    header, lines = lines[:2 + len(_CIF_TAGS)], lines[2 + len(_CIF_TAGS):]
    header.insert(7, '_atom_site.label_alt_id')
    lines = [line.replace('ATOM N ALA A 1 1 A N', 'ATOM N ALA A 1 . 1 A N')
             .replace('ATOM CA ALA A 1 1 A C', 'ATOM CA ALA A 1 B 1 A C')
             .replace('ATOM C ALA A 1 1 A C', 'ATOM C ALA A 1 . 1 A C') for line in lines]
    lines += ['HETATM O HOH W 1 . 1 W O 1.0 0.0 0.0',
              'ATOM H ALA A 1 . 1 A H 1.0 0.0 0.0']
    atoms = ResultArchiveService._cif_chain_atoms('\n'.join(header + lines) + '\n')
    assert [atom[3] for atom in atoms['A']] == ['N', 'C']
    assert 'W' not in atoms


@pytest.mark.parametrize('chains, properties, bonds, expected', [
    ([('A', 1), ('B', 1), ('C', 1)], [{'binder': 'C', 'ligand': 'C', 'target': 'A'}], [], [['A', 'B'], ['C']]),
    ([('A', 1), ('B', 1), ('C', 1), ('L', 1)], [{'binder': 'C', 'target': 'A'}], [('C', 'L')],
     [['A', 'B'], ['C', 'L']]),
    ([('A', 1), ('B', 1), ('L', 1)], [{'ligand': 'L', 'target': 'A'}], [('A', 'L')], [['A', 'B'], ['L']]),
    ([('A', 1), ('C', 1), ('L', 1)], [{'binder': 'C', 'ligand': 'L', 'target': 'A'}], [], [['A', 'L'], ['C']]),
    ([('A', 1), ('B', 1)], [{'ligand': 'B'}], [], [['A'], ['B']]),
    ([('A', 1), ('B', 1)], [{'binder': 'Z', 'target': 'A'}], [], None),
    ([('A', 1), ('B', 1)], [{'binder': None, 'target': 'A'}], [], None),
    ([('A', 1)], [{'binder': 'A'}], [], None),
])
def test_binding_sides(tmp_path, chains, properties, bonds, expected):
    sides = ResultArchiveService._binding_sides(
        _archive(tmp_path, {}), {chain: [] for chain, _count in chains},
        {'properties': properties, 'bonds': bonds})
    assert sides == (tuple(expected) if expected else None)


def test_binding_sides_follow_af3_chain_labels(tmp_path):
    zf = _archive(tmp_path, {'af3/metadata.json': json.dumps({'chain_id_label_map': {'receptor': 'A', 'pep': 'C'}})})
    chains = {'A': [], 'C': []}
    binding = {'properties': [{'binder': 'pep', 'target': 'receptor'}], 'bonds': [('receptor', 'pep')]}
    assert ResultArchiveService._binding_sides(zf, chains, binding) == (['A'], ['C'])


def test_pair_ipsae_scores_binder_against_receptor_group(tmp_path, monkeypatch):
    commands = _stub_ipsae_cli(monkeypatch)
    zf = _boltz_archive(tmp_path, _cif([('A', _polymer(3)), ('B', _polymer(3)), ('C', _polymer(1))]), _IL17A_YAML)
    out = ResultArchiveService._extract_pair_ipsae(zf)
    assert commands[0][commands[0].index('--group') + 1] == 'A+B'
    assert out['ipsae_protein_to_ligand'] == _value('A+B', 'C')
    assert out['ipsae_ligand_to_protein'] == _value('C', 'A+B')
    assert out['ipsae_official_max'] == max(_value('A+B', 'C'), _value('C', 'A+B'))
    assert [(row['a'], row['b']) for row in out['pair_ipsae']] == [('A', 'B'), ('A', 'C'), ('B', 'C'), ('A+B', 'C')]
    assert out['pair_ipsae'][-1] == {'a': 'A+B', 'b': 'C', 'grouped': True,
                                     'a_to_b': _value('A+B', 'C'), 'b_to_a': _value('C', 'A+B')}


def test_pair_ipsae_groups_both_sides(tmp_path, monkeypatch):
    commands = _stub_ipsae_cli(monkeypatch)
    yaml_text = _IL17A_YAML.replace(
        "  - protein:\n      id: C\n", "  - protein:\n      id: C\n  - ligand:\n      id: L\n") + (
        "constraints:\n  - bond:\n      atom1: [C, 1, SG]\n      atom2: [L, 1, C1]\n")
    cif = _cif([('A', _polymer(3)), ('B', _polymer(3)), ('C', _polymer(1)), ('L', _ligand())])
    out = ResultArchiveService._extract_pair_ipsae(_boltz_archive(tmp_path, cif, yaml_text))
    groups = [commands[0][index + 1] for index, arg in enumerate(commands[0]) if arg == '--group']
    assert groups == ['A+B', 'C+L']
    assert out['ipsae_protein_to_ligand'] == _value('A+B', 'C+L')
    assert out['ipsae_ligand_to_protein'] == _value('C+L', 'A+B')
    assert out['pair_ipsae'][-1]['b'] == 'C+L'


def test_pair_ipsae_passes_label_ids_for_non_letter_chains(tmp_path, monkeypatch):
    commands = _stub_ipsae_cli(monkeypatch)
    cif = _cif([('X', _polymer(3)), ('Y', _polymer(2)), ('Z', _polymer(1))])
    full = {'token_asym_id': [0] * 3 + [1] * 2 + [2], 'pae': [[0.0] * 6 for _ in range(6)]}
    yaml_text = (_IL17A_YAML.replace('id: A', 'id: X').replace('id: B', 'id: Y').replace('id: C', 'id: Z')
                 .replace('ligand: C', 'ligand: Z').replace('binder: C', 'binder: Z').replace('target: A', 'target: X'))
    zf = _archive(tmp_path, {
        'protenix/input.yaml': yaml_text,
        'protenix/output/input/seed_42/predictions/boltz_protenix_sample_0.cif': cif,
        'protenix/output/input/seed_42/predictions/boltz_protenix_full_data_sample_0.json': json.dumps(full),
        'protenix/output/input/seed_42/predictions/boltz_protenix_summary_confidence_sample_0.json': json.dumps({'ranking_score': 0.4}),
    })
    out = ResultArchiveService._extract_pair_ipsae(zf)
    assert commands[0][commands[0].index('--group') + 1] == 'X+Y'
    assert [(row['a'], row['b']) for row in out['pair_ipsae']] == [('A', 'B'), ('A', 'C'), ('B', 'C'), ('A+B', 'C')]


def test_pair_ipsae_without_designated_binder_groups_the_rest(tmp_path, monkeypatch):
    commands = _stub_ipsae_cli(monkeypatch)
    zf = _boltz_archive(tmp_path, _cif([('A', _polymer(3)), ('B', _polymer(2)), ('C', _ligand())]))
    out = ResultArchiveService._extract_pair_ipsae(zf)
    assert commands[0][commands[0].index('--group') + 1] == 'B+C'
    assert out['pair_ipsae'][-1] == {'a': 'A', 'b': 'B+C', 'grouped': True,
                                     'a_to_b': _value('A', 'B+C'), 'b_to_a': _value('B+C', 'A')}
    assert out['ipsae_official_max'] == max(_value('A', 'B+C'), _value('B+C', 'A'))


def test_pair_ipsae_two_chain_system_has_no_virtual_row(tmp_path, monkeypatch):
    _stub_ipsae_cli(monkeypatch)
    yaml_text = ("version: 1\nsequences:\n  - protein:\n      id: A\n  - ligand:\n      id: C\n"
                 "properties:\n  - ligand: C\n    binder: C\n    target: A\n")
    zf = _boltz_archive(tmp_path, _cif([('A', _polymer(3)), ('C', _ligand())]), yaml_text)
    assert ResultArchiveService._extract_pair_ipsae(zf) == {
        'pair_ipsae': [{'a': 'A', 'b': 'C', 'a_to_b': _value('A', 'C'), 'b_to_a': _value('C', 'A')}],
        'ipsae_protein_to_ligand': _value('A', 'C'),
        'ipsae_ligand_to_protein': _value('C', 'A'),
        'ipsae_official_max': max(_value('A', 'C'), _value('C', 'A')),
    }


def test_pair_ipsae_uses_task_row_binding_when_archive_has_no_input(tmp_path, monkeypatch):
    _stub_ipsae_cli(monkeypatch)
    cif = _cif([('A', _polymer(3)), ('B', _polymer(3)), ('C', _polymer(1))])
    binding = _binding_record({'binder': 'C', 'ligand': 'C', 'target': 'A', 'affinity': False}, [])
    out = ResultArchiveService._extract_pair_ipsae(_boltz_archive(tmp_path, cif), binding)
    assert out['pair_ipsae'][-1] == {'a': 'A+B', 'b': 'C', 'grouped': True,
                                     'a_to_b': _value('A+B', 'C'), 'b_to_a': _value('C', 'A+B')}


@pytest.mark.parametrize('yaml_text, expected', [
    (_IL17A_YAML, (15, 3)),
    (None, (9, 9)),
])
def test_interface_sc_follows_binding_sides(tmp_path, monkeypatch, yaml_text, expected):
    seen = {}

    class _Result:
        sc = 0.6
        median_distance = 4.5

    def compute_sc(receptor_coords, _ra, _rb, binder_coords, _ba, _bb):
        seen['sides'] = (len(receptor_coords), len(binder_coords))
        return _Result()

    monkeypatch.setattr(shape_complementarity, 'compute_sc', compute_sc)
    cif = _cif([('A', _polymer(3)), ('B', _polymer(2)), ('C', _polymer(1))])
    out = ResultArchiveService._extract_interface_sc(_boltz_archive(tmp_path, cif, yaml_text))
    assert out == {'interface_sc': 0.6, 'interface_sc_median': 4.5}
    assert seen['sides'] == expected
