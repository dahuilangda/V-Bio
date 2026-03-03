import type { Dispatch, SetStateAction } from 'react';
import type {
  InputComponent,
  PredictionConstraint,
  ProteinTemplateUpload
} from '../../types/models';
import type { ConstraintResiduePick } from '../../components/project/ConstraintEditor';
import type { MolstarResiduePick } from '../../components/project/MolstarViewer';
import {
  handleRuntimePeptideBicyclicCys1PosChangeAction,
  handleRuntimePeptideBicyclicCys2PosChangeAction,
  handleRuntimePeptideBicyclicCys3PosChangeAction,
  handleRuntimePeptideBicyclicCysPositionModeChangeAction,
  handleRuntimePeptideBicyclicFixTerminalCysChangeAction,
  handleRuntimePeptideBicyclicIncludeExtraCysChangeAction,
  handleRuntimePeptideBicyclicLinkerCcdChangeAction,
  handlePredictionComponentsChangeAction,
  handlePredictionProteinTemplateChangeAction,
  handlePredictionTemplateResiduePickAction,
  handleRuntimeBackendChangeAction,
  handleRuntimePeptideBinderLengthChangeAction,
  handleRuntimePeptideDesignModeChangeAction,
  handleRuntimePeptideEliteSizeChangeAction,
  handleRuntimePeptideInitialSequenceChangeAction,
  handleRuntimePeptideIterationsChangeAction,
  handleRuntimePeptideSequenceMaskChangeAction,
  handleRuntimePeptideMutationRateChangeAction,
  handleRuntimePeptidePopulationSizeChangeAction,
  handleRuntimePeptideUseInitialSequenceChangeAction,
  handleRuntimeSeedChangeAction,
  handleTaskNameChangeAction,
  handleTaskSummaryChangeAction
} from './editorActions';
import type { ProjectWorkspaceDraft } from './workspaceTypes';

interface UseProjectEditorHandlersParams<TDraft extends ProjectWorkspaceDraft> {
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
  setPickedResidue: Dispatch<SetStateAction<ConstraintResiduePick | null>>;
  setProteinTemplates: Dispatch<SetStateAction<Record<string, ProteinTemplateUpload>>>;
  filterConstraintsByBackend: (
    constraints: PredictionConstraint[],
    backend: string
  ) => PredictionConstraint[];
}

export interface UseProjectEditorHandlersResult {
  handlePredictionComponentsChange: (components: InputComponent[]) => void;
  handlePredictionProteinTemplateChange: (componentId: string, upload: ProteinTemplateUpload | null) => void;
  handlePredictionTemplateResiduePick: (pick: MolstarResiduePick) => void;
  handleRuntimeBackendChange: (backend: string) => void;
  handleRuntimeSeedChange: (seed: number | null) => void;
  handleRuntimePeptideDesignModeChange: (mode: 'linear' | 'cyclic' | 'bicyclic') => void;
  handleRuntimePeptideBinderLengthChange: (value: number) => void;
  handleRuntimePeptideUseInitialSequenceChange: (value: boolean) => void;
  handleRuntimePeptideInitialSequenceChange: (value: string) => void;
  handleRuntimePeptideSequenceMaskChange: (value: string) => void;
  handleRuntimePeptideIterationsChange: (value: number) => void;
  handleRuntimePeptidePopulationSizeChange: (value: number) => void;
  handleRuntimePeptideEliteSizeChange: (value: number) => void;
  handleRuntimePeptideMutationRateChange: (value: number) => void;
  handleRuntimePeptideBicyclicLinkerCcdChange: (value: 'SEZ' | '29N' | 'BS3') => void;
  handleRuntimePeptideBicyclicCysPositionModeChange: (value: 'auto' | 'manual') => void;
  handleRuntimePeptideBicyclicFixTerminalCysChange: (value: boolean) => void;
  handleRuntimePeptideBicyclicIncludeExtraCysChange: (value: boolean) => void;
  handleRuntimePeptideBicyclicCys1PosChange: (value: number) => void;
  handleRuntimePeptideBicyclicCys2PosChange: (value: number) => void;
  handleRuntimePeptideBicyclicCys3PosChange: (value: number) => void;
  handleTaskNameChange: (value: string) => void;
  handleTaskSummaryChange: (value: string) => void;
}

