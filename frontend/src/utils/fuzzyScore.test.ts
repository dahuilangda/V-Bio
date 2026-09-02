import { describe, expect, it } from 'vitest';
import { fuzzyRank, fuzzyScore } from './fuzzyScore';

describe('fuzzyScore (cmdk command-score port)', () => {
  it('scores a continuous prefix match highest', () => {
    expect(fuzzyScore('structureParser', 'str')).toBeGreaterThan(fuzzyScore('structureParser', 'sxp'));
  });

  it('drops non-matches to zero and ranks matches above them', () => {
    expect(fuzzyScore('DHODH', 'zzz')).toBe(0);
    expect(fuzzyScore('result-viewer.tsx', 'rvv')).toBeGreaterThan(0);
  });

  it('prefers word-boundary jumps over character jumps', () => {
    expect(fuzzyScore('use-project-detail', 'upd')).toBeGreaterThan(fuzzyScore('unnecessary-detail', 'upd'));
  });

  it('scores an adjacent transposition as a match with a penalty (cmdk contract)', () => {
    // 'acb' against 'abc': the c-b swap is recognized mid-string, penalized but matched.
    expect(fuzzyScore('abc', 'acb')).toBeGreaterThan(0);
    expect(fuzzyScore('abc', 'acb')).toBeLessThan(fuzzyScore('abc', 'abc'));
  });
});

describe('fuzzyRank (mention-menu ranking)', () => {
  const items = ['2BDG-klk4.cif', 'ibuprofen.smi', 'dhodh-model.cif', 'notes.txt'];

  it('keeps matches only, best first, stable on ties', () => {
    expect(fuzzyRank(items, 'cif', (x) => x)).toEqual(['2BDG-klk4.cif', 'dhodh-model.cif']);
    expect(fuzzyRank(items, 'dhodh', (x) => x)).toEqual(['dhodh-model.cif']);
    expect(fuzzyRank(items, 'zzz', (x) => x)).toEqual([]);
  });

  it('ranks subsequence queries across separators (cmdk gap-jump behavior)', () => {
    // 'dmc' hits dhodh-model.cif via the hyphen word jump; no other item matches.
    expect(fuzzyRank(items, 'dmc', (x) => x)).toEqual(['dhodh-model.cif']);
  });
});
