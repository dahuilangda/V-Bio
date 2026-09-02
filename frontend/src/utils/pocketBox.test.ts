import { describe, it, expect } from 'vitest';
import {
  detectLigands,
  computePocketBoxFromLigandAtoms,
  buildPocketBoxPdb,
  computePocketBoxFromLigandStructure,
  computePocketBoxFromResiduePicks,
  extractResidueCAPoints,
  parseStructureAtomCoords,
  pocketTargetChanged,
  type PocketBox
} from './pocketBox';

const PDB_TEXT = [
  'ATOM      1  N   MET A   1       0.000   0.000   0.000  1.00 50.00           N',
  'ATOM      2  CA  MET A   1       1.000   0.000   0.000  1.00 50.00           C',
  'ATOM      3  N   GLY A   2      10.000   4.000   2.000  1.00 50.00           N',
  'ATOM      4  CA  GLY A   2      11.000   4.000   2.000  1.00 50.00           C',
  'ATOM      5  CA  LYS B   3       5.000   5.000   5.000  1.00 50.00           C',
  'HETATM    6  C1  LIG L   1       2.000   2.000   2.000  1.00 50.00           C',
  'END'
].join('\n');

const CIF_TEXT = [
  'data_test',
  'loop_',
  '_atom_site.group_PDB',
  '_atom_site.id',
  '_atom_site.label_atom_id',
  '_atom_site.label_comp_id',
  '_atom_site.label_asym_id',
  '_atom_site.label_seq_id',
  '_atom_site.auth_seq_id',
  '_atom_site.Cartn_x',
  '_atom_site.Cartn_y',
  '_atom_site.Cartn_z',
  'ATOM 1 CA MET A 1 1 0.5 1.5 2.5',
  'ATOM 2 N  MET A 1 1 9.9 9.9 9.9',
  'ATOM 3 CA GLY A 2 2 4.5 5.5 6.5',
  'HETATM 4 C1 LIG L . 1 7.0 7.0 7.0',
  '#'
].join('\n');

describe('parseStructureAtomCoords', () => {
  it('parses PDB ATOM and HETATM records with coordinates', () => {
    const atoms = parseStructureAtomCoords(PDB_TEXT, 'pdb');
    expect(atoms).toHaveLength(6);
    expect(atoms[0]).toMatchObject({ chainId: 'A', residue: 1, atomName: 'N', x: 0, y: 0, z: 0 });
    expect(atoms[5]).toMatchObject({ chainId: 'L', residue: 1, atomName: 'C1' });
  });

  it('parses CIF atom_site loops including het records without seq', () => {
    const atoms = parseStructureAtomCoords(CIF_TEXT, 'cif');
    expect(atoms).toHaveLength(4);
    expect(atoms[0]).toMatchObject({ chainId: 'A', residue: 1, atomName: 'CA', x: 0.5, y: 1.5, z: 2.5 });
    expect(atoms[3]).toMatchObject({ chainId: 'L', atomName: 'C1' });
  });

  it('ignores malformed lines', () => {
    const garbage = parseStructureAtomCoords('ATOM xx bad\nnot a record', 'pdb');
    expect(garbage).toHaveLength(0);
  });
});

describe('extractResidueCAPoints', () => {
  it('keeps one CA per chain:residue key', () => {
    const points = extractResidueCAPoints(PDB_TEXT, 'pdb');
    expect([...points.keys()].sort()).toEqual(['A:1', 'A:2', 'B:3']);
    expect(points.get('A:1')).toMatchObject({ x: 1, y: 0, z: 0 });
  });
});

describe('computePocketBoxFromResiduePicks', () => {
  it('centers on Cα points with span padding', () => {
    const box = computePocketBoxFromResiduePicks(PDB_TEXT, 'pdb', [
      { chainId: 'A', residue: 1 },
      { chainId: 'A', residue: 2 }
    ]);
    expect(box).not.toBeNull();
    // CA at (1,0,0) and (11,4,2): center (6,2,1); span+pad → size max(10+6,18)
    expect(box!.centerX).toBeCloseTo(6);
    expect(box!.centerY).toBeCloseTo(2);
    expect(box!.centerZ).toBeCloseTo(1);
    expect(box!.sizeX).toBeCloseTo(18); // 10 span + 6 pad = 16, clamped to minimum 18
    expect(box!.sizeY).toBeCloseTo(18); // 4 span + 6 pad, clamped to minimum
  });

  it('drops picks with no CA and returns null when none resolve', () => {
    const box = computePocketBoxFromResiduePicks(PDB_TEXT, 'pdb', [{ chainId: 'A', residue: 99 }]);
    expect(box).toBeNull();
  });

  it('returns null for empty picks', () => {
    expect(computePocketBoxFromResiduePicks(PDB_TEXT, 'pdb', [])).toBeNull();
  });
});

