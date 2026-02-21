import type { ReactNode } from 'react';
import { Ligand2DPreview } from '../../components/project/Ligand2DPreview';
import { ensureStructureConfidenceColoringData } from '../../api/backendApi';
import type { InputComponent } from '../../types/models';
import type { MetricTone } from './projectMetrics';
import { buildSnapshotCards, resolveAffinityResultLigandSmiles } from './resultPresentation';
import { componentTypeLabel } from '../../utils/projectInputs';
import { isSequenceLigandType, OverviewLigandSequencePreview } from './OverviewLigandSequencePreview';

interface UseProjectResultDisplayOptions {
  structureText: string;
  structureFormat: 'cif' | 'pdb';
  confidenceBackend: string;
  projectBackend: string;
  activeResultTaskStructureName: string;
  projectStructureName: string;
  draftColorMode: string;
  hasAf3ConfidenceSignals: boolean;
  hasProtenixConfidenceSignals: boolean;
  selectedTemplatePreviewContent: string;
  selectedTemplatePreviewFormat: 'cif' | 'pdb';
  selectedResultTargetChainId: string | null;
  selectedResultLigandChainId: string | null;
  resultChainShortLabelById: Map<string, string>;
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
  affinityPreviewTargetStructureText: string;
  affinityPreviewTargetStructureFormat: 'cif' | 'pdb';
  affinityPreviewLigandStructureText: string;
  affinityPreviewLigandStructureFormat: 'cif' | 'pdb';
  snapshotAffinity: Record<string, unknown> | null;
  snapshotConfidence: Record<string, unknown> | null;
  statusContextLigandSmiles: string;
  activeResultLigandSmiles: string;
  snapshotLigandAtomPlddts: number[];
  affinityLigandSmiles: string;
  overviewPrimaryLigand: {
    isSmiles: boolean;
    smiles: string;
    selectedComponentType: InputComponent['type'] | null;
  };
  selectedResultLigandSequence: string;
  selectedResultLigandComponentType: InputComponent['type'] | null;
  snapshotLigandResiduePlddts: number[] | null;
}

interface UseProjectResultDisplayResult {
  displayStructureText: string;
  displayStructureFormat: 'cif' | 'pdb';
  displayStructureName: string;
  displayStructureColorMode: 'default' | 'alphafold';
  constraintStructureText: string;
  constraintStructureFormat: 'cif' | 'pdb';
  hasConstraintStructure: boolean;
  snapshotCards: Array<{ key: string; label: string; value: string; detail: string; tone: MetricTone }>;
  affinityPreviewStructureText: string;
  affinityPreviewStructureFormat: 'cif' | 'pdb';
  affinityPreviewLigandOverlayText: string;
  affinityPreviewLigandOverlayFormat: 'cif' | 'pdb';
  affinityResultLigandSmiles: string;
  predictionLigandPreview: ReactNode;
  predictionLigandRadarSmiles: string;
  affinityDisplayStructureText: string;
  affinityDisplayStructureFormat: 'cif' | 'pdb';
  hasAffinityDisplayStructure: boolean;
}

