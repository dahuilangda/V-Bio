import { describe, it, expect } from 'vitest';
import {
  aminoAcidOptionLabel,
  parsePocketResidueTokens,
  formatPlainPocketPositions,
  togglePocketPosition,
  pocketRadiusFromBox,
  pocketCenterString,
  peptidePocketSummaryLabel,
  peptidePocketTargetSignature,
  peptidePocketTargetChanged,
  formatPocketResiduePicks,
  pocketSubmissionFieldsFromBox
} from './peptidePocket';

describe('peptidePocketTargetSignature / peptidePocketTargetChanged', () => {
  const seqSig = (componentId: string, sequence: string) =>
    peptidePocketTargetSignature({ componentId, hasStructure: false, sequence });
  const tplSig = (componentId: string, fileName: string, length: number) =>
    peptidePocketTargetSignature({ componentId, hasStructure: true, fileName, contentLength: length });

  it('hydration of the same component template keeps the pocket (memory)', () => {
    expect(peptidePocketTargetChanged(seqSig('c1', 'MKT'), tplSig('c1', 'target.pdb', 9000))).toBe(false);
  });

  it('first sight never invalidates (restored task mounting)', () => {
    expect(peptidePocketTargetChanged('', tplSig('c1', 'target.pdb', 9000))).toBe(false);
    expect(peptidePocketTargetChanged('', seqSig('c1', 'MKT'))).toBe(false);
  });

  it('switching the target component invalidates', () => {
    expect(peptidePocketTargetChanged(seqSig('c1', 'MKT'), seqSig('c2', 'MKT'))).toBe(true);
    expect(peptidePocketTargetChanged(tplSig('c1', 'a.pdb', 10), tplSig('c2', 'a.pdb', 10))).toBe(true);
  });

  it('rebuilding or removing the structure invalidates', () => {
    expect(peptidePocketTargetChanged(tplSig('c1', 'a.pdb', 100), tplSig('c1', 'b.pdb', 200))).toBe(true);
    expect(peptidePocketTargetChanged(tplSig('c1', 'a.pdb', 100), seqSig('c1', 'MKT'))).toBe(true);
  });

  it('editing the target sequence invalidates sequence-position picks', () => {
    expect(peptidePocketTargetChanged(seqSig('c1', 'MKT'), seqSig('c1', 'MKTAY'))).toBe(true);
    expect(peptidePocketTargetChanged(seqSig('c1', 'MKT'), seqSig('c1', 'mkt'))).toBe(false);
  });
});

describe('aminoAcidOptionLabel', () => {
  it('labels constraint-style sequence positions with three-letter names', () => {
    expect(aminoAcidOptionLabel(25, 'E')).toBe('25 · GLU');
    expect(aminoAcidOptionLabel(1, 'M')).toBe('1 · MET');
  });

  it('falls back to the raw letter for unknown residues', () => {
    expect(aminoAcidOptionLabel(9, 'X')).toBe('9 · X');
    expect(aminoAcidOptionLabel(9, '')).toBe('9 · ?');
  });
});

describe('peptidePocketSummaryLabel', () => {
  it('counts residue picks across both numbering systems', () => {
    expect(peptidePocketSummaryLabel('', 'A:152,A:153,25')).toBe('3 residues');
    expect(peptidePocketSummaryLabel('', '25')).toBe('1 residue');
  });

  it('reports center boxes and the empty pocket', () => {
    expect(peptidePocketSummaryLabel('1.2,3,4', '')).toBe('center + radius');
    expect(peptidePocketSummaryLabel(null, null)).toBe('whole surface');
  });
});

describe('parsePocketResidueTokens', () => {
  it('splits chain-prefixed tokens from plain positions', () => {
    const parsed = parsePocketResidueTokens('A:152, 26 ,b:7');
    expect(parsed.chainContacts).toEqual([
      { chain: 'A', residue: 152 },
      { chain: 'b', residue: 7 }
    ]);
    expect(parsed.plainPositions).toEqual([26]);
  });

  it('drops malformed tokens', () => {
    const parsed = parsePocketResidueTokens('A:x, :3, 0, -5, 12,');
    expect(parsed.chainContacts).toEqual([]);
    expect(parsed.plainPositions).toEqual([12]);
  });

  it('handles empty input', () => {
    const parsed = parsePocketResidueTokens(null);
    expect(parsed.chainContacts).toEqual([]);
    expect(parsed.plainPositions).toEqual([]);
  });
});

