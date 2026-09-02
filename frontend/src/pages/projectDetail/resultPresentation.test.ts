import { describe, it, expect } from 'vitest';
import { buildSnapshotCards } from './resultPresentation';

const BASE = {
  snapshotPlddt: 88,
  snapshotSelectedLigandChainPlddt: null,
  snapshotLigandMeanPlddt: 79,
  snapshotPlddtTone: 'good' as const,
  preferredInterfaceMetric: {
    kind: 'ipsae_dom' as const,
    label: 'IPSAE' as const,
    source: 'ipsae' as const,
    value: 0.72,
    tone: 'good' as const,
    pairIptm: null,
    iptm: 0.9,
    ipsaeDom: 0.72,
    ligandIpsaeMax: 0.81
  },
  snapshotIc50Um: 0.4,
  snapshotIc50Error: null,
  snapshotIc50Tone: 'good' as const,
  snapshotBindingProbability: 0.62,
  snapshotBindingStd: null,
  snapshotBindingTone: 'medium' as const,
  selectedResultTargetLabel: 'A',
  selectedResultLigandLabel: 'L',
  selectedResultPairLabel: 'A–L'
};

describe('buildSnapshotCards — pIC50 card', () => {
  it('adds a pIC50 card with MW-corrected detail when provided', () => {
    const cards = buildSnapshotCards({ ...BASE, snapshotPic50: 6.42, snapshotPic50Mw: 6.71 });
    const pic50 = cards.find((c) => c.key === 'pic50');
    expect(pic50).toBeDefined();
    expect(pic50!.value).toBe('6.42');
    expect(pic50!.detail).toContain('6.71');
    // ordering: plddt, interface, pic50, ic50, binding
    expect(cards.map((c) => c.key)).toEqual(['plddt', 'ipsae', 'pic50', 'ic50', 'binding']);
  });

  it('shows a dash without pIC50 and keeps legacy cards intact', () => {
    const cards = buildSnapshotCards({ ...BASE });
    const pic50 = cards.find((c) => c.key === 'pic50');
    expect(pic50!.value).toBe('-');
    expect(pic50!.detail).toBe('-log10 IC50 (M)');
    expect(cards.find((c) => c.key === 'ic50')!.value).toBe('0.40');
  });
});
