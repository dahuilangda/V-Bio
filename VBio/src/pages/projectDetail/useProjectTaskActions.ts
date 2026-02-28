import type { Dispatch, FormEvent, MutableRefObject, SetStateAction } from 'react';
import { useCallback } from 'react';
import type { AffinityPersistedUploads } from '../../hooks/useAffinityWorkflow';
import type { DownloadResultMode } from '../../api/backendTaskApi';
import type {
  InputComponent,
  Project,
  ProjectInputConfig,
  ProjectTask,
  ProteinTemplateUpload
} from '../../types/models';
import {
  patchProjectRecord,
  patchTaskRecord,
  persistDraftTaskSnapshotRecord,
  resolveEditableDraftTaskRowIdFromContext,
  resolveRuntimeTaskRowIdFromContext
} from './projectDraftPersistence';
import { saveProjectDraftFromWorkspace, type SaveDraftFields } from './projectDraftSave';
import { pullResultForViewerTask, refreshTaskStatus } from './projectTaskRuntime';

interface UseProjectTaskActionsInput {
  project: Project | null;
  projectTasks: ProjectTask[];
  draft: SaveDraftFields | null;
  requestNewTask: boolean;
  locationSearch: string;
  workspaceTab: 'results' | 'basics' | 'components' | 'constraints';
  metadataOnlyDraftDirty: boolean;
  affinityLigandSmiles: string;
  affinityPreviewLigandSmiles: string;
  affinityTargetFile: File | null;
  affinityLigandFile: File | null;
  affinityCurrentUploads: AffinityPersistedUploads;
  proteinTemplates: Record<string, ProteinTemplateUpload>;
  requestedStatusTaskRowId: string | null;
  activeStatusTaskRowId: string | null;
  statusRefreshInFlightRef: MutableRefObject<Set<string>>;
  insertProjectTask: (input: Partial<ProjectTask>) => Promise<ProjectTask>;
  updateProject: (projectId: string, patch: Partial<Project>) => Promise<Project>;
  updateProjectTask: (
    taskRowId: string,
    patch: Partial<ProjectTask>,
    options?: { minimalReturn?: boolean; select?: string }
  ) => Promise<ProjectTask>;
  sortProjectTasks: (rows: ProjectTask[]) => ProjectTask[];
  isDraftTaskSnapshot: (task: ProjectTask | null) => boolean;
  normalizeConfigForBackend: (inputConfig: ProjectInputConfig, backend: string) => ProjectInputConfig;
  nonEmptyComponents: (components: InputComponent[]) => InputComponent[];
  computeUseMsaFlag: (components: InputComponent[], fallback?: boolean) => boolean;
  createDraftFingerprint: (draft: SaveDraftFields) => string;
  createComputationFingerprint: (draft: SaveDraftFields) => string;
  createProteinTemplatesFingerprint: (templates: Record<string, ProteinTemplateUpload>) => string;
  buildAffinityUploadSnapshotComponents: (
    baseComponents: InputComponent[],
    targetFile: File | null,
    ligandFile: File | null,
    ligandSmiles?: string
  ) => Promise<InputComponent[]>;
  addTemplatesToTaskSnapshotComponents: (
    components: InputComponent[],
    templates: Record<string, ProteinTemplateUpload>
  ) => InputComponent[];
  rememberTemplatesForTaskRow: (taskRowId: string | null, templates: Record<string, ProteinTemplateUpload>) => void;
  rememberAffinityUploadsForTaskRow: (taskRowId: string | null, uploads: AffinityPersistedUploads) => void;
  setProject: Dispatch<SetStateAction<Project | null>>;
  setProjectTasks: Dispatch<SetStateAction<ProjectTask[]>>;
  setDraft: (value: SaveDraftFields) => void;
  setSaving: (value: boolean) => void;
  setError: (value: string | null) => void;
  setSavedDraftFingerprint: (value: string) => void;
  setSavedComputationFingerprint: (value: string) => void;
  setSavedTemplateFingerprint: (value: string) => void;
  setRunMenuOpen: (value: boolean) => void;
  navigate: (path: string, options?: { replace?: boolean }) => void;
  setStructureText: (value: string) => void;
  setStructureFormat: (value: 'cif' | 'pdb') => void;
  setStructureTaskId: (value: string | null) => void;
  setResultError: (value: string | null) => void;
  setStatusInfo: Dispatch<SetStateAction<Record<string, unknown> | null>>;
}

