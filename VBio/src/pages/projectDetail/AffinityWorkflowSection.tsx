import type { CSSProperties, KeyboardEvent, PointerEvent, RefObject } from 'react';
import { AffinityBasicsWorkspace } from '../../components/project/AffinityWorkspace';

export interface AffinityWorkflowSectionProps {
  visible: boolean;
  canEdit: boolean;
  submitting: boolean;
  backend: string;
  targetFileName: string;
  ligandFileName: string;
  ligandSmiles: string;
  ligandEditorInput: string;
  useMsa: boolean;
  confidenceOnly: boolean;
  confidenceOnlyLocked: boolean;
  confidenceOnlyHint: string;
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
  onLigandSmilesChange: (value: string) => void;
  onResizerPointerDown: (event: PointerEvent<HTMLDivElement>) => void;
  onResizerKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
}

export function AffinityWorkflowSection({
  visible,
  canEdit,
  submitting,
  backend,
  targetFileName,
  ligandFileName,
  ligandSmiles,
  ligandEditorInput,
  useMsa,
  confidenceOnly,
  confidenceOnlyLocked,
  confidenceOnlyHint,
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
      targetFileName={targetFileName}
      ligandFileName={ligandFileName}
      ligandSmiles={ligandSmiles}
      ligandEditorInput={ligandEditorInput}
      useMsa={useMsa}
      confidenceOnly={confidenceOnly}
      confidenceOnlyLocked={confidenceOnlyLocked}
      confidenceOnlyHint={confidenceOnlyHint}
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
      onLigandSmilesChange={onLigandSmilesChange}
      onResizerPointerDown={onResizerPointerDown}
      onResizerKeyDown={onResizerKeyDown}
    />
  );
}
