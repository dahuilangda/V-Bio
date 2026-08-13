import { useCallback, useEffect, useMemo, useRef } from 'react';
import type { Dispatch, MutableRefObject, SetStateAction } from 'react';
import type {
  CustomCcdMoleculeInput,
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
  setSaving: Dispatch<SetStateAction<boolean>>;
  setSubmitting: Dispatch<SetStateAction<boolean>>;
  setError: Dispatch<SetStateAction<string | null>>;
  setProjectTasks: Dispatch<SetStateAction<ProjectTask[]>>;
  setWorkspaceTab: Dispatch<SetStateAction<WorkspaceTab>>;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
  setSavedDraftFingerprint: Dispatch<SetStateAction<string>>;
  setSavedComputationFingerprint: Dispatch<SetStateAction<string>>;
  setSavedTemplateFingerprint: Dispatch<SetStateAction<string>>;
  setSavedAffinityUploadsFingerprint: Dispatch<SetStateAction<string>>;
  setRunMenuOpen: Dispatch<SetStateAction<boolean>>;
  setProteinTemplates: Dispatch<SetStateAction<Record<string, ProteinTemplateUpload>>>;
  setCustomResidueLibrary: Dispatch<SetStateAction<CustomCcdMoleculeInput[]>>;
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
  setSaving,
  setSubmitting,
  setError,
  setProjectTasks,
  setWorkspaceTab,
  setDraft,
  setSavedDraftFingerprint,
  setSavedComputationFingerprint,
  setSavedTemplateFingerprint,
  setSavedAffinityUploadsFingerprint,
  setRunMenuOpen,
  setProteinTemplates,
  setCustomResidueLibrary,
  setTaskProteinTemplates,
  setTaskAffinityUploads,
  setActiveConstraintId,
  setSelectedContactConstraintIds,
  constraintSelectionAnchorRef,
  setSelectedConstraintTemplateComponentId,
  setPickedResidue,
  setProject,
}: UseProjectWorkspaceLoaderOptions<TDraft>): () => Promise<void> {
  const latestLocationSearchRef = useRef(locationSearch);
  useEffect(() => {
    latestLocationSearchRef.current = locationSearch;
  }, [locationSearch]);

  const loadContextSearchKey = useMemo(() => {
    // Only server-routing intent should trigger a full workspace refetch. Client-side view-state
    // params (task_row_id, tab, task_list_page, copilot_* prefill) are applied in place by their
    // own handlers — patching them in here would recreate `loadProject`, re-run the mount effect,
    // and cause the whole task list to be cleared and refetched on every submit/view switch.
    // The params that remain (new_task, source_task_row_id) are the ones that genuinely change
    // what the server must return (a fresh draft vs. an existing task's draft).
    const query = new URLSearchParams(locationSearch);
    query.delete('tab');
    query.delete('task_row_id');
    query.delete('task_list_page');
    query.delete('copilot_parameter_patch');
    const next = query.toString();
    return next ? `?${next}` : '';
  }, [locationSearch]);

  const loadProject = useCallback(async () => {
    await loadProjectIntoWorkspace({
      projectId,
      locationSearch: latestLocationSearchRef.current,
      requestNewTask,
      sessionUserId,
      setLoading,
      setSaving,
      setSubmitting,
      setError,
      setProjectTasks,
      setWorkspaceTab,
      setDraft,
      setSavedDraftFingerprint,
      setSavedComputationFingerprint,
      setSavedTemplateFingerprint,
      setSavedAffinityUploadsFingerprint,
      setRunMenuOpen,
      setProteinTemplates,
      setCustomResidueLibrary,
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
    loadContextSearchKey,
    requestNewTask,
    sessionUserId,
    setLoading,
    setSaving,
    setSubmitting,
    setError,
    setProjectTasks,
    setWorkspaceTab,
    setDraft,
    setSavedDraftFingerprint,
    setSavedComputationFingerprint,
    setSavedTemplateFingerprint,
    setSavedAffinityUploadsFingerprint,
    setRunMenuOpen,
    setProteinTemplates,
    setCustomResidueLibrary,
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