describe('computePocketBoxFromLigandStructure', () => {
  it('builds the ligand bounding box', () => {
    const box = computePocketBoxFromLigandStructure(PDB_TEXT, 'pdb');
    // All atoms: x 0..11, y 0..5, z 0..5
    expect(box!.centerX).toBeCloseTo(5.5);
    expect(box!.sizeX).toBeCloseTo(18); // 11 span + 6 pad = 17, clamped to minimum 18
    expect(box!.sizeY).toBeCloseTo(18);
  });

  it('returns null for structures without atoms', () => {
    expect(computePocketBoxFromLigandStructure('HEADER empty', 'pdb')).toBeNull();
  });
});

describe('buildPocketBoxPdb', () => {
  const box: PocketBox = {
    centerX: 0,
    centerY: 0,
    centerZ: 0,
    sizeX: 20,
    sizeY: 10,
    sizeZ: 4
  };

  it('emits 8 corner atoms and 12 CONECT edge records', () => {
    const pdb = buildPocketBoxPdb(box);
    const atoms = pdb.split('\n').filter((l) => l.startsWith('HETATM'));
    const conects = pdb.split('\n').filter((l) => l.startsWith('CONECT'));
    expect(atoms).toHaveLength(8);
    // CONECT lines: each atom has 3 neighbors, so 8 atoms × 3 = 24 bond mentions
    // split across CONECT records (max 4 per line = 1 line per atom since 3 < 4)
    expect(conects).toHaveLength(8);
    // Verify total unique edges = 12 (each edge appears once as source)
    const allBonds = new Set<string>();
    for (const line of conects) {
      const nums = (line.slice(6).match(/\d+/g) || []).map(Number);
      const src = nums[0];
      for (let k = 1; k < nums.length; k++) {
        const dst = nums[k];
        allBonds.add(`${Math.min(src,dst)}-${Math.max(src,dst)}`);
      }
    }
    expect(allBonds.size).toBe(12);
  });

  it('places corners at center ± half-size', () => {
    const pdb = buildPocketBoxPdb(box);
    const atoms = parseStructureAtomCoords(pdb, 'pdb');
    expect(atoms).toHaveLength(8);
    const xs = [...new Set(atoms.map(a => a.x))].sort((a, b) => a - b);
    const zs = [...new Set(atoms.map(a => a.z))].sort((a, b) => a - b);
    expect(xs).toEqual([-10, 10]);
    expect(zs).toEqual([-2, 2]);
  });

  it('round-trips through the PDB atom parser', () => {
    const pdb = buildPocketBoxPdb({ centerX: -4.95, centerY: 14.34, centerZ: -17.79, sizeX: 22, sizeY: 22, sizeZ: 22 });
    const atoms = parseStructureAtomCoords(pdb, 'pdb');
    expect(atoms).toHaveLength(8);
    const mid = (sel: (a: (typeof atoms)[number]) => number) => {
      const values = atoms.map(sel);
      return (Math.min(...values) + Math.max(...values)) / 2;
    };
    expect(mid((a) => a.x)).toBeCloseTo(-4.95, 2);
    expect(mid((a) => a.y)).toBeCloseTo(14.34, 2);
    expect(mid((a) => a.z)).toBeCloseTo(-17.79, 2);
  });

  it('atom names parse correctly from PDB columns', () => {
    const pdb = buildPocketBoxPdb(box);
    const atoms = parseStructureAtomCoords(pdb, 'pdb');
    expect(atoms.map(a => a.atomName).sort()).toEqual(
      ['B001','B002','B003','B004','B005','B006','B007','B008']
    );
  });
});

describe('detectLigands', () => {
  const STRUCTURE_WITH_LIGANDS = [
    'ATOM      1  N   MET A   1       0.000   0.000   0.000  1.00 50.00           N',
    'ATOM      2  CA  MET A   1       1.500   0.000   0.000  1.00 50.00           C',
    'ATOM      3  N   GLY A   2      20.000  20.000  20.000  1.00 50.00           N',
    'ATOM      4  CA  GLY A   2      21.500  20.000  20.000  1.00 50.00           C',
    'HETATM    5  C1  ATP B   1       5.000   5.000   5.000  1.00 50.00           C',
    'HETATM    6  C2  ATP B   1       6.000   5.000   5.000  1.00 50.00           C',
    'HETATM    7  C3  ATP B   1       7.000   5.000   5.000  1.00 50.00           C',
    'HETATM    8  N1  ATP B   1       8.000   5.000   5.000  1.00 50.00           N',
    'HETATM    9  C4  ATP B   1       9.000   5.000   5.000  1.00 50.00           C',
    'HETATM   10  C5  ATP B   1      10.000   5.000   5.000  1.00 50.00           C',
    'HETATM   11  C1  HOH C   1      50.000  50.000  50.000  1.00 50.00           O',
    'HETATM   10  CA  CA  C   2      60.000  60.000  60.000  1.00 50.00          CA',
    'END'
  ].join('\n');

  it('detects non-polymer ligands with ≥6 heavy atoms', () => {
    const ligands = detectLigands(STRUCTURE_WITH_LIGANDS, 'pdb');
    expect(ligands).toHaveLength(1);
    expect(ligands[0].resName).toBe('ATP');
    expect(ligands[0].atomCount).toBe(6);
  });

  it('filters out water, ions, amino acids, nucleotides', () => {
    const ligands = detectLigands(STRUCTURE_WITH_LIGANDS, 'pdb');
    const names = ligands.map(l => l.resName);
    expect(names).not.toContain('HOH');
    expect(names).not.toContain('CA');
    expect(names).not.toContain('MET');
    expect(names).not.toContain('GLY');
  });

  it('returns empty for protein-only structures', () => {
    const proteinOnly = STRUCTURE_WITH_LIGANDS.split('\n')
      .filter(l => l.startsWith('ATOM'))
      .join('\n');
    expect(detectLigands(proteinOnly, 'pdb')).toHaveLength(0);
  });
});

