import type { MutableRefObject } from 'react';
import { submitPrediction } from '../../api/backendApi';
import { assignChainIdsForComponents } from '../../utils/chainAssignments';
import { extractPrimaryProteinAndLigand, PEPTIDE_DESIGNED_LIGAND_TOKEN } from '../../utils/projectInputs';
import { buildQueuedPeptidePreviewFromOptions, PEPTIDE_TASK_PREVIEW_KEY } from '../../utils/peptideTaskPreview';
import type { InputComponent, Project, ProjectInputConfig, ProjectTask, ProteinTemplateUpload } from '../../types/models';

export type PredictionWorkspaceTab = 'results' | 'basics' | 'components' | 'constraints';

export interface PredictionDraftFields {
  taskName: string;
  taskSummary: string;
  backend: string;
  use_msa: boolean;
  color_mode: string;
  inputConfig: ProjectInputConfig;
}

export interface PredictionSubmitDeps {
  project: Project;
  draft: PredictionDraftFields;
  isPeptideDesignWorkflow?: boolean;
  workspaceTab: PredictionWorkspaceTab;
  proteinTemplates: Record<string, ProteinTemplateUpload>;
  submitInFlightRef: MutableRefObject<boolean>;
  runRedirectTimerRef: MutableRefObject<number | null>;
  runSuccessNoticeTimerRef: MutableRefObject<number | null>;
  setWorkspaceTab: (value: PredictionWorkspaceTab) => void;
  setSubmitting: (value: boolean) => void;
  setError: (value: string | null) => void;
  setRunRedirectTaskId: (value: string | null) => void;
  setRunSuccessNotice: (value: string | null) => void;
  setDraft: (value: PredictionDraftFields) => void;
  setSavedDraftFingerprint: (value: string) => void;
  setSavedComputationFingerprint: (value: string) => void;
  setSavedTemplateFingerprint: (value: string) => void;
  setRunMenuOpen: (value: boolean) => void;
  setProjectTasks: (updater: (prev: ProjectTask[]) => ProjectTask[]) => void;
  setProject: (updater: (prev: Project | null) => Project | null) => void;
  setStatusInfo: (value: Record<string, unknown> | null) => void;
  showRunQueuedNotice: (message: string) => void;
  normalizeConfigForBackend: (inputConfig: ProjectInputConfig, backend: string) => ProjectInputConfig;
  listIncompleteComponentOrders: (components: InputComponent[]) => number[];
  validateComponents: (components: InputComponent[]) => string | null;
  computeUseMsaFlag: (components: InputComponent[], fallback?: boolean) => boolean;
  createDraftFingerprint: (draft: PredictionDraftFields) => string;
  createComputationFingerprint: (draft: PredictionDraftFields) => string;
  createProteinTemplatesFingerprint: (templates: Record<string, ProteinTemplateUpload>) => string;
  addTemplatesToTaskSnapshotComponents: (
    components: InputComponent[],
    templates: Record<string, ProteinTemplateUpload>
  ) => InputComponent[];
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
  rememberTemplatesForTaskRow: (taskRowId: string | null, templates: Record<string, ProteinTemplateUpload>) => void;
  patch: (payload: Partial<Project>) => Promise<Project | null>;
  patchTask: (taskRowId: string, payload: Partial<ProjectTask>) => Promise<ProjectTask | null>;
  updateProjectTask: (taskRowId: string, payload: Partial<ProjectTask>) => Promise<ProjectTask>;
  sortProjectTasks: (rows: ProjectTask[]) => ProjectTask[];
  saveProjectInputConfig: (projectId: string, config: ProjectInputConfig) => void;
}

function resolveComponentIdByChainId(components: InputComponent[], chainId: string): string | null {
  const normalizedChainId = String(chainId || '').trim();
  if (!normalizedChainId || normalizedChainId === PEPTIDE_DESIGNED_LIGAND_TOKEN) return null;
  const assignments = assignChainIdsForComponents(components);
  for (let index = 0; index < assignments.length; index += 1) {
    const chainIds = assignments[index] || [];
    if (!chainIds.includes(normalizedChainId)) continue;
    return components[index]?.id || null;
  }
  return null;
}

