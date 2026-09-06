import { describe, it, expect } from 'vitest';
import {
  resolveAnchorsAtLength,
  minFeasibleLength,
  validateCysLayout,
  cysLayoutRangeNotice,
  convertLayoutParams,
  anchorsAcrossRange,
  defaultLayoutParams
} from './peptideCysLayout';
import type { CysLayoutParams } from './peptideCysLayout';

const LAYOUT: CysLayoutParams = defaultLayoutParams(15);

describe('resolveAnchorsAtLength', () => {
  it('ring mode anchors the block to the C-terminus and keeps ring sizes', () => {
    // ring1=4, ring2=6 -> at L=20: cys3=20, cys2=13, cys1=8
    expect(resolveAnchorsAtLength('ring', LAYOUT, 20, true)).toEqual([8, 13, 20]);
    // ring sizes preserved at another length
    expect(resolveAnchorsAtLength('ring', LAYOUT, 25, true)).toEqual([13, 18, 25]);
  });

  it('ring mode refuses lengths below the core size', () => {
    expect(resolveAnchorsAtLength('ring', LAYOUT, 12, true)).toBeNull();
    expect(minFeasibleLength('ring', LAYOUT, true)).toBe(13);
  });

  it('ratio mode scales with length and keeps the terminal anchor', () => {
    const ratio: CysLayoutParams = {
      ...LAYOUT,
      ratio: { pct1: 20, pct2: 50, pct3: 100 }
    };
    expect(resolveAnchorsAtLength('ratio', ratio, 20, true)).toEqual([4, 10, 20]);
    expect(resolveAnchorsAtLength('ratio', ratio, 10, true)).toEqual([2, 5, 10]);
  });

  it('ratio mode forward-fixes spacing on short lengths or refuses', () => {
    const tight: CysLayoutParams = { ...LAYOUT, ratio: { pct1: 40, pct2: 45, pct3: 100 } };
    // L=10: raw p1=4, p2=5 -> pushed to (4, 6, 10)
    expect(resolveAnchorsAtLength('ratio', tight, 10, true)).toEqual([4, 6, 10]);
    // too short to fit after forward fix -> null
    expect(resolveAnchorsAtLength('ratio', tight, 5, true)).toBeNull();
  });

  it('ratio mode uses round-half-up (JS/Python parity)', () => {
    const half: CysLayoutParams = { ...LAYOUT, ratio: { pct1: 25, pct2: 50, pct3: 100 } };
    // L=10 -> 2.5 rounds UP to 3 (Math.round parity via floor(x+0.5))
    expect(resolveAnchorsAtLength('ratio', half, 10, true)).toEqual([3, 5, 10]);
  });

  it('absolute mode honours literal positions and rejects short lengths', () => {
    const abs: CysLayoutParams = {
      ...LAYOUT,
      absolute: { cys1: 3, cys2: 8, cys3: 15 }
    };
    expect(resolveAnchorsAtLength('absolute', abs, 15, true)).toEqual([3, 8, 15]);
    expect(resolveAnchorsAtLength('absolute', abs, 14, true)).toBeNull();
  });

  it('auto mode mirrors the engine first/interior/last layout', () => {
    expect(resolveAnchorsAtLength('auto', LAYOUT, 15, true)).toEqual([1, 8, 15]);
    expect(resolveAnchorsAtLength('auto', LAYOUT, 8, true)).toEqual([1, 4, 8]);
  });
});

describe('validateCysLayout', () => {
  it('accepts ring layout spanning the range and reports the raised floor', () => {
    expect(validateCysLayout({ mode: 'ring', layout: LAYOUT, lengthMin: 13, lengthMax: 25, fixTerminalCys: true })).toBeNull();
    expect(validateCysLayout({ mode: 'ring', layout: LAYOUT, lengthMin: 8, lengthMax: 25, fixTerminalCys: true })).toBeNull();
    expect(validateCysLayout({ mode: 'ring', layout: LAYOUT, lengthMin: 8, lengthMax: 12, fixTerminalCys: true })).toMatch(/超过长度上限/);
    expect(cysLayoutRangeNotice({ mode: 'ring', layout: LAYOUT, lengthMin: 8, lengthMax: 25, fixTerminalCys: true })).toMatch(/实际最小长度为 13/);
    expect(cysLayoutRangeNotice({ mode: 'ring', layout: LAYOUT, lengthMin: 13, lengthMax: 25, fixTerminalCys: true })).toBeNull();
  });

  it('requires a pinned length for absolute layout', () => {
    const abs: CysLayoutParams = { ...LAYOUT, absolute: { cys1: 3, cys2: 8, cys3: 15 } };
    expect(validateCysLayout({ mode: 'absolute', layout: abs, lengthMin: 15, lengthMax: 15, fixTerminalCys: true })).toBeNull();
    expect(validateCysLayout({ mode: 'absolute', layout: abs, lengthMin: 10, lengthMax: 25, fixTerminalCys: true })).toMatch(/固定/);
  });

  it('flags ratio layouts that cannot fit the configured range', () => {
    const ok: CysLayoutParams = { ...LAYOUT, ratio: { pct1: 15, pct2: 50, pct3: 100 } };
    expect(validateCysLayout({ mode: 'ratio', layout: ok, lengthMin: 8, lengthMax: 25, fixTerminalCys: true })).toBeNull();
    const bad: CysLayoutParams = { ...LAYOUT, ratio: { pct1: 90, pct2: 95, pct3: 100 } };
    expect(validateCysLayout({ mode: 'ratio', layout: bad, lengthMin: 8, lengthMax: 12, fixTerminalCys: true })).not.toBeNull();
  });
});

describe('convertLayoutParams', () => {
  it('absolute -> ring keeps the ring topology at the reference length', () => {
    const abs: CysLayoutParams = { ...LAYOUT, absolute: { cys1: 3, cys2: 8, cys3: 15 } };
    const ringed = convertLayoutParams('absolute', 'ring', abs, 15, true);
    expect(ringed.ring).toEqual({ ring1: 4, ring2: 6 });
  });

  it('absolute -> ratio keeps the shape at the reference length', () => {
    const abs: CysLayoutParams = { ...LAYOUT, absolute: { cys1: 3, cys2: 8, cys3: 15 } };
    const ratio = convertLayoutParams('absolute', 'ratio', abs, 15, true);
    expect(ratio.ratio).toEqual({ pct1: 20, pct2: 53, pct3: 100 });
  });
});

describe('anchorsAcrossRange', () => {
  it('marks infeasible lengths as null instead of silently shifting anchors', () => {
    const rows = anchorsAcrossRange('ring', LAYOUT, 8, 16, true);
    expect(rows.find((row) => row.length === 8)?.anchors).toBeNull();
    expect(rows.find((row) => row.length === 13)?.anchors).toEqual([1, 6, 13]);
    expect(rows.find((row) => row.length === 16)?.anchors).toEqual([4, 9, 16]);
  });
});
