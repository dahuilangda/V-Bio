import type { MetricTone } from './projectMetrics';
import { readFirstNonEmptyStringMetric, readLigandSmilesFromMap } from './projectMetrics';

export interface SnapshotCard {
  key: string;
  label: string;
  value: string;
  detail: string;
  tone: MetricTone;
}

export function buildSnapshotCards(params: {
  snapshotPlddt: number | null;
  snapshotSelectedLigandChainPlddt: number | null;
  snapshotLigandMeanPlddt: number | null;
  snapshotPlddtTone: MetricTone;
  snapshotIptm: number | null;
  snapshotSelectedPairIptm: number | null;
  snapshotIptmTone: MetricTone;
  snapshotIc50Um: number | null;
  snapshotIc50Error: { plus: number; minus: number } | null;
  snapshotIc50Tone: MetricTone;
  snapshotBindingProbability: number | null;
  snapshotBindingStd: number | null;
  snapshotBindingTone: MetricTone;
  selectedResultTargetLabel: string;
  selectedResultLigandLabel: string;
  selectedResultPairLabel: string;
}): SnapshotCard[] {
  const {
    snapshotPlddt,
    snapshotSelectedLigandChainPlddt,
    snapshotLigandMeanPlddt,
    snapshotPlddtTone,
    snapshotIptm,
    snapshotSelectedPairIptm,
    snapshotIptmTone,
    snapshotIc50Um,
    snapshotIc50Error,
    snapshotIc50Tone,
    snapshotBindingProbability,
    snapshotBindingStd,
    snapshotBindingTone,
    selectedResultTargetLabel,
    selectedResultLigandLabel,
    selectedResultPairLabel,
  } = params;

  return [
    {
      key: 'plddt',
      label: 'pLDDT',
      value: snapshotPlddt === null ? '-' : snapshotPlddt.toFixed(2),
      detail:
        snapshotSelectedLigandChainPlddt !== null
          ? `${selectedResultLigandLabel} conf`
          : snapshotLigandMeanPlddt !== null
            ? 'Ligand atom conf'
            : 'Complex conf',
      tone: snapshotPlddtTone
    },
    {
      key: 'iptm',
      label: 'ipTM',
      value: snapshotIptm === null ? '-' : snapshotIptm.toFixed(4),
      detail:
        snapshotSelectedPairIptm !== null && selectedResultTargetLabel !== selectedResultLigandLabel
          ? selectedResultPairLabel
          : 'Interface conf',
      tone: snapshotIptmTone
    },
    {
      key: 'ic50',
      label: 'IC50 (uM)',
      value: snapshotIc50Um === null ? '-' : snapshotIc50Um >= 10 ? snapshotIc50Um.toFixed(1) : snapshotIc50Um.toFixed(2),
      detail: snapshotIc50Error
        ? `+${snapshotIc50Error.plus >= 1 ? snapshotIc50Error.plus.toFixed(2) : snapshotIc50Error.plus.toFixed(3)} / -${
            snapshotIc50Error.minus >= 1 ? snapshotIc50Error.minus.toFixed(2) : snapshotIc50Error.minus.toFixed(3)
          }`
        : 'uncertainty: -',
      tone: snapshotIc50Tone
    },
    {
      key: 'binding',
      label: 'Binding',
      value: snapshotBindingProbability === null ? '-' : `${(snapshotBindingProbability * 100).toFixed(1)}%`,
      detail: snapshotBindingStd === null ? 'uncertainty: -' : `± ${(snapshotBindingStd * 100).toFixed(1)}%`,
      tone: snapshotBindingTone
    }
  ];
}

export function resolveAffinityResultLigandSmiles(params: {
  snapshotAffinity: Record<string, unknown> | null;
  snapshotConfidence: Record<string, unknown> | null;
  selectedResultLigandChainId: string | null;
  statusContextLigandSmiles: string;
  activeResultLigandSmiles: string;
  snapshotLigandAtomPlddts: number[];
  affinityLigandSmiles: string;
}): string {
  const {
    snapshotAffinity,
    snapshotConfidence,
    selectedResultLigandChainId,
    statusContextLigandSmiles,
    activeResultLigandSmiles,
    snapshotLigandAtomPlddts,
    affinityLigandSmiles,
  } = params;
  const fromAffinityMap = readLigandSmilesFromMap(snapshotAffinity, selectedResultLigandChainId);
  const fromConfidenceMap = readLigandSmilesFromMap(snapshotConfidence, selectedResultLigandChainId);
  const fromAffinityMetrics = readFirstNonEmptyStringMetric(snapshotAffinity, [
    'ligand_smiles',
    'ligandSmiles',
    'smiles',
    'ligand.smiles'
  ]);
  const fromConfidenceMetrics = readFirstNonEmptyStringMetric(snapshotConfidence, [
    'ligand_smiles',
    'ligandSmiles',
    'smiles',
    'ligand.smiles'
  ]);
  const fromTaskRows = (statusContextLigandSmiles.trim() || activeResultLigandSmiles.trim());
  const preferConfidenceAlignedSmiles = snapshotLigandAtomPlddts.length > 0;
  if (preferConfidenceAlignedSmiles) {
    return (
      fromConfidenceMap ||
      fromConfidenceMetrics ||
      fromAffinityMap ||
      fromAffinityMetrics ||
      fromTaskRows ||
      affinityLigandSmiles.trim()
    );
  }
  return (
    fromTaskRows ||
    fromAffinityMap ||
    fromConfidenceMap ||
    fromAffinityMetrics ||
    fromConfidenceMetrics ||
    affinityLigandSmiles.trim()
  );
}
