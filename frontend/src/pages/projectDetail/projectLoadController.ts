import type { Dispatch, MutableRefObject, SetStateAction } from 'react';
import type { ProteinTemplateUpload, Project, ProjectTask } from '../../types/models';
import type { AffinityPersistedUploads } from '../../hooks/useAffinityWorkflow';
import { loadProjectFlow } from './projectLoadFlow';

interface DraftLike {
  taskName: string;
  taskSummary: string;
  backend: string;
  use_msa: boolean;
  color_mode: string;
  inputConfig: any;
}

export async function loadProjectIntoWorkspace<TDraft extends DraftLike>(params: {
  projectId: string;
  locationSearch: string;
  requestNewTask: boolean;
  sessionUserId?: string;
  setLoading: Dispatch<SetStateAction<boolean>>;
  setError: Dispatch<SetStateAction<string | null>>;
  setProjectTasks: Dispatch<SetStateAction<ProjectTask[]>>;
  setWorkspaceTab: Dispatch<SetStateAction<any>>;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
  setSavedDraftFingerprint: Dispatch<SetStateAction<string>>;
  setSavedComputationFingerprint: Dispatch<SetStateAction<string>>;
  setSavedTemplateFingerprint: Dispatch<SetStateAction<string>>;
  setRunMenuOpen: Dispatch<SetStateAction<boolean>>;
  setProteinTemplates: Dispatch<SetStateAction<Record<string, ProteinTemplateUpload>>>;
  setTaskProteinTemplates: Dispatch<SetStateAction<Record<string, Record<string, ProteinTemplateUpload>>>>;
  setTaskAffinityUploads: Dispatch<SetStateAction<Record<string, AffinityPersistedUploads>>>;
  setActiveConstraintId: Dispatch<SetStateAction<string | null>>;
  setSelectedContactConstraintIds: Dispatch<SetStateAction<string[]>>;
  constraintSelectionAnchorRef: MutableRefObject<string | null>;
  setSelectedConstraintTemplateComponentId: Dispatch<SetStateAction<string | null>>;
  setPickedResidue: Dispatch<SetStateAction<any>>;
  setProject: Dispatch<SetStateAction<Project | null>>;
}): Promise<void> {
  const {
    projectId,
    locationSearch,
    requestNewTask,
    sessionUserId,
    setLoading,
    setError,
    setProjectTasks,
    setWorkspaceTab,
    setDraft,
    setSavedDraftFingerprint,
    setSavedComputationFingerprint,
    setSavedTemplateFingerprint,
    setRunMenuOpen,
    setProteinTemplates,
    setTaskProteinTemplates,
    setTaskAffinityUploads,
    setActiveConstraintId,
    setSelectedContactConstraintIds,
    constraintSelectionAnchorRef,
    setSelectedConstraintTemplateComponentId,
    setPickedResidue,
    setProject,
  } = params;

  setLoading(true);
  setError(null);
  setProjectTasks([]);

  try {
    const loaded = await loadProjectFlow({
      projectId,
      locationSearch,
      requestNewTask,
      sessionUserId,
    });

    if (loaded.suggestedWorkspaceTab) {
      setWorkspaceTab(loaded.suggestedWorkspaceTab);
    }

    setDraft(loaded.draft as TDraft);
    setSavedDraftFingerprint(loaded.savedDraftFingerprint);
    setSavedComputationFingerprint(loaded.savedComputationFingerprint);
    setSavedTemplateFingerprint(loaded.savedTemplateFingerprint);
    setRunMenuOpen(false);
    setProteinTemplates(loaded.proteinTemplates);
    setTaskProteinTemplates(loaded.taskProteinTemplates);
    setTaskAffinityUploads(loaded.taskAffinityUploads);
    setActiveConstraintId(loaded.activeConstraintId);
    setSelectedContactConstraintIds([]);
    constraintSelectionAnchorRef.current = null;
    setSelectedConstraintTemplateComponentId(loaded.selectedConstraintTemplateComponentId);
    setPickedResidue(null);
    setProject(loaded.project);
    setProjectTasks(loaded.projectTasks);
  } catch (err) {
    setError(err instanceof Error ? err.message : 'Failed to load project.');
  } finally {
    setLoading(false);
  }
}
