import { useCallback, useEffect } from 'react';
import type { Dispatch, MutableRefObject, SetStateAction } from 'react';
import type {
  Project,
  ProjectTask,
  ProteinTemplateUpload,
} from '../../types/models';
import type { AffinityPersistedUploads } from '../../hooks/useAffinityWorkflow';
import type { ConstraintResiduePick } from '../../components/project/ConstraintEditor';
import { loadProjectIntoWorkspace } from './projectLoadController';
import type { ProjectWorkspaceDraft, WorkspaceTab } from './workspaceTypes';

interface UseProjectWorkspaceLoaderOptions<TDraft extends ProjectWorkspaceDraft> {
  entryRoutingResolved: boolean;
  projectId: string;
  locationSearch: string;
  requestNewTask: boolean;
  sessionUserId?: string;
  setLoading: Dispatch<SetStateAction<boolean>>;
  setError: Dispatch<SetStateAction<string | null>>;
  setProjectTasks: Dispatch<SetStateAction<ProjectTask[]>>;
  setWorkspaceTab: Dispatch<SetStateAction<WorkspaceTab>>;
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
  setPickedResidue: Dispatch<SetStateAction<ConstraintResiduePick | null>>;
  setProject: Dispatch<SetStateAction<Project | null>>;
}

export function useProjectWorkspaceLoader<TDraft extends ProjectWorkspaceDraft>({
  entryRoutingResolved,
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
}: UseProjectWorkspaceLoaderOptions<TDraft>): () => Promise<void> {
  const loadProject = useCallback(async () => {
    await loadProjectIntoWorkspace({
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
      setProject
    });
  }, [
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
  ]);

  useEffect(() => {
    if (!entryRoutingResolved) return;
    void loadProject();
  }, [entryRoutingResolved, loadProject]);

  return loadProject;
}