export function useProjectEditorHandlers<TDraft extends ProjectWorkspaceDraft>({
  setDraft,
  setPickedResidue,
  setProteinTemplates,
  filterConstraintsByBackend
}: UseProjectEditorHandlersParams<TDraft>): UseProjectEditorHandlersResult {
  const handlePredictionComponentsChange = (components: InputComponent[]) => {
    handlePredictionComponentsChangeAction({
      components,
      setDraft
    });
  };

  const handlePredictionProteinTemplateChange = (componentId: string, upload: ProteinTemplateUpload | null) => {
    handlePredictionProteinTemplateChangeAction({
      componentId,
      upload,
      setPickedResidue,
      setProteinTemplates
    });
  };

  const handlePredictionTemplateResiduePick = (pick: MolstarResiduePick) => {
    handlePredictionTemplateResiduePickAction({
      pick,
      setPickedResidue
    });
  };

  const handleRuntimeBackendChange = (backend: string) => {
    handleRuntimeBackendChangeAction({
      backend,
      setDraft,
      filterConstraintsByBackend
    });
  };

  const handleRuntimeSeedChange = (seed: number | null) => {
    handleRuntimeSeedChangeAction({
      seed,
      setDraft
    });
  };

  const handleRuntimePeptideDesignModeChange = (mode: 'linear' | 'cyclic' | 'bicyclic') => {
    handleRuntimePeptideDesignModeChangeAction({
      peptideDesignMode: mode,
      setDraft
    });
  };

  const handleRuntimePeptideBinderLengthChange = (value: number) => {
    handleRuntimePeptideBinderLengthChangeAction({
      peptideBinderLength: value,
      setDraft
    });
  };

  const handleRuntimePeptideUseInitialSequenceChange = (value: boolean) => {
    handleRuntimePeptideUseInitialSequenceChangeAction({
      peptideUseInitialSequence: value,
      setDraft
    });
  };

  const handleRuntimePeptideInitialSequenceChange = (value: string) => {
    handleRuntimePeptideInitialSequenceChangeAction({
      peptideInitialSequence: value,
      setDraft
    });
  };

  const handleRuntimePeptideSequenceMaskChange = (value: string) => {
    handleRuntimePeptideSequenceMaskChangeAction({
      peptideSequenceMask: value,
      setDraft
    });
  };

  const handleRuntimePeptideIterationsChange = (value: number) => {
    handleRuntimePeptideIterationsChangeAction({
      peptideIterations: value,
      setDraft
    });
  };

  const handleRuntimePeptidePopulationSizeChange = (value: number) => {
    handleRuntimePeptidePopulationSizeChangeAction({
      peptidePopulationSize: value,
      setDraft
    });
  };

  const handleRuntimePeptideEliteSizeChange = (value: number) => {
    handleRuntimePeptideEliteSizeChangeAction({
      peptideEliteSize: value,
      setDraft
    });
  };

  const handleRuntimePeptideMutationRateChange = (value: number) => {
    handleRuntimePeptideMutationRateChangeAction({
      peptideMutationRate: value,
      setDraft
    });
  };

  const handleRuntimePeptideBicyclicLinkerCcdChange = (value: 'SEZ' | '29N' | 'BS3') => {
    handleRuntimePeptideBicyclicLinkerCcdChangeAction({
      peptideBicyclicLinkerCcd: value,
      setDraft
    });
  };

  const handleRuntimePeptideBicyclicCysPositionModeChange = (value: 'auto' | 'manual') => {
    handleRuntimePeptideBicyclicCysPositionModeChangeAction({
      peptideBicyclicCysPositionMode: value,
      setDraft
    });
  };

  const handleRuntimePeptideBicyclicFixTerminalCysChange = (value: boolean) => {
    handleRuntimePeptideBicyclicFixTerminalCysChangeAction({
      peptideBicyclicFixTerminalCys: value,
      setDraft
    });
  };

  const handleRuntimePeptideBicyclicIncludeExtraCysChange = (value: boolean) => {
    handleRuntimePeptideBicyclicIncludeExtraCysChangeAction({
      peptideBicyclicIncludeExtraCys: value,
      setDraft
    });
  };

  const handleRuntimePeptideBicyclicCys1PosChange = (value: number) => {
    handleRuntimePeptideBicyclicCys1PosChangeAction({
      peptideBicyclicCys1Pos: value,
      setDraft
    });
  };

  const handleRuntimePeptideBicyclicCys2PosChange = (value: number) => {
    handleRuntimePeptideBicyclicCys2PosChangeAction({
      peptideBicyclicCys2Pos: value,
      setDraft
    });
  };

  const handleRuntimePeptideBicyclicCys3PosChange = (value: number) => {
    handleRuntimePeptideBicyclicCys3PosChangeAction({
      peptideBicyclicCys3Pos: value,
      setDraft
    });
  };

  const handleTaskNameChange = (value: string) => {
    handleTaskNameChangeAction({
      value,
      setDraft
    });
  };

  const handleTaskSummaryChange = (value: string) => {
    handleTaskSummaryChangeAction({
      value,
      setDraft
    });
  };

  return {
    handlePredictionComponentsChange,
    handlePredictionProteinTemplateChange,
    handlePredictionTemplateResiduePick,
    handleRuntimeBackendChange,
    handleRuntimeSeedChange,
    handleRuntimePeptideDesignModeChange,
    handleRuntimePeptideBinderLengthChange,
    handleRuntimePeptideUseInitialSequenceChange,
    handleRuntimePeptideInitialSequenceChange,
    handleRuntimePeptideSequenceMaskChange,
    handleRuntimePeptideIterationsChange,
    handleRuntimePeptidePopulationSizeChange,
    handleRuntimePeptideEliteSizeChange,
    handleRuntimePeptideMutationRateChange,
    handleRuntimePeptideBicyclicLinkerCcdChange,
    handleRuntimePeptideBicyclicCysPositionModeChange,
    handleRuntimePeptideBicyclicFixTerminalCysChange,
    handleRuntimePeptideBicyclicIncludeExtraCysChange,
    handleRuntimePeptideBicyclicCys1PosChange,
    handleRuntimePeptideBicyclicCys2PosChange,
    handleRuntimePeptideBicyclicCys3PosChange,
    handleTaskNameChange,
    handleTaskSummaryChange
  };
}