function buildQueuedPeptideDesignConfidenceSnapshot(options: ProjectInputConfig['options']): Record<string, unknown> {
  const requestOptions: Record<string, unknown> = {
    ...options
  };
  const peptideDesign: Record<string, unknown> = {};

  const designMode = options.peptideDesignMode;
  if (designMode) {
    requestOptions.peptide_design_mode = designMode;
    peptideDesign.design_mode = designMode;
  }
  if (typeof options.peptideBinderLength === 'number' && Number.isFinite(options.peptideBinderLength)) {
    requestOptions.peptide_binder_length = options.peptideBinderLength;
    peptideDesign.binder_length = Math.max(0, Math.floor(options.peptideBinderLength));
  }
  if (typeof options.peptideIterations === 'number' && Number.isFinite(options.peptideIterations)) {
    requestOptions.peptide_iterations = options.peptideIterations;
    peptideDesign.iterations = Math.max(0, Math.floor(options.peptideIterations));
    peptideDesign.total_generations = Math.max(0, Math.floor(options.peptideIterations));
    peptideDesign.current_generation = 0;
  }
  if (typeof options.peptidePopulationSize === 'number' && Number.isFinite(options.peptidePopulationSize)) {
    requestOptions.peptide_population_size = options.peptidePopulationSize;
    peptideDesign.population_size = Math.max(0, Math.floor(options.peptidePopulationSize));
  }
  if (typeof options.peptideEliteSize === 'number' && Number.isFinite(options.peptideEliteSize)) {
    requestOptions.peptide_elite_size = options.peptideEliteSize;
    peptideDesign.elite_size = Math.max(0, Math.floor(options.peptideEliteSize));
  }
  if (typeof options.peptideMutationRate === 'number' && Number.isFinite(options.peptideMutationRate)) {
    requestOptions.peptide_mutation_rate = options.peptideMutationRate;
    peptideDesign.mutation_rate = options.peptideMutationRate;
  }

  peptideDesign.current_status = 'queued';
  peptideDesign.status_message = 'Task submitted and waiting in queue';

  const progressSnapshot: Record<string, unknown> = {
    current_status: peptideDesign.current_status,
    status_message: peptideDesign.status_message
  };
  if (typeof peptideDesign.current_generation === 'number') progressSnapshot.current_generation = peptideDesign.current_generation;
  if (typeof peptideDesign.total_generations === 'number') progressSnapshot.total_generations = peptideDesign.total_generations;

  return {
    request: {
      options: requestOptions
    },
    peptide_design: peptideDesign,
    progress: progressSnapshot
  };
}

function buildPredictionSubmissionConfig(
  normalizedConfig: ProjectInputConfig,
  isPeptideDesignWorkflow: boolean
): ProjectInputConfig {
  if (!isPeptideDesignWorkflow) return normalizedConfig;
  const ligandSelector = String(normalizedConfig.properties?.binder || normalizedConfig.properties?.ligand || '').trim();
  const designedLigandComponentId = resolveComponentIdByChainId(normalizedConfig.components, ligandSelector);
  const filteredComponents = designedLigandComponentId
    ? normalizedConfig.components.filter((component) => component.id !== designedLigandComponentId)
    : normalizedConfig.components;
  return {
    ...normalizedConfig,
    components: filteredComponents,
    properties: {
      ...normalizedConfig.properties,
      affinity: false,
      ligand: PEPTIDE_DESIGNED_LIGAND_TOKEN,
      binder: PEPTIDE_DESIGNED_LIGAND_TOKEN
    }
  };
}

