import { describe, expect, it } from 'vitest';
import { normalizeProjectInputConfig } from './projectInputs';
import type { ProjectInputConfig } from '../types/models';

function peptideConfig(options: Record<string, unknown>): ProjectInputConfig {
  return {
    version: 1,
    components: [],
    constraints: [],
    properties: { affinity: false },
    options: { seed: 42, ...options }
  } as unknown as ProjectInputConfig;
}

describe('normalizeProjectInputConfig length window', () => {
  it('keeps an open length range (min !== max) — the backend needs both keys for adaptive design', () => {
    // Regression: the range keys used to be stripped here, so an open range
    // silently submitted as a fixed default length and the sequence mask was
    // dropped for length mismatch on the backend.
    const normalized = normalizeProjectInputConfig(
      peptideConfig({ peptideLengthMin: 8, peptideLengthMax: 12 })
    );
    expect(normalized.options.peptideLengthMin).toBe(8);
    expect(normalized.options.peptideLengthMax).toBe(12);
    expect(normalized.options.peptideBinderLength).toBeUndefined();
  });

  it('collapses a locked range (min === max) to the legacy single length', () => {
    const normalized = normalizeProjectInputConfig(
      peptideConfig({ peptideLengthMin: 14, peptideLengthMax: 14 })
    );
    expect(normalized.options.peptideBinderLength).toBe(14);
    expect(normalized.options.peptideLengthMin).toBe(14);
    expect(normalized.options.peptideLengthMax).toBe(14);
  });

  it('defaults a fully-unset length to the fixed default, not a window', () => {
    const normalized = normalizeProjectInputConfig(peptideConfig({}));
    expect(normalized.options.peptideLengthMin).toBe(20);
    expect(normalized.options.peptideLengthMax).toBe(20);
    expect(normalized.options.peptideBinderLength).toBe(20);
  });
});
