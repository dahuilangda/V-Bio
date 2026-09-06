import type { Dispatch, SetStateAction } from 'react';
import type {
  AffinityDockPocket,
  InputComponent,
  PeptideResiduePoolSelection,
  PredictionConstraint,
  ProteinTemplateUpload
} from '../../types/models';
import type { ConstraintResiduePick } from '../../components/project/ConstraintEditor';
import type { MolstarResiduePick } from '../../components/project/MolstarViewer';
import {
  handleRuntimePeptideBicyclicCys1PosChangeAction,
  handleRuntimePeptideBicyclicCys2PosChangeAction,
  handleRuntimePeptideBicyclicCys3PosChangeAction,
  handleRuntimePeptideBicyclicCysLayoutChangeAction,
  handleRuntimePeptideBicyclicRingChangeAction,
  handleRuntimePeptideBicyclicRatioChangeAction,
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
  handleRuntimePeptideChiralityChangeAction,
  handleRuntimePeptideStructureUploadChangeAction,
  handleRuntimePeptideEliteSizeChangeAction,
  handleRuntimePeptideInitialSequenceChangeAction,
  handleRuntimePeptideIterationsChangeAction,
  handleRuntimePeptideSequenceMaskChangeAction,
  handleRuntimePeptideNonNaturalRangeChangeAction,
  handleRuntimePeptidePopulationSizeChangeAction,
  handleRuntimePeptideResiduePoolChangeAction,
  handleRuntimePeptideUseInitialSequenceChangeAction,
  handleRuntimeSeedChangeAction,
  handleRuntimePeptidePocketFieldChangeAction,
  handleRuntimePeptideDockPocketChangeAction,
  handleRuntimeLeadOptDockPocketChangeAction,
  handleRuntimeLeadOptOptionChangeAction,
  handleRuntimeLowVramChangeAction,
  handleTaskNameChangeAction,
  handleTaskSummaryChangeAction,
  handleRuntimePeptideLengthRangeAction
} from './editorActions';
import type { ProjectWorkspaceDraft } from './workspaceTypes';

interface UseProjectEditorHandlersParams<TDraft extends ProjectWorkspaceDraft> {
  isPeptideDesignWorkflow: boolean;
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
  handleRuntimeLowVramChange: (lowVram: boolean) => void;
  handleRuntimePeptideDesignModeChange: (mode: 'linear' | 'cyclic' | 'bicyclic') => void;
  handleRuntimePeptideChiralityChange: (chirality: 'l' | 'd') => void;
  handleRuntimePeptideStructureUploadChange: (upload: {
    fileName: string; format: 'pdb' | 'cif'; content: string; chainId: string;
  } | null) => void;
  handleRuntimePeptideBinderLengthChange: (value: number) => void;
  handleRuntimePeptideLengthRange: (min: number, max: number) => void;
  handleRuntimePeptideUseInitialSequenceChange: (value: boolean) => void;
  handleRuntimePeptideInitialSequenceChange: (value: string) => void;
  handleRuntimePeptideSequenceMaskChange: (value: string) => void;
  handleRuntimePeptideIterationsChange: (value: number) => void;
  handleRuntimePeptidePopulationSizeChange: (value: number) => void;
  handleRuntimePeptideEliteSizeChange: (value: number) => void;
  handleRuntimePeptidePocketFieldChange: (field: 'peptidePocketCenter' | 'peptidePocketResidues' | 'peptidePocketBox', value: string | number | null) => void;
  handleRuntimePeptideDockPocketChange: (pocket: AffinityDockPocket | null) => void;
  handleRuntimeLeadOptOptionChange: (
    key: 'leadOptMode' | 'leadOptBackend' | 'leadOptRounds' | 'leadOptBudgetPerRound' | 'leadOptScaffoldHopRatio'
      | 'leadOptPocketCenter' | 'leadOptReferenceSmiles' | 'leadOptKeepFragmentSmiles' | 'leadOptEditAtomIndices',
    value: string | number | null
  ) => void;
  handleRuntimeLeadOptDockPocketChange: (pocket: AffinityDockPocket | null) => void;
  handleRuntimePeptideResiduePoolChange: (value: PeptideResiduePoolSelection[]) => void;
  handleRuntimePeptideNonNaturalRangeChange: (min: number, max: number) => void;
  handleRuntimePeptideBicyclicLinkerCcdChange: (value: 'SEZ' | '29N' | 'BS3') => void;
  handleRuntimePeptideBicyclicCysPositionModeChange: (value: 'auto' | 'manual') => void;
  handleRuntimePeptideBicyclicFixTerminalCysChange: (value: boolean) => void;
  handleRuntimePeptideBicyclicIncludeExtraCysChange: (value: boolean) => void;
  handleRuntimePeptideBicyclicCys1PosChange: (value: number) => void;
  handleRuntimePeptideBicyclicCysLayoutChange: (value: 'auto' | 'ring' | 'ratio' | 'absolute') => void;
  handleRuntimePeptideBicyclicRingChange: (ring1: number, ring2: number) => void;
  handleRuntimePeptideBicyclicRatioChange: (pct1: number, pct2: number, pct3?: number) => void;
  handleRuntimePeptideBicyclicCys2PosChange: (value: number) => void;
  handleRuntimePeptideBicyclicCys3PosChange: (value: number) => void;
  handleTaskNameChange: (value: string) => void;
  handleTaskSummaryChange: (value: string) => void;
}

