import { describe, expect, it } from 'vitest';
import { pocketOptionsWithRestoredTemplate } from './peptidePocket';

describe('pocket serialization at submission', () => {
  const stale = {
    peptidePocketResidues: 'A:50,A:51,A:52,A:53,A:54,A:55,A:56,A:57,A:58,A:59,A:61,A:62,A:65,A:67,A:68,A:71,A:72,A:73,A:74,A:75,A:91,A:93,A:94,A:95,A:96,A:97,A:99,A:100,A:103',
    peptidePocketCenter: null as string | null,
    peptidePocketBox: null as number | null
  };

  it('sequence-only target (no structure) drops chain-prefixed picks entirely', () => {
    const out = pocketOptionsWithRestoredTemplate(stale, false);
    expect(out.peptidePocketResidues).toBeNull();
    expect(out.peptidePocketCenter).toBeNull();
  });

  it('structure target keeps the author-numbered picks', () => {
    const out = pocketOptionsWithRestoredTemplate(stale, true);
    expect(out.peptidePocketResidues).toBe(stale.peptidePocketResidues);
  });

  it('mixed stale picks + fresh sequence picks keep only the sequence picks', () => {
    const out = pocketOptionsWithRestoredTemplate(
      { ...stale, peptidePocketResidues: 'A:50,12,13' }, false);
    expect(out.peptidePocketResidues).toBe('12,13');
  });
});
