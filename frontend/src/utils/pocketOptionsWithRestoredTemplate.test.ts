import { describe, expect, it } from 'vitest';
import { pocketOptionsWithRestoredTemplate } from './peptidePocket';

describe('pocketOptionsWithRestoredTemplate', () => {
  it('keeps structure-dependent picks when the template restored', () => {
    const options = { peptidePocketResidues: 'A:50,A:91', peptidePocketCenter: null as string | null };
    expect(pocketOptionsWithRestoredTemplate(options, true)).toEqual(options);
  });

  it('drops chain-prefixed picks and centers without a template, keeping plain picks', () => {
    expect(pocketOptionsWithRestoredTemplate(
      { peptidePocketResidues: 'A:50,51,A:91', peptidePocketCenter: '1,2,3' }, false
    )).toEqual({ peptidePocketResidues: '51', peptidePocketCenter: null });
  });

  it('passes through plain-only and empty definitions untouched', () => {
    expect(pocketOptionsWithRestoredTemplate(
      { peptidePocketResidues: '50,51', peptidePocketCenter: null }, false
    )).toEqual({ peptidePocketResidues: '50,51', peptidePocketCenter: null });
    expect(pocketOptionsWithRestoredTemplate(
      { peptidePocketResidues: null, peptidePocketCenter: null }, false
    )).toEqual({ peptidePocketResidues: null, peptidePocketCenter: null });
  });
});
