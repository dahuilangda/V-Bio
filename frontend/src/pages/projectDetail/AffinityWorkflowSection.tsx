import type { CSSProperties, KeyboardEvent, PointerEvent, RefObject } from 'react';
import { AffinityBasicsWorkspace } from '../../components/project/AffinityWorkspace';
import type { AffinityScoringMode } from '../../types/models';

export interface AffinityWorkflowSectionProps {
  visible: boolean;
  canEdit: boolean;
  submitting: boolean;
  backend: string;
  mode: AffinityScoringMode;
  seed: number | null;
  targetFileName: string;
  ligandFileName: string;
  ligandSmiles: string;
  ligandEditorInput: string;
  useMsa: boolean;
  confidenceOnly: boolean;
  confidenceOnlyLocked: boolean;
  previewTargetStructureText: string;
  previewTargetStructureFormat: 'cif' | 'pdb';
  previewLigandStructureText: string;
  previewLigandStructureFormat: 'cif' | 'pdb';
  previewLigandChainId: string;
  resultsGridRef: RefObject<HTMLDivElement>;
  isResultsResizing: boolean;
  resultsGridStyle: CSSProperties;
  onTargetFileChange: (file: File | null) => void;
  onLigandFileChange: (file: File | null) => void;
  onUseMsaChange: (value: boolean) => void;
  onConfidenceOnlyChange: (value: boolean) => void;
  onBackendChange: (backend: string) => void;
  onModeChange: (mode: AffinityScoringMode) => void;
  onSeedChange: (seed: number | null) => void;
  onLigandSmilesChange: (value: string) => void;
  onResizerPointerDown: (event: PointerEvent<HTMLDivElement>) => void;
  onResizerKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
}

export function AffinityWorkflowSection({
  visible,
  canEdit,
  submitting,
  backend,
  mode,
  seed,
  targetFileName,
  ligandFileName,
  ligandSmiles,
  ligandEditorInput,
  useMsa,
  confidenceOnly,
  confidenceOnlyLocked,
  previewTargetStructureText,
  previewTargetStructureFormat,
  previewLigandStructureText,
  previewLigandStructureFormat,
  previewLigandChainId,
  resultsGridRef,
  isResultsResizing,
  resultsGridStyle,
  onTargetFileChange,
  onLigandFileChange,
  onUseMsaChange,
  onConfidenceOnlyChange,
  onBackendChange,
  onModeChange,
  onSeedChange,
  onLigandSmilesChange,
  onResizerPointerDown,
  onResizerKeyDown
}: AffinityWorkflowSectionProps) {
  if (!visible) return null;

  return (
    <AffinityBasicsWorkspace
      canEdit={canEdit}
      submitting={submitting}
      backend={backend}
      mode={mode}
      seed={seed}
      targetFileName={targetFileName}
      ligandFileName={ligandFileName}
      ligandSmiles={ligandSmiles}
      ligandEditorInput={ligandEditorInput}
      useMsa={useMsa}
      confidenceOnly={confidenceOnly}
      confidenceOnlyLocked={confidenceOnlyLocked}
      previewTargetStructureText={previewTargetStructureText}
      previewTargetStructureFormat={previewTargetStructureFormat}
      previewLigandStructureText={previewLigandStructureText}
      previewLigandStructureFormat={previewLigandStructureFormat}
      previewLigandChainId={previewLigandChainId}
      resultsGridRef={resultsGridRef}
      isResultsResizing={isResultsResizing}
      resultsGridStyle={resultsGridStyle}
      onTargetFileChange={onTargetFileChange}
      onLigandFileChange={onLigandFileChange}
      onUseMsaChange={onUseMsaChange}
      onConfidenceOnlyChange={onConfidenceOnlyChange}
      onBackendChange={onBackendChange}
      onModeChange={onModeChange}
      onSeedChange={onSeedChange}
      onLigandSmilesChange={onLigandSmilesChange}
      onResizerPointerDown={onResizerPointerDown}
      onResizerKeyDown={onResizerKeyDown}
    />
  );
}
