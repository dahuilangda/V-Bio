import { describe, it, expect } from 'vitest';
import JSZip from 'jszip';
import { parseResultBundle } from './resultBundleParser';
import {
  ensureCifHasMaQaMetricLocal,
  ensureStructureConfidenceColoringData
} from './cifConfidenceColoring';
import { cifResidueKey, collectCifPolymerResidueKeys } from './resultBundleHelpers';

interface ResidueSpec {
  chain: string;
  entity: string;
  seq: number;
  comp: string;
  group: string;
  plddt: number;
}

function buildCif(
  residues: ResidueSpec[],
  options: { entities?: Array<[string, string]>; groupColumn?: boolean } = {}
): string {
  const lines = ['data_test'];
  if (options.entities) {
    lines.push('loop_', '_entity.id', '_entity.type');
    for (const [id, type] of options.entities) lines.push(`${id} ${type}`);
    lines.push('#');
  }
  const hasGroup = options.groupColumn !== false;
  lines.push(
    'loop_',
    ...(hasGroup ? ['_atom_site.group_PDB'] : []),
    '_atom_site.id',
    '_atom_site.type_symbol',
    '_atom_site.label_atom_id',
    '_atom_site.label_comp_id',
    '_atom_site.label_asym_id',
    '_atom_site.label_entity_id',
    '_atom_site.label_seq_id',
    '_atom_site.auth_seq_id',
    '_atom_site.auth_asym_id',
    '_atom_site.B_iso_or_equiv',
    '_atom_site.Cartn_x',
    '_atom_site.Cartn_y',
    '_atom_site.Cartn_z',
    '_atom_site.occupancy'
  );
  let id = 0;
  for (const residue of residues) {
    for (const atom of ['N', 'CA', 'C']) {
      id += 1;
      const tokens = [
        ...(hasGroup ? [residue.group] : []),
        String(id),
        'C',
        atom,
        residue.comp,
        residue.chain,
        residue.entity,
        String(residue.seq),
        String(residue.seq),
        residue.chain,
        residue.plddt.toFixed(2),
        '1.000',
        '2.000',
        '3.000',
        '1.0'
      ];
      lines.push(tokens.join(' '));
    }
  }
  lines.push('#');
  return lines.join('\n');
}

function qaRows(structureText: string): string[] {
  const lines = structureText.split(/\r?\n/);
  const header = lines.findIndex((line) => line.startsWith('_ma_qa_metric_local.label_asym_id'));
  if (header < 0) return [];
  let index = header + 1;
  while (index < lines.length && lines[index].startsWith('_')) index += 1;
  const rows: string[] = [];
  while (index < lines.length && lines[index].trim() !== '#' && !lines[index].startsWith('loop_')) {
    rows.push(lines[index]);
    index += 1;
  }
  return rows;
}

const ENTITIES: Array<[string, string]> = [['1', 'polymer'], ['2', 'non-polymer']];

describe('collectCifPolymerResidueKeys', () => {
  it('classifies residues by entity type, not by residue name', () => {
    const keys = collectCifPolymerResidueKeys(buildCif([
      { chain: 'B', entity: '1', seq: 1, comp: 'CYS', group: 'ATOM', plddt: 90 },
      { chain: 'B', entity: '1', seq: 2, comp: 'NLE', group: 'ATOM', plddt: 88 },
      { chain: 'C', entity: '2', seq: 1, comp: 'ZBR', group: 'HETATM', plddt: 70 }
    ], { entities: ENTITIES }));
    expect([...keys].sort()).toEqual([cifResidueKey('B', '1', 'CYS'), cifResidueKey('B', '2', 'NLE')]);
  });

  it('keeps HETATM residues of a polymer entity and drops ATOM residues of a non-polymer entity', () => {
    const keys = collectCifPolymerResidueKeys(buildCif([
      { chain: 'C', entity: '1', seq: 1, comp: 'ACILE', group: 'HETATM', plddt: 31 },
      { chain: 'C', entity: '1', seq: 2, comp: 'HIS', group: 'ATOM', plddt: 33 },
      { chain: 'L', entity: '2', seq: 1, comp: 'LIG', group: 'ATOM', plddt: 80 }
    ], { entities: ENTITIES }));
    expect([...keys].sort()).toEqual([cifResidueKey('C', '1', 'ACILE'), cifResidueKey('C', '2', 'HIS')]);
  });

  it('falls back to the record group when the entity table is absent', () => {
    const keys = collectCifPolymerResidueKeys(buildCif([
      { chain: 'B', entity: '1', seq: 2, comp: 'NLE', group: 'ATOM', plddt: 88 },
      { chain: 'C', entity: '2', seq: 1, comp: 'ZBR', group: 'HETATM', plddt: 70 }
    ]));
    expect([...keys]).toEqual([cifResidueKey('B', '2', 'NLE')]);
  });

  it('falls back to the residue name when neither entity table nor record group exists', () => {
    const keys = collectCifPolymerResidueKeys(buildCif([
      { chain: 'B', entity: '1', seq: 1, comp: 'ALA', group: 'ATOM', plddt: 88 },
      { chain: 'C', entity: '2', seq: 1, comp: 'ZBR', group: 'HETATM', plddt: 70 }
    ], { groupColumn: false }));
    expect([...keys]).toEqual([cifResidueKey('B', '1', 'ALA')]);
  });
});