describe('computePocketBoxFromLigandAtoms', () => {
  it('creates box around ligand atoms', () => {
    const box = computePocketBoxFromLigandAtoms({
      chainId: 'B',
      resName: 'ATP',
      resNum: 1,
      coords: [[5,5,5],[7,5,5],[6,7,5],[5,5,7]],
      atomCount: 4,
      label: 'ATP'
    });
    expect(box).not.toBeNull();
    expect(box!.centerX).toBeCloseTo(6);
    expect(box!.sizeX).toBeGreaterThanOrEqual(18); // clamped to min
  });
});

import { computeAutoPocketBox } from './pocketBox';

describe('computeAutoPocketBox', () => {
  const pdbWithLigand = [
    'ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N',
    'ATOM      2  CA  ALA A   1       1.000   0.000   0.000  1.00  0.00           C',
    'HETATM    3  C1  LIG B 999      30.000  30.000  30.000  1.00  0.00           C',
    'HETATM    4  C2  LIG B 999      31.000  30.000  30.000  1.00  0.00           C',
    'HETATM    5  C3  LIG B 999      30.000  31.000  30.000  1.00  0.00           C',
    'HETATM    6  C4  LIG B 999      30.000  30.000  31.000  1.00  0.00           C',
    'HETATM    7  C5  LIG B 999      31.000  31.000  31.000  1.00  0.00           C',
    'HETATM    8  C6  LIG B 999      32.000  30.000  30.000  1.00  0.00           C',
    'END'
  ].join('\n');
  const pdbProteinOnly = [
    'ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N',
    'ATOM      2  CA  ALA A   1      40.000  40.000  40.000  1.00  0.00           C',
    'END'
  ].join('\n');

  it('boxes the co-crystallized ligand pocket when one exists', () => {
    const result = computeAutoPocketBox(pdbWithLigand, 'pdb');
    expect(result?.method).toBe('ligand');
    expect(result?.ligandLabel).toBe('LIG');
    expect(result?.box.centerX).toBeCloseTo(31, 1);
    // Generous minimum: even a small ligand gets a searchable box.
    expect(result?.box.sizeX).toBeGreaterThanOrEqual(22);
  });

  it('falls back to the whole-protein box (large, blind docking)', () => {
    const result = computeAutoPocketBox(pdbProteinOnly, 'pdb');
    expect(result?.method).toBe('manual');
    expect(result?.ligandLabel).toBeNull();
    expect(result?.box.centerX).toBeCloseTo(20, 1);
    expect(result?.box.sizeX).toBeGreaterThan(40);
  });

  it('returns null when the structure has no atoms', () => {
    expect(computeAutoPocketBox('END', 'pdb')).toBeNull();
  });
});

describe('pocketTargetChanged', () => {
  it('keeps the box while a restored target preview is still loading', () => {
    // Restoring a saved task: the file name lands before the async 3D preview
    // fills in — that transition must not discard the persisted pocket.
    expect(pocketTargetChanged(null, { name: 't.pdb', length: 0 })).toBe(false);
    expect(pocketTargetChanged({ name: 't.pdb', length: 0 }, { name: 't.pdb', length: 52310 })).toBe(false);
  });

  it('keeps the box when the same file re-registers (run-driven)', () => {
    expect(pocketTargetChanged({ name: 't.pdb', length: 52310 }, { name: 't.pdb', length: 52310 })).toBe(false);
  });

  it('clears the box when a different target is loaded', () => {
    expect(pocketTargetChanged({ name: 'a.pdb', length: 100 }, { name: 'b.pdb', length: 200 })).toBe(true);
    // Same name, genuinely different content.
    expect(pocketTargetChanged({ name: 'a.pdb', length: 100 }, { name: 'a.pdb', length: 220 })).toBe(true);
  });

  it('clears the box when the target is swapped before either preview loaded', () => {
    expect(pocketTargetChanged({ name: 'a.pdb', length: 0 }, { name: 'b.pdb', length: 0 })).toBe(true);
  });

  it('ignores resets through the empty-target state', () => {
    expect(pocketTargetChanged({ name: 'a.pdb', length: 100 }, { name: '', length: 0 })).toBe(false);
    expect(pocketTargetChanged({ name: '', length: 0 }, { name: 'b.pdb', length: 0 })).toBe(false);
  });
});
