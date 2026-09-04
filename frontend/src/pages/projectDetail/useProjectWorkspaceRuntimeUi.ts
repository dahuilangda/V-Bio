import { useEffect, useRef } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import type { CustomCcdMoleculeInput, Project, ProteinTemplateUpload, TaskState } from '../../types/models';
import type { AffinityPersistedUploads } from '../../hooks/useAffinityWorkflow';
import { saveProjectUiState } from '../../utils/projectInputs';
import { getWorkflowDefinition, isPredictionLikeWorkflowKey } from '../../utils/workflows';
import { allowedConstraintTypesForBackend } from './projectDraftUtils';
import type { WorkspaceTab } from './workspaceTypes';

interface UseProjectWorkspaceRuntimeUiOptions {
  project: Project | null;
  backend: string;
  workspaceTab: WorkspaceTab;
  setWorkspaceTab: Dispatch<SetStateAction<WorkspaceTab>>;
  proteinTemplates: Record<string, ProteinTemplateUpload>;
  customResidueLibrary: CustomCcdMoleculeInput[];
  taskProteinTemplates: Record<string, Record<string, ProteinTemplateUpload>>;
  taskAffinityUploads: Record<string, AffinityPersistedUploads>;
  activeConstraintId: string | null;
  selectedConstraintTemplateComponentId: string | null;
}

export function useProjectWorkspaceRuntimeUi({
  project,
  backend,
  workspaceTab,
  setWorkspaceTab,
  proteinTemplates,
  customResidueLibrary,
  taskProteinTemplates,
  taskAffinityUploads,
  activeConstraintId,
  selectedConstraintTemplateComponentId,
}: UseProjectWorkspaceRuntimeUiOptions): void {
  const prevTaskStateRef = useRef<TaskState | null>(null);

  useEffect(() => {
    if (!project) return;
    const workflowDef = getWorkflowDefinition(project.task_type);
    const isPredictionLikeWorkflow = isPredictionLikeWorkflowKey(workflowDef.key);
    const allowsComponentsTab =
      isPredictionLikeWorkflow || workflowDef.key === 'affinity' || workflowDef.key === 'lead_optimization';
    const allowsConstraintsTab =
      (isPredictionLikeWorkflow && allowedConstraintTypesForBackend(backend).length > 0) ||
      workflowDef.key === 'lead_optimization';
    if (
      (!allowsComponentsTab && workspaceTab === 'components') ||
      (!allowsConstraintsTab && workspaceTab === 'constraints')
    ) {
      setWorkspaceTab('basics');
    }
  }, [project, backend, workspaceTab, setWorkspaceTab]);

  useEffect(() => {
    if (!project) return;
    const prev = prevTaskStateRef.current;
    const next = project.task_state;
    if (prev && prev !== next && next === 'SUCCESS') {
      setWorkspaceTab('results');
    }
    prevTaskStateRef.current = next;
  }, [project, setWorkspaceTab]);

  // NOTE: a former 1s `setNowTs` interval lived here, ticking a top-level state that
  // re-rendered the entire 2700-line workspace tree every second while a task ran —
  // its only consumer was the header's elapsed-seconds chip, which now owns its own
  // tick inside ProjectHeaderMeta's <ElapsedSeconds>.

  useEffect(() => {
    if (!project) return;
    saveProjectUiState(project.id, {
      proteinTemplates,
      customResidueLibrary,
      taskProteinTemplates,
      taskAffinityUploads,
      activeConstraintId,
      selectedConstraintTemplateComponentId
    });
    // Keyed on project.id, not the project object: saveProjectUiState reads the project id
    // only, and the runtime overlays refresh the project object on every poll — object-keyed
    // deps re-serialized the whole (MB-scale) template/upload blob to localStorage per tick.
  }, [
    project?.id,
    proteinTemplates,
    customResidueLibrary,
    taskProteinTemplates,
    taskAffinityUploads,
    activeConstraintId,
    selectedConstraintTemplateComponentId,
  ]);
}
