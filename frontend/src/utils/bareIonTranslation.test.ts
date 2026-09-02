import { describe, it, expect } from 'vitest';
import { bareIonCcdFromSmiles } from './yaml';

describe('bareIonCcdFromSmiles', () => {
  it('extracts element codes from bracketed single-atom SMILES', () => {
    expect(bareIonCcdFromSmiles('[Fe]')).toBe('Fe');
    expect(bareIonCcdFromSmiles('[Zn]')).toBe('Zn');
    expect(bareIonCcdFromSmiles('[Na+]')).toBe('Na');
    expect(bareIonCcdFromSmiles('[Cl-]')).toBe('Cl');
    expect(bareIonCcdFromSmiles('[Ca+2]')).toBe('Ca');
    expect(bareIonCcdFromSmiles(' [Mg] ')).toBe('Mg');
  });

  it('returns null for multi-atom molecules and organic SMILES', () => {
    expect(bareIonCcdFromSmiles('CCO')).toBeNull();
    expect(bareIonCcdFromSmiles('c1ccccc1')).toBeNull();
    expect(bareIonCcdFromSmiles('[Fe]C')).toBeNull();
    expect(bareIonCcdFromSmiles('[Na+].[Cl-]')).toBeNull();
    expect(bareIonCcdFromSmiles('')).toBeNull();
  });
});
