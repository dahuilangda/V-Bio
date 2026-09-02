import type { CSSProperties, KeyboardEvent, PointerEvent, RefObject } from 'react';
import { AffinityBasicsWorkspace } from '../../components/project/AffinityWorkspace';
import type { AffinityDockPocket, AffinityScoringMode } from '../../types/models';

export interface AffinityWorkflowSectionProps {
  visible: boolean;
  canEdit: boolean;
  submitting: boolean;
  backend: string;
  mode: AffinityScoringMode;
  dockPocket: AffinityDockPocket | null;
  seed: number | null;
  targetFileName: string;
  ligandFileName: string;
  ligandSmiles: string;
  ligandEditorInput: string;
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
  onConfidenceOnlyChange: (value: boolean) => void;
  onBackendChange: (backend: string) => void;
  onModeChange: (mode: AffinityScoringMode) => void;
  onDockPocketChange: (pocket: AffinityDockPocket | null) => void;
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
  dockPocket,
  seed,
  targetFileName,
  ligandFileName,
  ligandSmiles,
  ligandEditorInput,
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
  onConfidenceOnlyChange,
  onBackendChange,
  onModeChange,
  onDockPocketChange,
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
      dockPocket={dockPocket}
      seed={seed}
      targetFileName={targetFileName}
      ligandFileName={ligandFileName}
      ligandSmiles={ligandSmiles}
      ligandEditorInput={ligandEditorInput}
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
      onConfidenceOnlyChange={onConfidenceOnlyChange}
      onBackendChange={onBackendChange}
      onModeChange={onModeChange}
      onDockPocketChange={onDockPocketChange}
      onSeedChange={onSeedChange}
      onLigandSmilesChange={onLigandSmilesChange}
      onResizerPointerDown={onResizerPointerDown}
      onResizerKeyDown={onResizerKeyDown}
    />
  );
}
