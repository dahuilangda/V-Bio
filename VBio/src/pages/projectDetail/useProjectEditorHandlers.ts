import type { Dispatch, SetStateAction } from 'react';
import type {
  InputComponent,
  PredictionConstraint,
  ProteinTemplateUpload
} from '../../types/models';
import type { ConstraintResiduePick } from '../../components/project/ConstraintEditor';
import type { MolstarResiduePick } from '../../components/project/MolstarViewer';
import {
  handlePredictionComponentsChangeAction,
  handlePredictionProteinTemplateChangeAction,
  handlePredictionTemplateResiduePickAction,
  handleRuntimeBackendChangeAction,
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
    handleTaskNameChange,
    handleTaskSummaryChange
  };
}
