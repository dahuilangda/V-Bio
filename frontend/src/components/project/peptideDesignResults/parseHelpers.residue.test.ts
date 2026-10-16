import { describe, expect, it } from 'vitest';
import { parseCandidateResiduePlddts } from './parseHelpers';

describe('parseCandidateResiduePlddts scale normalization', () => {
  it('rescales 0-1 fractional series to the 0-100 pLDDT scale', () => {
    const row = { residue_plddt: [0.91, 0.85, 0.78, 0.94, 0.62] };
    expect(parseCandidateResiduePlddts(row, 5)).toEqual([91, 85, 78, 94, 62]);
  });

  it('leaves 0-100 series untouched', () => {
    const row = { residue_plddt: [91, 85, 78, 94, 62] };
    expect(parseCandidateResiduePlddts(row, 5)).toEqual([91, 85, 78, 94, 62]);
  });

  it('normalizes by-chain series read via residue_plddt_by_chain', () => {
    const row = { residue_plddt_by_chain: { B: [0.9, 0.72, 0.55] } };
    const series = parseCandidateResiduePlddts(row, 3, 'B');
    // readResiduePlddtByChainSeries already normalizes its list (with float
    // tail) — the assertion pins the scale, not the artifact digits.
    series.forEach((value, index) => expect(value).toBeCloseTo([90, 72, 55][index], 6));
  });
});