describe('AlphaFold confidence coloring data', () => {
  const protenixStyle = buildCif([
    { chain: 'B', entity: '1', seq: 1, comp: 'CYS', group: 'ATOM', plddt: 90 },
    { chain: 'B', entity: '1', seq: 2, comp: 'NLE', group: 'ATOM', plddt: 88 },
    { chain: 'C', entity: '2', seq: 1, comp: 'ZBR', group: 'HETATM', plddt: 70 }
  ], { entities: ENTITIES });

  it('keeps non-standard polymer residues through repeated application', () => {
    const once = ensureStructureConfidenceColoringData(protenixStyle, 'cif', 'protenix');
    const twice = ensureStructureConfidenceColoringData(once, 'cif', 'protenix');
    expect(qaRows(once).map((row) => row.split(/\s+/).slice(2, 5))).toEqual([['B', '1', 'CYS'], ['B', '2', 'NLE']]);
    expect(qaRows(twice)).toEqual(qaRows(once));
  });

  it('prunes native metric rows of non-polymer residues only', () => {
    const native = `${protenixStyle}#
loop_
_ma_qa_metric.id
_ma_qa_metric.mode
1 local
#
loop_
_ma_qa_metric_local.ordinal_id
_ma_qa_metric_local.model_id
_ma_qa_metric_local.label_asym_id
_ma_qa_metric_local.label_seq_id
_ma_qa_metric_local.label_comp_id
_ma_qa_metric_local.metric_id
_ma_qa_metric_local.metric_value
1 1 B 1 CYS 1 90.000
2 1 B 2 NLE 1 88.000
3 1 C 1 ZBR 1 70.000
#
`;
    const rows = qaRows(ensureStructureConfidenceColoringData(native, 'cif', 'protenix'));
    expect(rows.map((row) => row.split(/\s+/).slice(2, 5))).toEqual([['B', '1', 'CYS'], ['B', '2', 'NLE']]);
  });

  it('synthesizes metrics for HETATM residues of a polymer entity', () => {
    const af3Style = buildCif([
      { chain: 'C', entity: '1', seq: 1, comp: 'ACILE', group: 'HETATM', plddt: 31 },
      { chain: 'C', entity: '1', seq: 2, comp: 'HIS', group: 'ATOM', plddt: 33 },
      { chain: 'L', entity: '2', seq: 1, comp: 'LIG', group: 'HETATM', plddt: 80 }
    ], { entities: ENTITIES });
    const rows = qaRows(ensureCifHasMaQaMetricLocal(af3Style));
    expect(rows.map((row) => row.split(/\s+/).slice(2, 7))).toEqual([
      ['C', '1', 'ACILE', '1', '31.000'],
      ['C', '2', 'HIS', '1', '33.000']
    ]);
  });

  it('still synthesizes metrics for ligand atoms written as ATOM records', () => {
    const esmfold2Style = buildCif([
      { chain: 'L', entity: '2', seq: 1, comp: 'LIG', group: 'ATOM', plddt: 80 }
    ], { entities: ENTITIES });
    expect(qaRows(ensureCifHasMaQaMetricLocal(esmfold2Style)).map((row) => row.split(/\s+/).slice(2, 5)))
      .toEqual([['L', '1', 'LIG']]);
  });
});

describe('parseResultBundle — modified binder next to a linker ligand', () => {
  it('aligns per-residue confidence with the sequence and keys ligand atoms by the declared chain', async () => {
    const structure = buildCif([
      { chain: 'A', entity: '1', seq: 1, comp: 'THR', group: 'ATOM', plddt: 97 },
      { chain: 'B', entity: '1', seq: 1, comp: 'CYS', group: 'ATOM', plddt: 90 },
      { chain: 'B', entity: '1', seq: 2, comp: 'NLE', group: 'ATOM', plddt: 88 },
      { chain: 'B', entity: '1', seq: 3, comp: 'SER', group: 'ATOM', plddt: 84 },
      { chain: 'C', entity: '2', seq: 1, comp: 'ZBR', group: 'HETATM', plddt: 70 }
    ], { entities: ENTITIES });
    const prefix = 'protenix/output/boltz_protenix/seed_42/predictions/boltz_protenix_';
    const zip = new JSZip();
    zip.file(`${prefix}sample_0.cif`, structure);
    zip.file(`${prefix}summary_confidence_sample_0.json`, JSON.stringify({ ranking_score: 0.8 }));
    zip.file(`${prefix}full_data_sample_0.json`, JSON.stringify({ pae: [[0]], token_asym_id: [0] }));
    zip.file('best_ipsae.json', JSON.stringify({ ligand_chain_id: 'B' }));
    zip.file('compact_metrics.json', JSON.stringify({ ligand_atom_plddts: [90, 88, 84] }));
    const parsed = await parseResultBundle(
      (await zip.generateAsync({ type: 'arraybuffer' })) as unknown as Blob,
      { preservePeptideCandidateStructureText: false }
    );
    expect(parsed).not.toBeNull();
    const confidence = parsed!.confidence as Record<string, any>;
    expect(confidence.residue_plddt_by_chain.B).toEqual([90, 88, 84]);
    expect(Object.keys(confidence.ligand_atom_plddts_by_chain)).toEqual(['B']);
    expect(qaRows(parsed!.structureText).map((row) => row.split(/\s+/).slice(2, 5)))
      .toContainEqual(['B', '2', 'NLE']);
  });
});
