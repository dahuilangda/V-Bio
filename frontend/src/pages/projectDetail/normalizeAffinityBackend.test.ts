import { describe, it, expect } from 'vitest';
import { normalizeAffinityBackend } from '../apiAccessHelpers';

describe('normalizeAffinityBackend', () => {
  it('defaults to boltz', () => {
    expect(normalizeAffinityBackend(undefined)).toBe('boltz');
    expect(normalizeAffinityBackend(null)).toBe('boltz');
    expect(normalizeAffinityBackend('')).toBe('boltz');
    expect(normalizeAffinityBackend('   ')).toBe('boltz');
    expect(normalizeAffinityBackend('boltz')).toBe('boltz');
    expect(normalizeAffinityBackend('Boltz-2')).toBe('boltz');
    expect(normalizeAffinityBackend('unknown-backend')).toBe('boltz');
  });

  it('resolves protenix aliases case-insensitively', () => {
    expect(normalizeAffinityBackend('protenix')).toBe('protenix');
    expect(normalizeAffinityBackend('Protenix')).toBe('protenix');
    expect(normalizeAffinityBackend('protenix2dock')).toBe('protenix');
    expect(normalizeAffinityBackend('p2d')).toBe('protenix');
  });
});