export function useProjectResultDisplay({
  structureText,
  structureFormat,
  confidenceBackend,
  projectBackend,
  activeResultTaskStructureName,
  projectStructureName,
  draftColorMode,
  hasAf3ConfidenceSignals,
  hasProtenixConfidenceSignals,
  selectedTemplatePreviewContent,
  selectedTemplatePreviewFormat,
  selectedResultTargetChainId,
  selectedResultLigandChainId,
  resultChainShortLabelById,
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
  affinityPreviewTargetStructureText,
  affinityPreviewTargetStructureFormat,
  affinityPreviewLigandStructureText,
  affinityPreviewLigandStructureFormat,
  snapshotAffinity,
  snapshotConfidence,
  statusContextLigandSmiles,
  activeResultLigandSmiles,
  snapshotLigandAtomPlddts,
  affinityLigandSmiles,
  overviewPrimaryLigand,
  selectedResultLigandSequence,
  selectedResultLigandComponentType,
  snapshotLigandResiduePlddts,
}: UseProjectResultDisplayOptions): UseProjectResultDisplayResult {
  const displayStructureText = ensureStructureConfidenceColoringData(
    structureText,
    structureFormat,
    confidenceBackend || projectBackend
  );
  const displayStructureFormat: 'cif' | 'pdb' = structureFormat;
  const displayStructureName = activeResultTaskStructureName || projectStructureName || '-';
  const displayStructureColorMode: 'default' | 'alphafold' =
    projectBackend === 'alphafold3' ||
    projectBackend === 'protenix' ||
    draftColorMode === 'alphafold' ||
    confidenceBackend === 'alphafold3' ||
    hasAf3ConfidenceSignals ||
    hasProtenixConfidenceSignals
      ? 'alphafold'
      : 'default';

  const constraintStructureText = selectedTemplatePreviewContent || '';
  const constraintStructureFormat: 'cif' | 'pdb' = selectedTemplatePreviewFormat || 'pdb';
  const hasConstraintStructure = Boolean(constraintStructureText.trim());

  const selectedResultTargetLabel = selectedResultTargetChainId
    ? resultChainShortLabelById.get(selectedResultTargetChainId) || selectedResultTargetChainId
    : 'Comp 1';
  const selectedResultLigandLabel = selectedResultLigandChainId
    ? resultChainShortLabelById.get(selectedResultLigandChainId) || selectedResultLigandChainId
    : 'Comp 1';
  const selectedResultPairLabel = `${selectedResultTargetLabel} ↔ ${selectedResultLigandLabel}`;

  const snapshotCards: Array<{ key: string; label: string; value: string; detail: string; tone: MetricTone }> =
    buildSnapshotCards({
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
    });

  const affinityPreviewStructureText = affinityPreviewTargetStructureText;
  const affinityPreviewStructureFormat: 'cif' | 'pdb' = affinityPreviewTargetStructureFormat;
  const affinityPreviewLigandOverlayText = affinityPreviewLigandStructureText;
  const affinityPreviewLigandOverlayFormat: 'cif' | 'pdb' = affinityPreviewLigandStructureFormat;

  const affinityResultLigandSmiles = resolveAffinityResultLigandSmiles({
    snapshotAffinity,
    snapshotConfidence,
    selectedResultLigandChainId,
    statusContextLigandSmiles,
    activeResultLigandSmiles,
    snapshotLigandAtomPlddts,
    affinityLigandSmiles,
  });

  const predictionLigandPreviewSmiles = (
    affinityResultLigandSmiles.trim() ||
    (overviewPrimaryLigand.isSmiles ? overviewPrimaryLigand.smiles : '')
  ).trim();

  const affinityDisplayStructureText = displayStructureText.trim() ? displayStructureText : affinityPreviewStructureText;
  const affinityDisplayStructureFormat: 'cif' | 'pdb' = displayStructureText.trim()
    ? displayStructureFormat
    : affinityPreviewStructureFormat;
  const hasAffinityDisplayStructure = Boolean(affinityDisplayStructureText.trim());

  const predictionLigandPreview =
    predictionLigandPreviewSmiles ? (
      <Ligand2DPreview
        smiles={predictionLigandPreviewSmiles}
        atomConfidences={snapshotLigandAtomPlddts}
        confidenceHint={snapshotPlddt}
      />
    ) : selectedResultLigandSequence && isSequenceLigandType(selectedResultLigandComponentType || null) ? (
      <OverviewLigandSequencePreview sequence={selectedResultLigandSequence} residuePlddts={snapshotLigandResiduePlddts} />
    ) : (
      <div className="ligand-preview-empty">
        {overviewPrimaryLigand.selectedComponentType && overviewPrimaryLigand.selectedComponentType !== 'ligand'
          ? `Selected binding ligand component is ${componentTypeLabel(overviewPrimaryLigand.selectedComponentType)}.`
          : overviewPrimaryLigand.smiles
            ? '2D preview requires SMILES input.'
            : 'No ligand input.'}
      </div>
    );

  return {
    displayStructureText,
    displayStructureFormat,
    displayStructureName,
    displayStructureColorMode,
    constraintStructureText,
    constraintStructureFormat,
    hasConstraintStructure,
    snapshotCards,
    affinityPreviewStructureText,
    affinityPreviewStructureFormat,
    affinityPreviewLigandOverlayText,
    affinityPreviewLigandOverlayFormat,
    affinityResultLigandSmiles,
    predictionLigandPreview,
    predictionLigandRadarSmiles: predictionLigandPreviewSmiles,
    affinityDisplayStructureText,
    affinityDisplayStructureFormat,
    hasAffinityDisplayStructure,
  };
}
