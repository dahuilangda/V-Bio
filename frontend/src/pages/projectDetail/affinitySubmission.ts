import type { MutableRefObject } from 'react';
import { submitAffinityScoring, terminateTask } from '../../api/backendApi';
import type { AffinityPersistedUploads } from '../../hooks/useAffinityWorkflow';
import type { AffinityPreviewPayload, InputComponent, Project, ProjectInputConfig, ProjectTask, ProteinTemplateUpload } from '../../types/models';
import { normalizeTaskSummary } from '../../utils/taskMetadata';
import { normalizeAffinityBackend } from '../apiAccessHelpers';
import { mergeTaskInputOptionsIntoProperties } from './projectTaskSnapshot';

export type AffinityWorkspaceTab = 'results' | 'basics' | 'components' | 'constraints';

export interface AffinityDraftFields {
  taskName: string;
  taskSummary: string;
  backend: string;
  use_msa: boolean;
  color_mode: string;
  inputConfig: ProjectInputConfig;
}

export interface AffinitySubmitDeps {
  project: Project;
  draft: AffinityDraftFields;
  affinityTargetFile: File | null;
  affinityLigandFile: File | null;
  affinityPreviewLoading: boolean;
  affinityPreviewCurrent: boolean;
  affinityPreview: AffinityPreviewPayload | null;
  affinityPreviewError: string | null;
  affinityTargetChainIds: string[];
  affinityLigandChainId: string;
  affinityLigandSmiles: string;
  affinityHasLigand: boolean;
  affinitySupportsActivity: boolean;
  affinityConfidenceOnly: boolean;
  affinityCurrentUploads: AffinityPersistedUploads;
  proteinTemplates: Record<string, ProteinTemplateUpload>;
  submitInFlightRef: MutableRefObject<boolean>;
  runRedirectTimerRef: MutableRefObject<number | null>;
  runSuccessNoticeTimerRef: MutableRefObject<number | null>;
  setSubmitting: (value: boolean) => void;
  setError: (value: string | null) => void;
  setRunRedirectTaskId: (value: string | null) => void;
  setRunSuccessNotice: (value: string | null) => void;
  setDraft: (value: AffinityDraftFields) => void;
  setSavedDraftFingerprint: (value: string) => void;
  setSavedComputationFingerprint: (value: string) => void;
  setSavedTemplateFingerprint: (value: string) => void;
  setSavedAffinityUploadsFingerprint: (value: string) => void;
  setRunMenuOpen: (value: boolean) => void;
  syncWorkspaceTaskRow: (taskRowId: string) => void;
  setProjectTasks: (updater: (prev: ProjectTask[]) => ProjectTask[]) => void;
  setProject: (updater: (prev: Project | null) => Project | null) => void;
  setStatusInfo: (value: Record<string, unknown> | null) => void;
  showRunQueuedNotice: (message: string) => void;
  normalizeConfigForBackend: (inputConfig: ProjectInputConfig, backend: string) => ProjectInputConfig;
  computeUseMsaFlag: (components: InputComponent[], fallback?: boolean) => boolean;
  createDraftFingerprint: (draft: AffinityDraftFields) => string;
  createComputationFingerprint: (draft: AffinityDraftFields) => string;
  createProteinTemplatesFingerprint: (templates: Record<string, ProteinTemplateUpload>) => string;
  createAffinityUploadsFingerprint: (uploads: AffinityPersistedUploads) => string;
  buildAffinityUploadSnapshotComponents: (
    baseComponents: InputComponent[],
    targetFile: File | null,
    ligandFile: File | null,
    ligandSmiles?: string
  ) => Promise<InputComponent[]>;
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
  resolveEditableDraftTaskRowId: () => string | null;
  rememberAffinityUploadsForTaskRow: (taskRowId: string | null, uploads: AffinityPersistedUploads) => void;
  patch: (payload: Partial<Project>) => Promise<Project | null>;
  patchTask: (taskRowId: string, payload: Partial<ProjectTask>) => Promise<ProjectTask | null>;
  updateProjectTask: (taskRowId: string, payload: Partial<ProjectTask>) => Promise<ProjectTask>;
  findProjectTaskByTaskId: (taskId: string, projectId?: string) => Promise<ProjectTask | null>;
  deleteProjectTask: (taskRowId: string) => Promise<void>;
  sortProjectTasks: (rows: ProjectTask[]) => ProjectTask[];
  saveProjectInputConfig: (projectId: string, config: ProjectInputConfig) => void;
}

