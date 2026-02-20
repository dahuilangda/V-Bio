import type { CSSProperties, KeyboardEvent, PointerEvent, RefObject } from 'react';
import { AffinityBasicsWorkspace } from '../../components/project/AffinityWorkspace';

export interface AffinityWorkflowSectionProps {
  visible: boolean;
  canEdit: boolean;
  submitting: boolean;
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
  resultsGridRef: RefObject<HTMLDivElement>;
  isResultsResizing: boolean;
  resultsGridStyle: CSSProperties;
  onTargetFileChange: (file: File | null) => void;
  onLigandFileChange: (file: File | null) => void;
  onUseMsaChange: (value: boolean) => void;
  onConfidenceOnlyChange: (value: boolean) => void;
  onLigandSmilesChange: (value: string) => void;
  onResizerPointerDown: (event: PointerEvent<HTMLDivElement>) => void;
  onResizerKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
}

export function AffinityWorkflowSection({
  visible,
  canEdit,
  submitting,
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
  resultsGridRef,
  isResultsResizing,
  resultsGridStyle,
  onTargetFileChange,
  onLigandFileChange,
  onUseMsaChange,
  onConfidenceOnlyChange,
  onLigandSmilesChange,
  onResizerPointerDown,
  onResizerKeyDown
}: AffinityWorkflowSectionProps) {
  if (!visible) return null;

  return (
    <AffinityBasicsWorkspace
      canEdit={canEdit}
      submitting={submitting}
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
      resultsGridRef={resultsGridRef}
      isResultsResizing={isResultsResizing}
      resultsGridStyle={resultsGridStyle}
      onTargetFileChange={onTargetFileChange}
      onLigandFileChange={onLigandFileChange}
      onUseMsaChange={onUseMsaChange}
      onConfidenceOnlyChange={onConfidenceOnlyChange}
      onLigandSmilesChange={onLigandSmilesChange}
      onResizerPointerDown={onResizerPointerDown}
      onResizerKeyDown={onResizerKeyDown}
    />
  );
}
