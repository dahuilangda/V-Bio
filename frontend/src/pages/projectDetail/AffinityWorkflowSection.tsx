import type { CSSProperties, KeyboardEvent, PointerEvent, RefObject } from 'react';
import { AffinityBasicsWorkspace } from '../../components/project/AffinityWorkspace';
import type { AffinityDockPocket, AffinityScoringMode } from '../../types/models';

export interface AffinityWorkflowSectionProps {
  isVisible: boolean;
  isEditable: boolean;
  isSubmitting: boolean;
  backend: string;
  mode: AffinityScoringMode;
  dockPocket: AffinityDockPocket | null;
  seed: number | null;
  targetFileName: string;
  ligandFileName: string;
  ligandSmiles: string;
  ligandEditorInput: string;
  isConfidenceOnly: boolean;
  isConfidenceOnlyLocked: boolean;
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
  isDockBlind: boolean;
  onDockBlindChange: (blind: boolean) => void;
  onSeedChange: (seed: number | null) => void;
  onLigandSmilesChange: (value: string) => void;
  onResizerPointerDown: (event: PointerEvent<HTMLDivElement>) => void;
  onResizerKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
}

export function AffinityWorkflowSection({
  isVisible,
  isEditable,
  isSubmitting,
  backend,
  mode,
  dockPocket,
  seed,
  targetFileName,
  ligandFileName,
  ligandSmiles,
  ligandEditorInput,
  isConfidenceOnly,
  isConfidenceOnlyLocked,
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
  isDockBlind,
  onDockBlindChange,
  onSeedChange,
  onLigandSmilesChange,
  onResizerPointerDown,
  onResizerKeyDown
}: AffinityWorkflowSectionProps) {
  if (!isVisible) return null;

  return (
    <AffinityBasicsWorkspace
      isEditable={isEditable}
      isSubmitting={isSubmitting}
      backend={backend}
      mode={mode}
      dockPocket={dockPocket}
      isDockBlind={isDockBlind}
      onDockBlindChange={onDockBlindChange}
      seed={seed}
      targetFileName={targetFileName}
      ligandFileName={ligandFileName}
      ligandSmiles={ligandSmiles}
      ligandEditorInput={ligandEditorInput}
      isConfidenceOnly={isConfidenceOnly}
      isConfidenceOnlyLocked={isConfidenceOnlyLocked}
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