export async function submitAffinityTaskFromDraft(deps: AffinitySubmitDeps): Promise<void> {
  const {
    project,
    draft,
    affinityTargetFile,
    affinityLigandFile,
    affinityPreviewLoading,
    affinityPreviewCurrent,
    affinityPreview,
    affinityPreviewError,
    affinityTargetChainIds,
    affinityLigandChainId,
    affinityLigandSmiles,
    affinityHasLigand,
    affinitySupportsActivity,
    affinityConfidenceOnly,
    affinityCurrentUploads,
    proteinTemplates,
    submitInFlightRef,
    runRedirectTimerRef,
    runSuccessNoticeTimerRef,
    setSubmitting,
    setError,
    setRunRedirectTaskId,
    setRunSuccessNotice,
    setDraft,
    setSavedDraftFingerprint,
    setSavedComputationFingerprint,
    setSavedTemplateFingerprint,
    setSavedAffinityUploadsFingerprint,
    setRunMenuOpen,
    syncWorkspaceTaskRow,
    setProjectTasks,
    setProject,
    setStatusInfo,
    showRunQueuedNotice,
    normalizeConfigForBackend,
    computeUseMsaFlag,
    createDraftFingerprint,
    createComputationFingerprint,
    createProteinTemplatesFingerprint,
    createAffinityUploadsFingerprint,
    buildAffinityUploadSnapshotComponents,
    persistDraftTaskSnapshot,
    resolveEditableDraftTaskRowId,
    rememberAffinityUploadsForTaskRow,
    patch,
    patchTask,
    updateProjectTask,
    findProjectTaskByTaskId,
    deleteProjectTask,
    sortProjectTasks,
    saveProjectInputConfig
  } = deps;

  if (submitInFlightRef.current) return;

  if (!affinityTargetFile) {
    setError('Please upload target structure first.');
    return;
  }
  if (affinityPreviewLoading) {
    setError('Preview is building. Please wait a moment.');
    return;
  }
  if (!affinityPreviewCurrent || !affinityPreview?.structureText.trim()) {
    setError(affinityPreviewError || 'Failed to prepare scoring input from uploaded files.');
    return;
  }
  const activeAffinityBackend = normalizeAffinityBackend(draft.backend);
  // protenix2dock ignores the boltz-only affinity head (enable_affinity), so
  // the activity toggle only applies to the boltz backend.
  const backendSupportsActivity = activeAffinityBackend === 'boltz';
  const effectiveConfidenceOnly = backendSupportsActivity ? affinityConfidenceOnly : true;
  const affinityMode = ['score', 'pose', 'refine', 'interface', 'dock'].includes(draft.inputConfig.options.affinityMode || '')
    ? draft.inputConfig.options.affinityMode
    : 'dock';
  const dockPocket = affinityMode === 'dock' ? (draft.inputConfig.options.affinityDockPocket || null) : null;
  const targetChains = affinityTargetChainIds.filter((item) => item.trim());
  const ligandChain = affinityLigandChainId.trim() || (affinityMode === 'dock' ? 'L' : '');
  const previewLigandSmiles = String(affinityPreview?.ligandSmiles || '').trim();
  const ligandSmilesInput = affinityLigandSmiles.trim();
  const ligandSmiles = ligandSmilesInput || previewLigandSmiles;
  const usingSeparateInputs = Boolean(affinityTargetFile && (affinityLigandFile || (affinityMode === 'dock' && ligandSmiles)));
  const runAffinityActivity =
    backendSupportsActivity &&
    (affinityMode === 'dock'
      ? Boolean(ligandSmiles.trim())
      : !effectiveConfidenceOnly && affinityHasLigand && (affinitySupportsActivity || Boolean(ligandSmiles.trim())));
  if (affinityMode === 'dock' && !ligandSmiles) {
    const msg = 'Dock mode requires a ligand SMILES (draw or paste it in the editor).';
    setError(msg);
    // Throw so programmatic callers (Copilot submit) record an honest failed receipt
    // instead of resolving as if a task had been queued.
    throw new Error(msg);
  }
  if (affinityMode === 'dock' && !dockPocket) {
    setError('Dock mode requires a pocket box: pick residues in the 3D view, set a center manually, or upload a reference ligand.');
    return;
  }
  if (affinityMode !== 'score' && affinityMode !== 'dock' && !usingSeparateInputs) {
    const msg = 'Pose/refine/interface modes require uploaded target and ligand files.';
    setError(msg);
    throw new Error(msg);
  }
  if (runAffinityActivity && !targetChains.length) {
    setError('No target chain could be inferred from uploaded target structure.');
    return;
  }
  if (runAffinityActivity && !ligandChain) {
    setError('No ligand chain was detected for affinity activity mode.');
    return;
  }
  if (runAffinityActivity && !ligandSmiles) {
    setError('Ligand SMILES is required for affinity activity mode.');
    return;
  }

  submitInFlightRef.current = true;
  setSubmitting(true);
  setError(null);
  if (runRedirectTimerRef.current !== null) {
    window.clearTimeout(runRedirectTimerRef.current);
    runRedirectTimerRef.current = null;
  }
  setRunRedirectTaskId(null);
  setRunSuccessNotice(null);
  if (runSuccessNoticeTimerRef.current !== null) {
    window.clearTimeout(runSuccessNoticeTimerRef.current);
    runSuccessNoticeTimerRef.current = null;
  }

  try {
    const normalizedConfig = normalizeConfigForBackend(draft.inputConfig, activeAffinityBackend);
    const hasMsa = computeUseMsaFlag(normalizedConfig.components, draft.use_msa);
    const configWithAffinity: ProjectInputConfig = {
      ...normalizedConfig,
      properties: {
        ...normalizedConfig.properties,
        affinity: runAffinityActivity,
        target: runAffinityActivity ? targetChains[0] : null,
        ligand: runAffinityActivity ? ligandChain : null,
        binder: runAffinityActivity ? ligandChain : null
      }
    };
    const configWithAffinityTaskOptions: ProjectInputConfig = {
      ...configWithAffinity,
      properties: mergeTaskInputOptionsIntoProperties(configWithAffinity.properties, configWithAffinity.options)
    };
    const persistenceWarnings: string[] = [];
    const snapshotComponents = await buildAffinityUploadSnapshotComponents(
      configWithAffinity.components,
      affinityTargetFile,
      affinityLigandFile,
      ligandSmiles
    );

    saveProjectInputConfig(project.id, configWithAffinity);
    const nextDraft: AffinityDraftFields = {
      taskName: draft.taskName.trim(),
      taskSummary: normalizeTaskSummary(draft.taskSummary),
      backend: activeAffinityBackend,
      use_msa: hasMsa,
      color_mode: draft.color_mode === 'alphafold' ? 'alphafold' : 'default',
      inputConfig: configWithAffinity
    };
    setDraft(nextDraft);
    setSavedDraftFingerprint(createDraftFingerprint(nextDraft));
    setSavedComputationFingerprint(createComputationFingerprint(nextDraft));
    setSavedTemplateFingerprint(createProteinTemplatesFingerprint(proteinTemplates));
    setSavedAffinityUploadsFingerprint(createAffinityUploadsFingerprint(affinityCurrentUploads));
    setRunMenuOpen(false);

    try {
      await patch({
        backend: nextDraft.backend,
        use_msa: nextDraft.use_msa,
        color_mode: nextDraft.color_mode,
        status_text: 'Draft saved',
        protein_sequence: '',
        ligand_smiles: ligandSmiles
      });
    } catch (draftPersistError) {
      persistenceWarnings.push(
        `saving draft failed: ${draftPersistError instanceof Error ? draftPersistError.message : 'unknown error'}`
      );
    }

    const draftTaskRow = await persistDraftTaskSnapshot(configWithAffinityTaskOptions, {
      statusText: 'Affinity draft snapshot prepared for run',
      reuseTaskRowId: resolveEditableDraftTaskRowId(),
      snapshotComponents,
      proteinSequenceOverride: '',
      ligandSmilesOverride: ligandSmiles
    });
    rememberAffinityUploadsForTaskRow(draftTaskRow.id, affinityCurrentUploads);

    const taskId = await submitAffinityScoring({
      projectId: project.id,
      inputStructureText: affinityPreview.structureText,
      inputStructureName: affinityPreview.structureName || 'affinity_input.cif',
      targetFile: affinityTargetFile,
      ligandFile: affinityLigandFile,
      backend: activeAffinityBackend,
      seed: configWithAffinity.options.seed ?? null,
      mode: affinityMode,
      computeIpsae: affinityHasLigand,
      enableAffinity: runAffinityActivity,
      ligandSmiles,
      targetChainIds: ligandChain ? targetChains : [],
      ligandChainId: ligandChain,
      useMsa: nextDraft.use_msa,
      dockPocket
    });

    const queuedAt = new Date().toISOString();
    const queuedTaskPatch: Partial<ProjectTask> = {
      name: nextDraft.taskName.trim(),
      summary: nextDraft.taskSummary.trim(),
      task_id: taskId,
      task_state: 'QUEUED',
      status_text: 'Task submitted and waiting in queue',
      error_text: '',
      backend: activeAffinityBackend,
      seed: configWithAffinity.options.seed ?? null,
      protein_sequence: '',
      ligand_smiles: ligandSmiles,
      components: snapshotComponents,
      constraints: configWithAffinity.constraints,
      properties: configWithAffinityTaskOptions.properties,
      confidence: {},
      affinity: {},
      structure_name: '',
      submitted_at: queuedAt,
      completed_at: null,
      duration_seconds: null
    };

    try {
      if (draftTaskRow.id.startsWith('local-')) {
        await patchTask(draftTaskRow.id, queuedTaskPatch);
      } else {
        const queuedTaskRow = await updateProjectTask(draftTaskRow.id, queuedTaskPatch);
        setProjectTasks((prev) => sortProjectTasks(prev.map((row) => (row.id === queuedTaskRow.id ? queuedTaskRow : row))));
      }
    } catch (taskPersistError) {
      // Unique task_id conflict: the gateway's submit snapshot already claimed this task_id.
      // One runtime task is exactly one row — adopt the existing row and drop the local draft
      // row instead of failing a submit the runtime already queued.
      const conflictMessage = taskPersistError instanceof Error ? taskPersistError.message : String(taskPersistError);
      const isUniqueConflict = /PostgREST 409|23505|duplicate key|unique_project_tasks_task_id/i.test(conflictMessage);
      if (isUniqueConflict) {
        const existingRow = await findProjectTaskByTaskId(taskId, project.id);
        if (existingRow && existingRow.id !== draftTaskRow.id) {
          const adoptedRow = await updateProjectTask(existingRow.id, queuedTaskPatch);
          await deleteProjectTask(draftTaskRow.id).catch(() => { /* the draft row is redundant; deletion is best-effort */ });
          setProjectTasks((prev) => sortProjectTasks([
            adoptedRow,
            ...prev.filter((row) => row.id !== adoptedRow.id && row.id !== draftTaskRow.id)
          ]));
        } else {
          throw taskPersistError;
        }
      } else {
        // The backend task was queued but the local DB row couldn't be persisted — terminate the
        // orphaned backend task so it doesn't waste GPU compute. Fire-and-forget: the primary error
        // is the persist failure, which the caller must handle; the termination is best-effort cleanup.
        terminateTask(taskId).catch(() => { /* ignore termination errors */ });
        throw new Error(
          `Task submitted (${taskId}) but failed to persist queued task row: ${
            taskPersistError instanceof Error ? taskPersistError.message : 'unknown error'
          }`
        );
      }
    }

    const dbPayload: Partial<Project> = {
      task_id: taskId,
      task_state: 'QUEUED',
      status_text: 'Task submitted and waiting in queue',
      error_text: '',
      backend: activeAffinityBackend,
      protein_sequence: '',
      ligand_smiles: ligandSmiles,
      submitted_at: queuedAt,
      completed_at: null,
      duration_seconds: null
    };

    try {
      await patch(dbPayload);
    } catch (dbError) {
      setProject((prev) =>
        prev
          ? {
              ...prev,
              ...dbPayload
            }
          : prev
      );
      persistenceWarnings.push(`saving project state failed: ${dbError instanceof Error ? dbError.message : 'unknown error'}`);
    }

    setStatusInfo(null);
    setRunRedirectTaskId(null);
    syncWorkspaceTaskRow(draftTaskRow.id);
    if (persistenceWarnings.length > 0) {
      showRunQueuedNotice(`Task ${taskId.slice(0, 8)} queued with sync warning.`);
    } else {
      showRunQueuedNotice(`Task ${taskId.slice(0, 8)} queued.`);
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : 'Failed to submit the docking run.';
    if (runRedirectTimerRef.current !== null) {
      window.clearTimeout(runRedirectTimerRef.current);
      runRedirectTimerRef.current = null;
    }
    setRunRedirectTaskId(null);
    setError(message);
    // Re-throw so the Copilot execution chain can detect the failure and record it.
    throw err;
  } finally {
    submitInFlightRef.current = false;
    setSubmitting(false);
  }
}