interface UseProjectTaskActionsOutput {
  patch: (payload: Partial<Project>) => Promise<Project | null>;
  patchTask: (taskRowId: string, payload: Partial<ProjectTask>) => Promise<ProjectTask | null>;
  resolveEditableDraftTaskRowId: (options?: { allowLatestDraftFallback?: boolean }) => string | null;
  resolveRuntimeTaskRowId: () => string | null;
  persistDraftTaskSnapshot: (
    normalizedConfig: ProjectInputConfig,
    options?: {
      statusText?: string;
      reuseTaskRowId?: string | null;
      snapshotComponents?: InputComponent[];
      proteinSequenceOverride?: string;
      ligandSmilesOverride?: string;
    }
  ) => Promise<ProjectTask>;
  saveDraft: (event?: FormEvent) => Promise<void>;
  pullResultForViewer: (
    taskId: string,
    options?: { taskRowId?: string; persistProject?: boolean; resultMode?: DownloadResultMode }
  ) => Promise<void>;
  refreshStatus: (options?: { silent?: boolean }) => Promise<void>;
}

export function useProjectTaskActions(input: UseProjectTaskActionsInput): UseProjectTaskActionsOutput {
  const {
    project,
    projectTasks,
    draft,
    requestNewTask,
    locationSearch,
    workspaceTab,
    metadataOnlyDraftDirty,
    affinityLigandSmiles,
    affinityPreviewLigandSmiles,
    affinityTargetFile,
    affinityLigandFile,
    affinityCurrentUploads,
    proteinTemplates,
    requestedStatusTaskRowId,
    activeStatusTaskRowId,
    statusRefreshInFlightRef,
    insertProjectTask,
    updateProject,
    updateProjectTask,
    sortProjectTasks,
    isDraftTaskSnapshot,
    normalizeConfigForBackend,
    nonEmptyComponents,
    computeUseMsaFlag,
    createDraftFingerprint,
    createComputationFingerprint,
    createProteinTemplatesFingerprint,
    buildAffinityUploadSnapshotComponents,
    addTemplatesToTaskSnapshotComponents,
    rememberTemplatesForTaskRow,
    rememberAffinityUploadsForTaskRow,
    setProject,
    setProjectTasks,
    setDraft,
    setSaving,
    setError,
    setSavedDraftFingerprint,
    setSavedComputationFingerprint,
    setSavedTemplateFingerprint,
    setRunMenuOpen,
    navigate,
    setStructureText,
    setStructureFormat,
    setStructureTaskId,
    setResultError,
    setStatusInfo
  } = input;

  const patch = useCallback(
    async (payload: Partial<Project>) =>
      patchProjectRecord({
        project,
        payload,
        updateProject,
        setProject
      }),
    [project, updateProject, setProject]
  );

  const patchTask = useCallback(
    async (taskRowId: string, payload: Partial<ProjectTask>) =>
      patchTaskRecord({
        taskRowId,
        payload,
        updateProjectTask,
        setProjectTasks,
        sortProjectTasks
      }),
    [updateProjectTask, setProjectTasks, sortProjectTasks]
  );

  const resolveEditableDraftTaskRowId = useCallback(
    (options?: { allowLatestDraftFallback?: boolean }): string | null =>
      resolveEditableDraftTaskRowIdFromContext({
        requestNewTask,
        locationSearch,
        project,
        projectTasks,
        isDraftTaskSnapshot,
        allowLatestDraftFallback: options?.allowLatestDraftFallback
      }),
    [requestNewTask, locationSearch, project, projectTasks, isDraftTaskSnapshot]
  );

  const resolveRuntimeTaskRowId = useCallback(
    (): string | null =>
      resolveRuntimeTaskRowIdFromContext({
        project,
        projectTasks
      }),
    [project, projectTasks]
  );

  const persistDraftTaskSnapshot = useCallback(
    async (
      normalizedConfig: ProjectInputConfig,
      options?: {
        statusText?: string;
        reuseTaskRowId?: string | null;
        snapshotComponents?: InputComponent[];
        proteinSequenceOverride?: string;
        ligandSmilesOverride?: string;
      }
    ): Promise<ProjectTask> =>
      persistDraftTaskSnapshotRecord({
        project,
        draft,
        normalizedConfig,
        options,
        insertProjectTask,
        updateProjectTask,
        setProjectTasks,
        sortProjectTasks
      }),
    [project, draft, insertProjectTask, updateProjectTask, setProjectTasks, sortProjectTasks]
  );

  const saveDraft = useCallback(
    async (event?: FormEvent) => {
      event?.preventDefault();
      if (!project || !draft) return;

      setSaving(true);
      setError(null);
      try {
        await saveProjectDraftFromWorkspace({
          project,
          draft,
          workspaceTab,
          metadataOnlyDraftDirty,
          affinityLigandSmiles,
          affinityPreviewLigandSmiles,
          affinityTargetFile,
          affinityLigandFile,
          affinityCurrentUploads,
          proteinTemplates,
          requestedStatusTaskRowId,
          activeStatusTaskRowId,
          normalizeConfigForBackend,
          nonEmptyComponents,
          computeUseMsaFlag,
          createDraftFingerprint,
          createComputationFingerprint,
          createProteinTemplatesFingerprint,
          buildAffinityUploadSnapshotComponents,
          addTemplatesToTaskSnapshotComponents,
          persistDraftTaskSnapshot,
          resolveEditableDraftTaskRowId,
          resolveRuntimeTaskRowId,
          patch,
          patchTask,
          rememberTemplatesForTaskRow,
          rememberAffinityUploadsForTaskRow,
          setDraft,
          setSavedDraftFingerprint,
          setSavedComputationFingerprint,
          setSavedTemplateFingerprint,
          setRunMenuOpen,
          navigate
        });
      } catch (err) {
        setError(err instanceof Error ? `Failed to save draft: ${err.message}` : 'Failed to save draft.');
      } finally {
        setSaving(false);
      }
    },
    [
      project,
      draft,
      workspaceTab,
      metadataOnlyDraftDirty,
      affinityLigandSmiles,
      affinityPreviewLigandSmiles,
      affinityTargetFile,
      affinityLigandFile,
      affinityCurrentUploads,
      proteinTemplates,
      requestedStatusTaskRowId,
      activeStatusTaskRowId,
      normalizeConfigForBackend,
      nonEmptyComponents,
      computeUseMsaFlag,
      createDraftFingerprint,
      createComputationFingerprint,
      createProteinTemplatesFingerprint,
      buildAffinityUploadSnapshotComponents,
      addTemplatesToTaskSnapshotComponents,
      persistDraftTaskSnapshot,
      resolveEditableDraftTaskRowId,
      resolveRuntimeTaskRowId,
      patch,
      patchTask,
      rememberTemplatesForTaskRow,
      rememberAffinityUploadsForTaskRow,
      setDraft,
      setSavedDraftFingerprint,
      setSavedComputationFingerprint,
      setSavedTemplateFingerprint,
      setRunMenuOpen,
      navigate,
      setSaving,
      setError
    ]
  );

  const pullResultForViewer = useCallback(
    async (taskId: string, options?: { taskRowId?: string; persistProject?: boolean; resultMode?: DownloadResultMode }) => {
      const normalizedTaskRowId = String(options?.taskRowId || '').trim();
      const normalizedTaskId = String(taskId || '').trim();
      const taskRow =
        (normalizedTaskRowId
          ? projectTasks.find((row) => String(row.id || '').trim() === normalizedTaskRowId)
          : null) ||
        projectTasks.find((row) => String(row.task_id || '').trim() === normalizedTaskId) ||
        null;
      const baseTaskConfidence =
        taskRow?.confidence && typeof taskRow.confidence === 'object' ? (taskRow.confidence as Record<string, unknown>) : null;
      const baseTaskProperties =
        taskRow?.properties && typeof taskRow.properties === 'object' ? (taskRow.properties as unknown as Record<string, unknown>) : null;
      const baseProjectConfidence =
        project?.confidence && typeof project.confidence === 'object' ? (project.confidence as Record<string, unknown>) : null;
      return pullResultForViewerTask({
        taskId,
        options,
        baseProjectConfidence,
        baseTaskConfidence,
        baseTaskProperties,
        patch,
        patchTask,
        setStatusInfo,
        setStructureText,
        setStructureFormat,
        setStructureTaskId,
        setResultError
      });
    },
    [project, projectTasks, patch, patchTask, setStatusInfo, setStructureText, setStructureFormat, setStructureTaskId, setResultError]
  );

  const refreshStatus = useCallback(
    async (options?: { silent?: boolean }) =>
      refreshTaskStatus({
        project,
        projectTasks,
        statusRefreshInFlightRef,
        setError,
        setStatusInfo,
        patch,
        patchTask,
        pullResultForViewer,
        options
      }),
    [project, projectTasks, statusRefreshInFlightRef, setError, setStatusInfo, patch, patchTask, pullResultForViewer]
  );

  return {
    patch,
    patchTask,
    resolveEditableDraftTaskRowId,
    resolveRuntimeTaskRowId,
    persistDraftTaskSnapshot,
    saveDraft,
    pullResultForViewer,
    refreshStatus
  };
}