describe('formatPlainPocketPositions', () => {
  it('sorts, de-duplicates and joins', () => {
    expect(formatPlainPocketPositions([30, 25, 25, 27])).toBe('25,27,30');
  });

  it('drops non-positive values', () => {
    expect(formatPlainPocketPositions([3, 0, -1])).toBe('3');
  });
});

describe('togglePocketPosition', () => {
  it('removes an existing position', () => {
    expect(togglePocketPosition([25, 26], 25)).toEqual([26]);
  });

  it('adds a missing position', () => {
    expect(togglePocketPosition([25], 26)).toEqual([25, 26]);
  });
});

describe('pocketRadiusFromBox', () => {
  it('uses half the longest edge', () => {
    expect(pocketRadiusFromBox({ sizeX: 22, sizeY: 16, sizeZ: 30 })).toBe(15);
  });

  it('clamps into the 4-40 range', () => {
    expect(pocketRadiusFromBox({ sizeX: 6, sizeY: 6, sizeZ: 6 })).toBe(4);
    expect(pocketRadiusFromBox({ sizeX: 100, sizeY: 100, sizeZ: 100 })).toBe(40);
  });
});

describe('pocketCenterString', () => {
  it('rounds to one decimal', () => {
    expect(pocketCenterString({ centerX: 12.26, centerY: -3.51, centerZ: 20 })).toBe('12.3,-3.5,20');
  });
});

describe('formatPocketResiduePicks', () => {
  it('uses the target chain with raw author numbering, de-duplicated', () => {
    const picks = [
      { chainId: 'X', residue: 152 },
      { chainId: 'X', residue: 152 },
      { chainId: 'X', residue: 153 }
    ];
    expect(formatPocketResiduePicks('A', picks)).toBe('A:152,A:153');
  });

  it('falls back to chain A and drops invalid residues', () => {
    expect(formatPocketResiduePicks(null, [{ chainId: 'X', residue: 0 }])).toBe('');
    expect(formatPocketResiduePicks('  ', [{ chainId: 'X', residue: 7 }])).toBe('A:7');
  });
});

describe('pocketSubmissionFieldsFromBox', () => {
  it('residues-method box with picks submits the residue list only', () => {
    const fields = pocketSubmissionFieldsFromBox(
      'A',
      { centerX: 1, centerY: 2, centerZ: 3, sizeX: 20, sizeY: 20, sizeZ: 20, method: 'residues' },
      [{ chainId: 'X', residue: 152 }]
    );
    expect(fields).toEqual({
      peptidePocketCenter: null,
      peptidePocketResidues: 'A:152',
      peptidePocketBox: null
    });
  });

  it('manual/ligand box submits center + radius', () => {
    const fields = pocketSubmissionFieldsFromBox(
      'A',
      { centerX: 1.24, centerY: 2, centerZ: 3, sizeX: 22, sizeY: 16, sizeZ: 16, method: 'manual' },
      []
    );
    expect(fields).toEqual({
      peptidePocketCenter: '1.2,2,3',
      peptidePocketResidues: null,
      peptidePocketBox: 11
    });
  });

  it('manual box with stale picks still submits center + radius', () => {
    const fields = pocketSubmissionFieldsFromBox(
      'A',
      { centerX: 0, centerY: 0, centerZ: 0, sizeX: 20, sizeY: 20, sizeZ: 20, method: 'manual' },
      [{ chainId: 'X', residue: 10 }]
    );
    expect(fields.peptidePocketCenter).toBe('0,0,0');
    expect(fields.peptidePocketResidues).toBeNull();
  });

  it('null pocket clears everything', () => {
    const fields = pocketSubmissionFieldsFromBox('A', null, []);
    expect(fields).toEqual({
      peptidePocketCenter: null,
      peptidePocketResidues: null,
      peptidePocketBox: null
    });
  });
});
