import type { MutableRefObject } from 'react';
import { submitAffinityScoring } from '../../api/backendApi';
import type { AffinityPersistedUploads } from '../../hooks/useAffinityWorkflow';
import type { AffinityPreviewPayload, InputComponent, Project, ProjectInputConfig, ProjectTask, ProteinTemplateUpload } from '../../types/models';
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
  workspaceTab: AffinityWorkspaceTab;
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
  setRunMenuOpen: (value: boolean) => void;
  setProjectTasks: (updater: (prev: ProjectTask[]) => ProjectTask[]) => void;
  setProject: (updater: (prev: Project | null) => Project | null) => void;
  setStatusInfo: (value: Record<string, unknown> | null) => void;
  showRunQueuedNotice: (message: string) => void;
  normalizeConfigForBackend: (inputConfig: ProjectInputConfig, backend: string) => ProjectInputConfig;
  computeUseMsaFlag: (components: InputComponent[], fallback?: boolean) => boolean;
  createDraftFingerprint: (draft: AffinityDraftFields) => string;
  createComputationFingerprint: (draft: AffinityDraftFields) => string;
  createProteinTemplatesFingerprint: (templates: Record<string, ProteinTemplateUpload>) => string;
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
  resolveEditableDraftTaskRowId: (options?: { allowLatestDraftFallback?: boolean }) => string | null;
  rememberAffinityUploadsForTaskRow: (taskRowId: string | null, uploads: AffinityPersistedUploads) => void;
  patch: (payload: Partial<Project>) => Promise<Project | null>;
  patchTask: (taskRowId: string, payload: Partial<ProjectTask>) => Promise<ProjectTask | null>;
  updateProjectTask: (taskRowId: string, payload: Partial<ProjectTask>) => Promise<ProjectTask>;
  sortProjectTasks: (rows: ProjectTask[]) => ProjectTask[];
  saveProjectInputConfig: (projectId: string, config: ProjectInputConfig) => void;
}

export async function submitAffinityTaskFromDraft(deps: AffinitySubmitDeps): Promise<void> {
  const {
    project,
    draft,
    workspaceTab,
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
    setRunMenuOpen,
    setProjectTasks,
    setProject,
    setStatusInfo,
    showRunQueuedNotice,
    normalizeConfigForBackend,
    computeUseMsaFlag,
    createDraftFingerprint,
    createComputationFingerprint,
    createProteinTemplatesFingerprint,
    buildAffinityUploadSnapshotComponents,
    persistDraftTaskSnapshot,
    resolveEditableDraftTaskRowId,
    rememberAffinityUploadsForTaskRow,
    patch,
    patchTask,
    updateProjectTask,
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
  const activeAffinityBackend = 'boltz';
  const backendSupportsActivity = true;
  const effectiveConfidenceOnly = backendSupportsActivity ? affinityConfidenceOnly : true;
  const targetChains = affinityTargetChainIds.filter((item) => item.trim());
  const ligandChain = affinityLigandChainId.trim();
  const previewLigandSmiles = String(affinityPreview?.ligandSmiles || '').trim();
  const ligandSmilesInput = affinityLigandSmiles.trim();
  const ligandSmiles = ligandSmilesInput || previewLigandSmiles;
  const runAffinityActivity =
    backendSupportsActivity &&
    !effectiveConfidenceOnly &&
    affinityHasLigand &&
    (affinitySupportsActivity || Boolean(ligandSmiles.trim()));
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
    const storedLigandSmiles = ligandSmiles;
    const snapshotComponents = await buildAffinityUploadSnapshotComponents(
      configWithAffinity.components,
      affinityTargetFile,
      affinityLigandFile,
      storedLigandSmiles
    );

    saveProjectInputConfig(project.id, configWithAffinity);
    const nextDraft: AffinityDraftFields = {
      taskName: draft.taskName.trim(),
      taskSummary: draft.taskSummary.trim(),
      backend: activeAffinityBackend,
      use_msa: hasMsa,
      color_mode: draft.color_mode === 'alphafold' ? 'alphafold' : 'default',
      inputConfig: configWithAffinity
    };
    setDraft(nextDraft);
    setSavedDraftFingerprint(createDraftFingerprint(nextDraft));
    setSavedComputationFingerprint(createComputationFingerprint(nextDraft));
    setSavedTemplateFingerprint(createProteinTemplatesFingerprint(proteinTemplates));
    setRunMenuOpen(false);

    try {
      await patch({
        backend: nextDraft.backend,
        use_msa: nextDraft.use_msa,
        color_mode: nextDraft.color_mode,
        status_text: 'Draft saved',
        protein_sequence: '',
        ligand_smiles: storedLigandSmiles
      });
    } catch (draftPersistError) {
      persistenceWarnings.push(
        `saving draft failed: ${draftPersistError instanceof Error ? draftPersistError.message : 'unknown error'}`
      );
    }

    const draftTaskRow = await persistDraftTaskSnapshot(configWithAffinityTaskOptions, {
      statusText: 'Affinity draft snapshot prepared for run',
      reuseTaskRowId: resolveEditableDraftTaskRowId({ allowLatestDraftFallback: false }),
      snapshotComponents,
      proteinSequenceOverride: '',
      ligandSmilesOverride: storedLigandSmiles
    });
    rememberAffinityUploadsForTaskRow(draftTaskRow.id, affinityCurrentUploads);

    const taskId = await submitAffinityScoring({
      inputStructureText: affinityPreview.structureText,
      inputStructureName: affinityPreview.structureName || 'affinity_input.cif',
      targetFile: affinityTargetFile,
      ligandFile: affinityLigandFile,
      backend: activeAffinityBackend,
      seed: configWithAffinity.options.seed ?? null,
      enableAffinity: runAffinityActivity,
      ligandSmiles,
      targetChainIds: ligandChain ? targetChains : [],
      ligandChainId: ligandChain,
      useMsa: nextDraft.use_msa
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
      ligand_smiles: storedLigandSmiles,
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
      throw new Error(
        `Task submitted (${taskId}) but failed to persist queued task row: ${
          taskPersistError instanceof Error ? taskPersistError.message : 'unknown error'
        }`
      );
    }

    const dbPayload: Partial<Project> = {
      task_id: taskId,
      task_state: 'QUEUED',
      status_text: 'Task submitted and waiting in queue',
      error_text: '',
      backend: activeAffinityBackend,
      protein_sequence: '',
      ligand_smiles: storedLigandSmiles,
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
    const shouldAutoRedirect = workspaceTab !== 'components';
    if (shouldAutoRedirect) {
      setRunRedirectTaskId(taskId);
    } else {
      setRunRedirectTaskId(null);
    }
    if (persistenceWarnings.length > 0) {
      showRunQueuedNotice(`Task ${taskId.slice(0, 8)} queued with sync warning.`);
    } else if (!shouldAutoRedirect) {
      showRunQueuedNotice(`Task ${taskId.slice(0, 8)} queued.`);
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : 'Failed to submit affinity scoring.';
    if (runRedirectTimerRef.current !== null) {
      window.clearTimeout(runRedirectTimerRef.current);
      runRedirectTimerRef.current = null;
    }
    setRunRedirectTaskId(null);
    setError(message);
  } finally {
    submitInFlightRef.current = false;
    setSubmitting(false);
  }
}
