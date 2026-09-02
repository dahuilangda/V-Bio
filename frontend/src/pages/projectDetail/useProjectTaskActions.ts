import type { Dispatch, FormEvent, MutableRefObject, SetStateAction } from 'react';
import { useCallback, useRef } from 'react';
import type { AffinityPersistedUploads } from '../../hooks/useAffinityWorkflow';
import type { DownloadResultMode } from '../../api/backendTaskApi';
import type {
  CustomCcdMoleculeInput,
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
  customResidueLibrary: CustomCcdMoleculeInput[];
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
  createAffinityUploadsFingerprint: (uploads: AffinityPersistedUploads) => string;
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
  setSavedAffinityUploadsFingerprint: (value: string) => void;
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
  resolveEditableDraftTaskRowId: () => string | null;
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
    options?: { taskRowId?: string; persistProject?: boolean; resultMode?: DownloadResultMode; preferredStructureName?: string }
  ) => Promise<void>;
  refreshStatus: (options?: { silent?: boolean; taskId?: string }) => Promise<void>;
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
    customResidueLibrary,
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
    createAffinityUploadsFingerprint,
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
    setSavedAffinityUploadsFingerprint,
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
        sortProjectTasks,
        currentTask: projectTasks.find((row) => String(row.id || '').trim() === taskRowId) || null
      }),
    [projectTasks, updateProjectTask, setProjectTasks, sortProjectTasks]
  );

  const resolveEditableDraftTaskRowId = useCallback(
    (): string | null =>
      resolveEditableDraftTaskRowIdFromContext({
        requestNewTask,
        locationSearch,
        project,
        projectTasks,
        isDraftTaskSnapshot
      }),
    [requestNewTask, locationSearch, project, projectTasks, isDraftTaskSnapshot]
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

  const saveInFlightRef = useRef(false);

const saveDraft = useCallback(
    async (event?: FormEvent) => {
      event?.preventDefault();
      if (!project || !draft) return;
      // Save-in-flight guard: the header button disables on `saving`, but the copilot
      // auto-save paths call saveDraft directly — two overlapping saves both resolved
      // 'no reusable row' and INSERTED duplicate draft rows.
      if (saveInFlightRef.current) return;
      saveInFlightRef.current = true;

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
          customResidueLibrary,
          normalizeConfigForBackend,
          nonEmptyComponents,
          computeUseMsaFlag,
          createDraftFingerprint,
          createComputationFingerprint,
          createProteinTemplatesFingerprint,
          createAffinityUploadsFingerprint,
          buildAffinityUploadSnapshotComponents,
          addTemplatesToTaskSnapshotComponents,
          persistDraftTaskSnapshot,
          resolveEditableDraftTaskRowId,
            patch,
          patchTask,
          rememberTemplatesForTaskRow,
          rememberAffinityUploadsForTaskRow,
          setDraft,
          setSavedDraftFingerprint,
          setSavedComputationFingerprint,
          setSavedTemplateFingerprint,
          setSavedAffinityUploadsFingerprint,
          setRunMenuOpen,
          navigate
        });
      } catch (err) {
        setError(err instanceof Error ? `Failed to save draft: ${err.message}` : 'Failed to save draft.');
      } finally {
        saveInFlightRef.current = false;
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
      normalizeConfigForBackend,
      nonEmptyComponents,
      computeUseMsaFlag,
      createDraftFingerprint,
      createComputationFingerprint,
      createProteinTemplatesFingerprint,
      createAffinityUploadsFingerprint,
      buildAffinityUploadSnapshotComponents,
      addTemplatesToTaskSnapshotComponents,
      persistDraftTaskSnapshot,
      resolveEditableDraftTaskRowId,
        patch,
      patchTask,
      rememberTemplatesForTaskRow,
      rememberAffinityUploadsForTaskRow,
      setDraft,
      setSavedDraftFingerprint,
      setSavedComputationFingerprint,
      setSavedTemplateFingerprint,
      setSavedAffinityUploadsFingerprint,
      setRunMenuOpen,
      navigate,
      setSaving,
      setError
    ]
  );

  const pullResultForViewer = useCallback(
    async (taskId: string, options?: { taskRowId?: string; persistProject?: boolean; resultMode?: DownloadResultMode; preferredStructureName?: string }) => {
      const normalizedTaskRowId = String(options?.taskRowId || '').trim();
      const normalizedTaskId = String(taskId || '').trim();
      const taskRow =
        (normalizedTaskRowId
          ? projectTasks.find((row) => String(row.id || '').trim() === normalizedTaskRowId)
          : null) ||
        projectTasks.find((row) => String(row.task_id || '').trim() === normalizedTaskId) ||
        null;
      const scopedProjectTaskId = String(project?.task_id || '').trim();
      const runtimeState = String(
        taskRow?.task_state || (scopedProjectTaskId === normalizedTaskId ? project?.task_state : '') || ''
      )
        .trim()
        .toUpperCase();
      if (runtimeState && runtimeState !== 'SUCCESS') {
        setResultError(null);
        return;
      }
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
    async (options?: { silent?: boolean; taskId?: string }) =>
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
    persistDraftTaskSnapshot,
    saveDraft,
    pullResultForViewer,
    refreshStatus
  };
}
