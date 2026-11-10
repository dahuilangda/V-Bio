import { useCallback } from 'react';
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
  handleRuntimePeptideStructureModeChangeAction,
  handleRuntimePeptideStructureUploadChangeAction,
  handleRuntimePeptideEliteSizeChangeAction,
  handleRuntimePeptideInitialSequenceChangeAction,
  handleRuntimePeptideIterationsChangeAction,
  handleRuntimePeptideProposerBackendChangeAction,
  handleRuntimePeptideSequenceMaskChangeAction,
  handleRuntimePeptideExcludedAasChangeAction,
  handleRuntimePeptideFixedModificationsChangeAction,
  handleRuntimePeptideNonNaturalRangeChangeAction,
  handleRuntimePeptidePopulationSizeChangeAction,
  handleRuntimePeptideResiduePoolChangeAction,
  handleRuntimePeptideUseInitialSequenceChangeAction,
  handleRuntimeSeedChangeAction,
  handleRuntimeRfd4SpecChangeAction,
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
  handleRuntimeRfd4SpecChange: (value: string) => void;
  handleRuntimeLowVramChange: (lowVram: boolean) => void;
  handleRuntimePeptideDesignModeChange: (mode: 'linear' | 'cyclic' | 'bicyclic') => void;
  handleRuntimePeptideChiralityChange: (chirality: 'l' | 'd') => void;
  handleRuntimePeptideStructureModeChange: (
    mode: 'auto' | 'helix' | 'hairpin' | 'strand_loop'
  ) => void;
  handleRuntimePeptideStructureUploadChange: (upload: {
    fileName: string; format: 'pdb' | 'cif'; content: string; chainId: string;
  } | null) => void;
  handleRuntimePeptideBinderLengthChange: (value: number) => void;
  handleRuntimePeptideLengthRange: (min: number, max: number) => void;
  handleRuntimePeptideUseInitialSequenceChange: (value: boolean) => void;
  handleRuntimePeptideInitialSequenceChange: (value: string) => void;
  handleRuntimePeptideSequenceMaskChange: (value: string) => void;
  handleRuntimePeptideExcludedAasChange: (value: string[]) => void;
  handleRuntimePeptideFixedModificationsChange: (value: { position: number; ccd: string }[]) => void;
  handleRuntimePeptideIterationsChange: (value: number) => void;
  handleRuntimePeptideProposerBackendChange: (value: 'esm3' | 'rfd4') => void;
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
  const handlePredictionComponentsChange = useCallback((components: InputComponent[]) => {
    handlePredictionComponentsChangeAction({
      components,
      setDraft
    });
  }, [setDraft]);

  const handlePredictionProteinTemplateChange = useCallback((componentId: string, upload: ProteinTemplateUpload | null) => {
    handlePredictionProteinTemplateChangeAction({
      componentId,
      upload,
      setPickedResidue,
      setProteinTemplates
    });
  }, [setPickedResidue, setProteinTemplates]);

  const handlePredictionTemplateResiduePick = useCallback((pick: MolstarResiduePick) => {
    handlePredictionTemplateResiduePickAction({
      pick,
      setPickedResidue
    });
  }, [setPickedResidue]);

  const handleRuntimeBackendChange = useCallback((backend: string) => {
    handleRuntimeBackendChangeAction({
      backend,
      isPeptideDesignWorkflow,
      setDraft,
      filterConstraintsByBackend
    });
  }, [filterConstraintsByBackend, isPeptideDesignWorkflow, setDraft]);

  const handleRuntimeSeedChange = useCallback((seed: number | null) => {
    handleRuntimeSeedChangeAction({
      seed,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimeRfd4SpecChange = useCallback((value: string) => {
    handleRuntimeRfd4SpecChangeAction({ value, setDraft });
  }, [setDraft]);

  const handleRuntimePeptidePocketFieldChange = useCallback((
    field: 'peptidePocketCenter' | 'peptidePocketResidues' | 'peptidePocketBox',
    value: string | number | null
  ) => {
    handleRuntimePeptidePocketFieldChangeAction({ field, value, setDraft });
  }, [setDraft]);

  const handleRuntimePeptideDockPocketChange = useCallback((pocket: AffinityDockPocket | null) => {
    handleRuntimePeptideDockPocketChangeAction({ pocket, setDraft });
  }, [setDraft]);

  const handleRuntimeLeadOptOptionChange = useCallback((
    key: 'leadOptMode' | 'leadOptBackend' | 'leadOptRounds' | 'leadOptBudgetPerRound' | 'leadOptScaffoldHopRatio'
      | 'leadOptPocketCenter' | 'leadOptReferenceSmiles' | 'leadOptKeepFragmentSmiles' | 'leadOptEditAtomIndices',
    value: string | number | null
  ) => {
    handleRuntimeLeadOptOptionChangeAction({ key, value, setDraft });
  }, [setDraft]);

  const handleRuntimeLeadOptDockPocketChange = useCallback((pocket: AffinityDockPocket | null) => {
    handleRuntimeLeadOptDockPocketChangeAction({ pocket, setDraft });
  }, [setDraft]);

  const handleRuntimeLowVramChange = useCallback((lowVram: boolean) => {
    handleRuntimeLowVramChangeAction({
      lowVram,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideDesignModeChange = useCallback((mode: 'linear' | 'cyclic' | 'bicyclic') => {
    handleRuntimePeptideDesignModeChangeAction({
      peptideDesignMode: mode,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideChiralityChange = useCallback((chirality: 'l' | 'd') => {
    handleRuntimePeptideChiralityChangeAction({
      peptideChirality: chirality,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideStructureModeChange = useCallback((
    structureMode: 'auto' | 'helix' | 'hairpin' | 'strand_loop'
  ) => {
    handleRuntimePeptideStructureModeChangeAction({
      peptideStructureMode: structureMode,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideStructureUploadChange = useCallback((upload: {
    fileName: string; format: 'pdb' | 'cif'; content: string; chainId: string;
  } | null) => {
    handleRuntimePeptideStructureUploadChangeAction({ upload, setDraft });
  }, [setDraft]);

  const handleRuntimePeptideBinderLengthChange = useCallback((value: number) => {
    handleRuntimePeptideBinderLengthChangeAction({
      peptideBinderLength: value,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideLengthRange = useCallback((min: number, max: number) => {
    handleRuntimePeptideLengthRangeAction({
      peptideLengthMin: min,
      peptideLengthMax: max,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideUseInitialSequenceChange = useCallback((value: boolean) => {
    handleRuntimePeptideUseInitialSequenceChangeAction({
      peptideUseInitialSequence: value,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideInitialSequenceChange = useCallback((value: string) => {
    handleRuntimePeptideInitialSequenceChangeAction({
      peptideInitialSequence: value,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideSequenceMaskChange = useCallback((value: string) => {
    handleRuntimePeptideSequenceMaskChangeAction({
      peptideSequenceMask: value,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideExcludedAasChange = useCallback((value: string[]) => {
    handleRuntimePeptideExcludedAasChangeAction({
      peptideExcludedAAs: value,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideFixedModificationsChange = useCallback((value: { position: number; ccd: string }[]) => {
    handleRuntimePeptideFixedModificationsChangeAction({
      peptideFixedModifications: value,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideIterationsChange = useCallback((value: number) => {
    handleRuntimePeptideIterationsChangeAction({
      peptideIterations: value,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideProposerBackendChange = useCallback((value: 'esm3' | 'rfd4') => {
    handleRuntimePeptideProposerBackendChangeAction({
      peptideProposerBackend: value,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptidePopulationSizeChange = useCallback((value: number) => {
    handleRuntimePeptidePopulationSizeChangeAction({
      peptidePopulationSize: value,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideEliteSizeChange = useCallback((value: number) => {
    handleRuntimePeptideEliteSizeChangeAction({
      peptideEliteSize: value,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideResiduePoolChange = useCallback((value: PeptideResiduePoolSelection[]) => {
    handleRuntimePeptideResiduePoolChangeAction({
      peptideResiduePool: value,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideNonNaturalRangeChange = useCallback((min: number, max: number) => {
    handleRuntimePeptideNonNaturalRangeChangeAction({
      min,
      max,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideBicyclicLinkerCcdChange = useCallback((value: 'SEZ' | '29N' | 'BS3') => {
    handleRuntimePeptideBicyclicLinkerCcdChangeAction({
      peptideBicyclicLinkerCcd: value,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideBicyclicCysPositionModeChange = useCallback((value: 'auto' | 'manual') => {
    handleRuntimePeptideBicyclicCysPositionModeChangeAction({
      peptideBicyclicCysPositionMode: value,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideBicyclicFixTerminalCysChange = useCallback((value: boolean) => {
    handleRuntimePeptideBicyclicFixTerminalCysChangeAction({
      peptideBicyclicFixTerminalCys: value,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideBicyclicIncludeExtraCysChange = useCallback((value: boolean) => {
    handleRuntimePeptideBicyclicIncludeExtraCysChangeAction({
      peptideBicyclicIncludeExtraCys: value,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideBicyclicCys1PosChange = useCallback((value: number) => {
    handleRuntimePeptideBicyclicCys1PosChangeAction({
      peptideBicyclicCys1Pos: value,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideBicyclicCysLayoutChange = useCallback((value: 'auto' | 'ring' | 'ratio' | 'absolute') => {
    handleRuntimePeptideBicyclicCysLayoutChangeAction({
      peptideBicyclicCysLayout: value,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideBicyclicRingChange = useCallback((ring1: number, ring2: number) => {
    handleRuntimePeptideBicyclicRingChangeAction({
      peptideBicyclicRing1: ring1,
      peptideBicyclicRing2: ring2,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideBicyclicRatioChange = useCallback((pct1: number, pct2: number, pct3?: number) => {
    handleRuntimePeptideBicyclicRatioChangeAction({
      peptideBicyclicRatio1: pct1,
      peptideBicyclicRatio2: pct2,
      peptideBicyclicRatio3: pct3,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideBicyclicCys2PosChange = useCallback((value: number) => {
    handleRuntimePeptideBicyclicCys2PosChangeAction({
      peptideBicyclicCys2Pos: value,
      setDraft
    });
  }, [setDraft]);

  const handleRuntimePeptideBicyclicCys3PosChange = useCallback((value: number) => {
    handleRuntimePeptideBicyclicCys3PosChangeAction({
      peptideBicyclicCys3Pos: value,
      setDraft
    });
  }, [setDraft]);

  const handleTaskNameChange = useCallback((value: string) => {
    handleTaskNameChangeAction({
      value,
      setDraft
    });
  }, [setDraft]);

  const handleTaskSummaryChange = useCallback((value: string) => {
    handleTaskSummaryChangeAction({
      value,
      setDraft
    });
  }, [setDraft]);

  return {
    handlePredictionComponentsChange,
    handlePredictionProteinTemplateChange,
    handlePredictionTemplateResiduePick,
    handleRuntimeBackendChange,
    handleRuntimeSeedChange,
    handleRuntimeRfd4SpecChange,
    handleRuntimeLowVramChange,
    handleRuntimePeptideDesignModeChange,
    handleRuntimePeptideChiralityChange,
    handleRuntimePeptideStructureModeChange,
    handleRuntimePeptideStructureUploadChange,
    handleRuntimePeptideBinderLengthChange,
    handleRuntimePeptideLengthRange,
    handleRuntimePeptideUseInitialSequenceChange,
    handleRuntimePeptideInitialSequenceChange,
    handleRuntimePeptideSequenceMaskChange,
    handleRuntimePeptideExcludedAasChange,
    handleRuntimePeptideFixedModificationsChange,
    handleRuntimePeptideIterationsChange,
    handleRuntimePeptideProposerBackendChange,
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
