import { describe, it, expect } from 'vitest';
import { buildSnapshotCards } from './resultPresentation';
import {
  readInterfaceMetricIpsaeChannel,
  resolvePreferredInterfaceMetric,
  resolvePreferredInterfaceMetricFromValues
} from './projectMetrics';

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
    ligandIpsaeMax: 0.81,
    interfaceMetric: null
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

// The D-peptide design loop reports its refined interface score through the
// unified interface_metric channel (interface_metric + interface_metric_label
// 'IPSAE'); the legacy ligand_ipsae_max / ipsae_dom keys stay absent. The
// detail/metrics resolvers must surface it as IPSAE instead of degrading to
// ipTM (regression: 2026-09-04 demo run showed ipTM where ipSAE belonged).
describe('interface_metric channel — ipSAE display', () => {
  const DPEPTIDE_CONFIDENCE = {
    iptm: 0.6547,
    pair_iptm: 0.6547,
    interface_metric: 0.3812953531742096,
    interface_metric_label: 'IPSAE',
    chain_mean_plddt: { A: 94.2, B: 82.3 }
  };

  it('readInterfaceMetricIpsaeChannel picks up the labeled channel value', () => {
    expect(readInterfaceMetricIpsaeChannel(DPEPTIDE_CONFIDENCE)).toBeCloseTo(0.3813, 4);
    expect(readInterfaceMetricIpsaeChannel({ interface_metric: 0.5 })).toBeNull();
    expect(readInterfaceMetricIpsaeChannel({ interface_metric: 0.5, interface_metric_label: 'IPTM' })).toBeNull();
    expect(readInterfaceMetricIpsaeChannel(null)).toBeNull();
  });

  it('resolver prefers the interface_metric channel over ipTM when legacy keys are absent', () => {
    const resolved = resolvePreferredInterfaceMetricFromValues({
      pairIptm: 0.6547,
      iptm: 0.6547,
      ipsaeDom: null,
      ligandIpsaeMax: null,
      interfaceMetric: readInterfaceMetricIpsaeChannel(DPEPTIDE_CONFIDENCE)
    });
    expect(resolved.source).toBe('ipsae');
    expect(resolved.label).toBe('IPSAE');
    expect(resolved.kind).toBe('interface_metric');
    expect(resolved.value).toBeCloseTo(0.3813, 4);
  });

  it('legacy keys still outrank the unified channel', () => {
    const resolved = resolvePreferredInterfaceMetricFromValues({
      pairIptm: null,
      iptm: 0.7,
      ipsaeDom: 0.66,
      ligandIpsaeMax: 0.9,
      interfaceMetric: 0.38
    });
    expect(resolved.kind).toBe('ligand_ipsae');
    expect(resolved.value).toBe(0.9);
  });

  it('confidence-based resolver renders the IPSAE card for the D-peptide payload', () => {
    const resolved = resolvePreferredInterfaceMetric(DPEPTIDE_CONFIDENCE, 'A', 'B', ['A', 'B']);
    expect(resolved.source).toBe('ipsae');
    expect(resolved.value).toBeCloseTo(0.3813, 4);
    const cards = buildSnapshotCards({ ...BASE, preferredInterfaceMetric: resolved });
    const interfaceCard = cards.find((c) => c.key === 'ipsae');
    expect(interfaceCard).toBeDefined();
    expect(interfaceCard!.label).toBe('IPSAE');
    expect(interfaceCard!.value).toBe('0.3813');
  });
});
