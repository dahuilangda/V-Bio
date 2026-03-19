import type { MetricTone, PreferredInterfaceMetric } from './projectMetrics';
import { readFirstNonEmptyStringMetric, readLigandSmilesFromMap } from './projectMetrics';
import {
  readAlignedLigandAtomPlddtsFromConfidence,
  readAlignedLigandSmilesFromConfidence,
  readLigandDisplayAtomPlddtsFromConfidence,
  readLigandRenderSmilesFromConfidence
} from './projectConfidence';

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
  preferredInterfaceMetric: PreferredInterfaceMetric;
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
    preferredInterfaceMetric,
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
      key: preferredInterfaceMetric.source === 'ipsae' ? 'ipsae' : 'iptm',
      label: preferredInterfaceMetric.label,
      value: preferredInterfaceMetric.value === null ? '-' : preferredInterfaceMetric.value.toFixed(4),
      detail:
        preferredInterfaceMetric.kind === 'ligand_ipsae'
          ? 'Best ligand contact IPSAE'
          : preferredInterfaceMetric.kind === 'ipsae_dom'
            ? 'Interface IPSAE'
            : preferredInterfaceMetric.pairIptm !== null && selectedResultTargetLabel !== selectedResultLigandLabel
          ? selectedResultPairLabel
            : 'Interface conf',
      tone: preferredInterfaceMetric.tone
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
  const fromConfidenceDisplay = readLigandRenderSmilesFromConfidence(snapshotConfidence);
  const fromAffinityDisplay = readLigandRenderSmilesFromConfidence(snapshotAffinity);
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
      fromConfidenceDisplay ||
      fromAffinityDisplay ||
      fromConfidenceMap ||
      fromConfidenceMetrics ||
      fromAffinityMap ||
      fromAffinityMetrics ||
      fromTaskRows ||
      affinityLigandSmiles.trim()
    );
  }
  return (
    fromConfidenceDisplay ||
    fromAffinityDisplay ||
    fromTaskRows ||
    fromAffinityMap ||
    fromConfidenceMap ||
    fromAffinityMetrics ||
    fromConfidenceMetrics ||
    affinityLigandSmiles.trim()
  );
}

export function resolveExactLigandConfidencePreview(params: {
  snapshotAffinity: Record<string, unknown> | null;
  snapshotConfidence: Record<string, unknown> | null;
  selectedResultLigandChainId: string | null;
  statusContextLigandSmiles: string;
  activeResultLigandSmiles: string;
  affinityLigandSmiles: string;
}): { smiles: string; atomPlddts: number[] } {
  const {
    snapshotAffinity,
    snapshotConfidence,
    selectedResultLigandChainId,
    statusContextLigandSmiles,
    activeResultLigandSmiles,
    affinityLigandSmiles
  } = params;

  const exactDisplayContracts = [
    {
      smiles: readLigandRenderSmilesFromConfidence(snapshotConfidence),
      atomPlddts: readLigandDisplayAtomPlddtsFromConfidence(snapshotConfidence, selectedResultLigandChainId)
    },
    {
      smiles: readLigandRenderSmilesFromConfidence(snapshotAffinity),
      atomPlddts: readLigandDisplayAtomPlddtsFromConfidence(snapshotAffinity, selectedResultLigandChainId)
    }
  ];
  for (const contract of exactDisplayContracts) {
    if (contract.smiles.trim() && contract.atomPlddts.length > 0) {
      return {
        smiles: contract.smiles.trim(),
        atomPlddts: contract.atomPlddts
      };
    }
  }

  const exactAlignedContracts = [
    {
      smiles: readAlignedLigandSmilesFromConfidence(snapshotConfidence),
      atomPlddts: readAlignedLigandAtomPlddtsFromConfidence(snapshotConfidence, selectedResultLigandChainId)
    },
    {
      smiles: readAlignedLigandSmilesFromConfidence(snapshotAffinity),
      atomPlddts: readAlignedLigandAtomPlddtsFromConfidence(snapshotAffinity, selectedResultLigandChainId)
    }
  ];
  for (const contract of exactAlignedContracts) {
    if (contract.smiles.trim() && contract.atomPlddts.length > 0) {
      return {
        smiles: contract.smiles.trim(),
        atomPlddts: contract.atomPlddts
      };
    }
  }

  return {
    smiles: resolveAffinityResultLigandSmiles({
      snapshotAffinity,
      snapshotConfidence,
      selectedResultLigandChainId,
      statusContextLigandSmiles,
      activeResultLigandSmiles,
      snapshotLigandAtomPlddts: [],
      affinityLigandSmiles
    }),
    atomPlddts: []
  };
}