export async function submitPredictionTaskFromDraft(deps: PredictionSubmitDeps): Promise<void> {
  const {
    project,
    draft,
    isPeptideDesignWorkflow = false,
    workspaceTab,
    proteinTemplates,
    submitInFlightRef,
    runRedirectTimerRef,
    runSuccessNoticeTimerRef,
    setWorkspaceTab,
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
    listIncompleteComponentOrders,
    validateComponents,
    computeUseMsaFlag,
    createDraftFingerprint,
    createComputationFingerprint,
    createProteinTemplatesFingerprint,
    addTemplatesToTaskSnapshotComponents,
    persistDraftTaskSnapshot,
    resolveEditableDraftTaskRowId,
    rememberTemplatesForTaskRow,
    patch,
    patchTask,
    updateProjectTask,
    sortProjectTasks,
    saveProjectInputConfig
  } = deps;

  const normalizedConfig = normalizeConfigForBackend(draft.inputConfig, draft.backend);
  const submissionConfig = buildPredictionSubmissionConfig(normalizedConfig, isPeptideDesignWorkflow);
  const missingOrders = listIncompleteComponentOrders(normalizedConfig.components);
  if (missingOrders.length > 0) {
    const maxShown = 3;
    const shown = missingOrders
      .slice(0, maxShown)
      .map((order) => `#${order}`)
      .join(', ');
    const suffix = missingOrders.length > maxShown ? ` and ${missingOrders.length - maxShown} more` : '';
    setWorkspaceTab('components');
    setError(`Please complete all components before running. Missing input: ${shown}${suffix}.`);
    return;
  }

  const activeComponents = submissionConfig.components;
  const validationError = validateComponents(activeComponents);
  if (validationError) {
    setError(validationError);
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
    const { proteinSequence, ligandSmiles } = extractPrimaryProteinAndLigand(submissionConfig);
    const hasMsa = computeUseMsaFlag(activeComponents, draft.use_msa);
    const persistenceWarnings: string[] = [];

    saveProjectInputConfig(project.id, normalizedConfig);
    const nextDraft: PredictionDraftFields = {
      taskName: draft.taskName.trim(),
      taskSummary: draft.taskSummary.trim(),
      backend: draft.backend,
      use_msa: hasMsa,
      color_mode: draft.color_mode === 'alphafold' ? 'alphafold' : 'default',
      inputConfig: normalizedConfig
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
        protein_sequence: proteinSequence,
        ligand_smiles: ligandSmiles,
        color_mode: nextDraft.color_mode,
        status_text: 'Draft saved'
      });
    } catch (draftPersistError) {
      persistenceWarnings.push(
        `saving draft failed: ${draftPersistError instanceof Error ? draftPersistError.message : 'unknown error'}`
      );
    }

    const snapshotComponents = addTemplatesToTaskSnapshotComponents(submissionConfig.components, proteinTemplates);
    const draftTaskRow = await persistDraftTaskSnapshot(submissionConfig, {
      statusText: 'Draft snapshot prepared for run',
      reuseTaskRowId: resolveEditableDraftTaskRowId(),
      snapshotComponents
    });
    rememberTemplatesForTaskRow(draftTaskRow.id, proteinTemplates);

    const activeAssignments = assignChainIdsForComponents(activeComponents);
    const templateUploads: NonNullable<Parameters<typeof submitPrediction>[0]['templateUploads']> = [];
    activeComponents.forEach((comp, index) => {
      if (comp.type !== 'protein') return;
      const template = proteinTemplates[comp.id];
      if (!template) return;
      const targetChainIds = activeAssignments[index] || [];
      const suffix = template.format === 'pdb' ? '.pdb' : '.cif';
      templateUploads.push({
        fileName: `template_${comp.id}${suffix}`,
        format: template.format,
        content: template.content,
        templateChainId: template.chainId,
        targetChainIds
      });
    });

    const taskId = await submitPrediction({
      projectId: project.id,
      projectName: project.name,
      proteinSequence,
      ligandSmiles,
      workflow: isPeptideDesignWorkflow ? 'peptide_design' : 'prediction',
      components: activeComponents,
      constraints: submissionConfig.constraints,
      properties: submissionConfig.properties,
      peptideDesignOptions: isPeptideDesignWorkflow ? submissionConfig.options : undefined,
      peptideDesignTargetChainId: isPeptideDesignWorkflow ? submissionConfig.properties.target : null,
      seed: submissionConfig.options.seed,
      backend: draft.backend,
      useMsa: hasMsa,
      templateUploads
    });

    const queuedAt = new Date().toISOString();
    const queuedTaskProperties: ProjectTask['properties'] = (() => {
      if (!isPeptideDesignWorkflow) return submissionConfig.properties;
      const preview = buildQueuedPeptidePreviewFromOptions(submissionConfig.options as unknown as Record<string, unknown>);
      if (Object.keys(preview).length === 0) return submissionConfig.properties;
      return {
        ...(submissionConfig.properties as unknown as Record<string, unknown>),
        [PEPTIDE_TASK_PREVIEW_KEY]: preview
      } as unknown as ProjectTask['properties'];
    })();
    const queuedTaskPatch: Partial<ProjectTask> = {
      name: nextDraft.taskName.trim(),
      summary: nextDraft.taskSummary.trim(),
      task_id: taskId,
      task_state: 'QUEUED',
      status_text: 'Task submitted and waiting in queue',
      error_text: '',
      backend: draft.backend,
      seed: submissionConfig.options.seed ?? null,
      protein_sequence: proteinSequence,
      ligand_smiles: ligandSmiles,
      components: snapshotComponents,
      constraints: submissionConfig.constraints,
      properties: queuedTaskProperties,
      confidence: isPeptideDesignWorkflow
        ? buildQueuedPeptideDesignConfidenceSnapshot(submissionConfig.options)
        : {},
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
    const message = err instanceof Error ? err.message : 'Failed to submit prediction.';
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