export function useProjectEditorHandlers<TDraft extends ProjectWorkspaceDraft>({
  isPeptideDesignWorkflow,
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
      isPeptideDesignWorkflow,
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

  const handleRuntimePeptidePocketFieldChange = (
    field: 'peptidePocketCenter' | 'peptidePocketResidues' | 'peptidePocketBox',
    value: string | number | null
  ) => {
    handleRuntimePeptidePocketFieldChangeAction({ field, value, setDraft });
  };

  const handleRuntimePeptideDockPocketChange = (pocket: AffinityDockPocket | null) => {
    handleRuntimePeptideDockPocketChangeAction({ pocket, setDraft });
  };

  const handleRuntimeLeadOptOptionChange = (
    key: 'leadOptMode' | 'leadOptBackend' | 'leadOptRounds' | 'leadOptBudgetPerRound' | 'leadOptScaffoldHopRatio'
      | 'leadOptPocketCenter' | 'leadOptReferenceSmiles' | 'leadOptKeepFragmentSmiles' | 'leadOptEditAtomIndices',
    value: string | number | null
  ) => {
    handleRuntimeLeadOptOptionChangeAction({ key, value, setDraft });
  };

  const handleRuntimeLeadOptDockPocketChange = (pocket: AffinityDockPocket | null) => {
    handleRuntimeLeadOptDockPocketChangeAction({ pocket, setDraft });
  };

  const handleRuntimeLowVramChange = (lowVram: boolean) => {
    handleRuntimeLowVramChangeAction({
      lowVram,
      setDraft
    });
  };

  const handleRuntimePeptideDesignModeChange = (mode: 'linear' | 'cyclic' | 'bicyclic') => {
    handleRuntimePeptideDesignModeChangeAction({
      peptideDesignMode: mode,
      setDraft
    });
  };

  const handleRuntimePeptideChiralityChange = (chirality: 'l' | 'd') => {
    handleRuntimePeptideChiralityChangeAction({
      peptideChirality: chirality,
      setDraft
    });
  };

  const handleRuntimePeptideStructureUploadChange = (upload: {
    fileName: string; format: 'pdb' | 'cif'; content: string; chainId: string;
  } | null) => {
    handleRuntimePeptideStructureUploadChangeAction({ upload, setDraft });
  };

  const handleRuntimePeptideBinderLengthChange = (value: number) => {
    handleRuntimePeptideBinderLengthChangeAction({
      peptideBinderLength: value,
      setDraft
    });
  };

  const handleRuntimePeptideLengthRange = (min: number, max: number) => {
    handleRuntimePeptideLengthRangeAction({
      peptideLengthMin: min,
      peptideLengthMax: max,
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

  const handleRuntimePeptideResiduePoolChange = (value: PeptideResiduePoolSelection[]) => {
    handleRuntimePeptideResiduePoolChangeAction({
      peptideResiduePool: value,
      setDraft
    });
  };

  const handleRuntimePeptideNonNaturalRangeChange = (min: number, max: number) => {
    handleRuntimePeptideNonNaturalRangeChangeAction({
      min,
      max,
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

  const handleRuntimePeptideBicyclicCysLayoutChange = (value: 'auto' | 'ring' | 'ratio' | 'absolute') => {
    handleRuntimePeptideBicyclicCysLayoutChangeAction({
      peptideBicyclicCysLayout: value,
      setDraft
    });
  };

  const handleRuntimePeptideBicyclicRingChange = (ring1: number, ring2: number) => {
    handleRuntimePeptideBicyclicRingChangeAction({
      peptideBicyclicRing1: ring1,
      peptideBicyclicRing2: ring2,
      setDraft
    });
  };

  const handleRuntimePeptideBicyclicRatioChange = (pct1: number, pct2: number, pct3?: number) => {
    handleRuntimePeptideBicyclicRatioChangeAction({
      peptideBicyclicRatio1: pct1,
      peptideBicyclicRatio2: pct2,
      peptideBicyclicRatio3: pct3,
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
    handleRuntimeLowVramChange,
    handleRuntimePeptideDesignModeChange,
    handleRuntimePeptideChiralityChange,
    handleRuntimePeptideStructureUploadChange,
    handleRuntimePeptideBinderLengthChange,
    handleRuntimePeptideLengthRange,
    handleRuntimePeptideUseInitialSequenceChange,
    handleRuntimePeptideInitialSequenceChange,
    handleRuntimePeptideSequenceMaskChange,
    handleRuntimePeptideIterationsChange,
    handleRuntimePeptidePocketFieldChange,
    handleRuntimePeptideDockPocketChange,
    handleRuntimeLeadOptOptionChange,
    handleRuntimeLeadOptDockPocketChange,
    handleRuntimePeptidePopulationSizeChange,
    handleRuntimePeptideEliteSizeChange,
    handleRuntimePeptideResiduePoolChange,
    handleRuntimePeptideNonNaturalRangeChange,
    handleRuntimePeptideBicyclicLinkerCcdChange,
    handleRuntimePeptideBicyclicCysPositionModeChange,
    handleRuntimePeptideBicyclicFixTerminalCysChange,
    handleRuntimePeptideBicyclicIncludeExtraCysChange,
    handleRuntimePeptideBicyclicCys1PosChange,
    handleRuntimePeptideBicyclicCysLayoutChange,
    handleRuntimePeptideBicyclicRingChange,
    handleRuntimePeptideBicyclicRatioChange,
    handleRuntimePeptideBicyclicCys2PosChange,
    handleRuntimePeptideBicyclicCys3PosChange,
    handleTaskNameChange,
    handleTaskSummaryChange
  };
}
