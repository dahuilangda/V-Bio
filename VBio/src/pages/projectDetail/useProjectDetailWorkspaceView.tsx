import { useCallback, useRef, useState, type RefObject } from 'react';
import { Link } from 'react-router-dom';
import type { PredictionConstraint } from '../../types/models';
import { downloadResultFile } from '../../api/backendApi';
import { createInputComponent } from '../../utils/projectInputs';
import { getWorkflowDefinition } from '../../utils/workflows';
import { ProjectDetailLayout } from './ProjectDetailLayout';
import {
  computeUseMsaFlag,
  filterConstraintsByBackend,
} from './projectDraftUtils';
import { useProjectResultDisplay } from './useProjectResultDisplay';
import { useProjectRunHandlers } from './useProjectRunHandlers';
import {
  constraintLabel,
  formatConstraintCombo as formatConstraintComboForWorkspace,
  formatConstraintDetail as formatConstraintDetailForWorkspace
} from './constraintWorkspaceUtils';
import { useConstraintWorkspaceActions } from './useConstraintWorkspaceActions';
import { scrollToEditorBlock } from './editorActions';
import { useProjectEditorHandlers } from './useProjectEditorHandlers';
import { useProjectSidebarActions } from './useProjectSidebarActions';
import { useProjectWorkflowSectionProps } from './useProjectWorkflowSectionProps';
import { useProjectRunState } from './useProjectRunState';
import { usePredictionWorkspaceProps } from './usePredictionWorkspaceProps';
import { useProjectDetailRuntimeContext } from './useProjectDetailRuntimeContext';
import { buildLeadOptUploadSnapshotComponents, type LeadOptPersistedUploads } from './projectTaskSnapshot';
import { buildLeadOptCandidatesUiStateSignature, type LeadOptCandidatesUiState } from '../../components/project/leadopt/LeadOptCandidatesPanel';
import type { LeadOptPredictionRecord } from '../../components/project/leadopt/hooks/useLeadOptMmpQueryMachine';

function readText(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value);
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
}

function asPredictionRecordMap(value: unknown): Record<string, LeadOptPredictionRecord> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  return value as Record<string, LeadOptPredictionRecord>;
}

function summarizeLeadOptPredictions(records: Record<string, LeadOptPredictionRecord>) {
  let queued = 0;
  let running = 0;
  let success = 0;
  let failure = 0;
  for (const record of Object.values(records)) {
    const token = String(record.state || '').toUpperCase();
    if (token === 'QUEUED') queued += 1;
    else if (token === 'RUNNING') running += 1;
    else if (token === 'SUCCESS') success += 1;
    else if (token === 'FAILURE') failure += 1;
  }
  return {
    total: Object.keys(records).length,
    queued,
    running,
    success,
    failure
  };
}

function asRecordArray(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.filter((item) => item && typeof item === 'object' && !Array.isArray(item)) as Array<Record<string, unknown>>;
}

function toFiniteNumber(value: unknown): number | null {
  const numeric = typeof value === 'number' ? value : typeof value === 'string' ? Number(value) : Number.NaN;
  if (!Number.isFinite(numeric)) return null;
  return numeric;
}

function readBooleanToken(value: unknown): boolean | null {
  if (value === true) return true;
  if (value === false) return false;
  const token = readText(value).trim().toLowerCase();
  if (!token) return null;
  if (token === '1' || token === 'true' || token === 'yes' || token === 'on') return true;
  if (token === '0' || token === 'false' || token === 'no' || token === 'off') return false;
  return null;
}

function compactLeadOptPredictionRecord(value: LeadOptPredictionRecord): LeadOptPredictionRecord {
  return {
    taskId: readText(value.taskId).trim(),
    state: value.state,
    backend: readText(value.backend).trim().toLowerCase() || 'boltz',
    pairIptm: toFiniteNumber(value.pairIptm),
    pairPae: toFiniteNumber(value.pairPae),
    pairIptmResolved: value.pairIptmResolved === true,
    ligandPlddt: toFiniteNumber(value.ligandPlddt),
    ligandAtomPlddts: [],
    structureText: '',
    structureFormat: readText(value.structureFormat).toLowerCase() === 'pdb' ? 'pdb' : 'cif',
    structureName: readText(value.structureName).trim(),
    error: readText(value.error),
    updatedAt: Number.isFinite(Number(value.updatedAt)) ? Number(value.updatedAt) : Date.now()
  };
}

function compactLeadOptPredictionMap(value: Record<string, LeadOptPredictionRecord>): Record<string, LeadOptPredictionRecord> {
  const out: Record<string, LeadOptPredictionRecord> = {};
  for (const [smiles, record] of Object.entries(value)) {
    const key = readText(smiles).trim();
    if (!key) continue;
    out[key] = compactLeadOptPredictionRecord(record);
  }
  return out;
}

function buildLeadOptPredictionPersistSignature(records: Record<string, LeadOptPredictionRecord>): string {
  return Object.entries(records)
    .map(([key, record]) => {
      const normalizedKey = readText(key).trim();
      const taskId = readText(record.taskId).trim();
      const state = readText(record.state).trim().toUpperCase();
      const backend = readText(record.backend).trim().toLowerCase() || 'boltz';
      const pairIptm = toFiniteNumber(record.pairIptm);
      const pairPae = toFiniteNumber(record.pairPae);
      const ligandPlddt = toFiniteNumber(record.ligandPlddt);
      const error = readText(record.error).trim();
      return [
        normalizedKey,
        taskId,
        state,
        backend,
        pairIptm === null ? '' : pairIptm.toFixed(4),
        pairPae === null ? '' : pairPae.toFixed(3),
        record.pairIptmResolved === true ? '1' : '0',
        ligandPlddt === null ? '' : ligandPlddt.toFixed(3),
        error
      ].join('~');
    })
    .sort((a, b) => a.localeCompare(b))
    .join('||');
}

function compactLeadOptCandidatesUiState(value: LeadOptCandidatesUiState): LeadOptCandidatesUiState {
  return {
    selectedBackend: readText(value.selectedBackend).trim().toLowerCase() || 'boltz',
    stateFilter: value.stateFilter,
    showAdvanced: value.showAdvanced === true,
    mwMin: readText(value.mwMin).trim(),
    mwMax: readText(value.mwMax).trim(),
    logpMin: readText(value.logpMin).trim(),
    logpMax: readText(value.logpMax).trim(),
    tpsaMin: readText(value.tpsaMin).trim(),
    tpsaMax: readText(value.tpsaMax).trim(),
    plddtMin: readText(value.plddtMin).trim(),
    plddtMax: readText(value.plddtMax).trim(),
    iptmMin: readText(value.iptmMin).trim(),
    iptmMax: readText(value.iptmMax).trim(),
    paeMin: readText(value.paeMin).trim(),
    paeMax: readText(value.paeMax).trim(),
    structureSearchMode: value.structureSearchMode,
    structureSearchQuery: readText(value.structureSearchQuery).trim(),
    previewRenderMode: value.previewRenderMode
  };
}

function buildLeadOptSelectionFromPayload(payload: Record<string, unknown>, context: {
  querySmiles: string;
  targetChain: string;
  ligandChain: string;
}) {
  const variableSpec = asRecord(payload.variable_spec);
  const variableItems = asRecordArray(variableSpec.items).map((item) => {
    const atomIndices = Array.isArray(item.atom_indices)
      ? item.atom_indices
        .map((value) => Number(value))
        .filter((value) => Number.isFinite(value) && value >= 0)
        .map((value) => Math.floor(value))
      : [];
    return {
      query: readText(item.query).trim(),
      mode: readText(item.mode).trim() || 'substructure',
      fragment_id: readText(item.fragment_id).trim(),
      atom_indices: atomIndices
    };
  });
  const selectedFragmentIds = Array.from(
    new Set(variableItems.map((item) => readText(item.fragment_id).trim()).filter(Boolean))
  );
  const selectedFragmentAtomIndices = Array.from(
    new Set(
      variableItems.flatMap((item) => item.atom_indices || [])
    )
  );
  const variableQueries = Array.from(
    new Set(variableItems.map((item) => readText(item.query).trim()).filter(Boolean))
  );
  const groupedByEnvironmentValue = readBooleanToken(payload.grouped_by_environment);
  const groupedByEnvironmentMode =
    groupedByEnvironmentValue === true ? 'on' : groupedByEnvironmentValue === false ? 'off' : 'auto';
  const propertyTargets = asRecord(payload.property_targets);
  const queryProperty = readText(propertyTargets.property).trim();
  const directionToken = readText(propertyTargets.direction).trim().toLowerCase();
  const direction = directionToken === 'increase' || directionToken === 'decrease' ? directionToken : '';
  const minPairsRaw = Number(payload.min_pairs);
  const minPairs = Number.isFinite(minPairsRaw) ? Math.max(1, Math.floor(minPairsRaw)) : 1;
  const envRadiusRaw = Number(payload.rule_env_radius);
  const envRadius = Number.isFinite(envRadiusRaw) ? Math.max(0, Math.floor(envRadiusRaw)) : 1;
  return {
    query_smiles: readText(context.querySmiles).trim(),
    target_chain: readText(context.targetChain).trim(),
    ligand_chain: readText(context.ligandChain).trim(),
    selected_fragment_ids: selectedFragmentIds,
    selected_fragment_atom_indices: selectedFragmentAtomIndices,
    variable_queries: variableQueries,
    variable_items: variableItems,
    grouped_by_environment_mode: groupedByEnvironmentMode,
    query_property: queryProperty,
    direction,
    min_pairs: minPairs,
    env_radius: envRadius
  };
}

type WorkspaceRuntime = ReturnType<typeof useProjectDetailRuntimeContext>;
type WorkspaceRuntimeReady = WorkspaceRuntime & {
  project: NonNullable<WorkspaceRuntime['project']>;
  draft: NonNullable<WorkspaceRuntime['draft']>;
};

export function useProjectDetailWorkspaceView() {
  const runtime = useProjectDetailRuntimeContext();
  const { locationSearch, entryRoutingResolved, loading, error, project, draft } = runtime;

  if (!entryRoutingResolved || loading) {
    const query = new URLSearchParams(locationSearch);
    const requestedTaskRowId = String(query.get('task_row_id') || '').trim();
    const loadingLabel =
      !entryRoutingResolved
        ? 'Loading project...'
        : requestedTaskRowId || query.get('tab') === 'results'
          ? 'Loading current task...'
          : 'Loading project...';
    return <div className="centered-page">{loadingLabel}</div>;
  }

  if (error && !project) {
    return (
      <div className="page-grid">
        <div className="alert error">{error}</div>
        <Link className="btn btn-ghost" to="/projects">
          Back to projects
        </Link>
      </div>
    );
  }

  if (!project || !draft) {
    return null;
  }

  return <ProjectDetailWorkspaceLoaded runtime={runtime as WorkspaceRuntimeReady} />;
}

function ProjectDetailWorkspaceLoaded({ runtime }: { runtime: WorkspaceRuntimeReady }) {
  const [leadOptHeaderRunAction, setLeadOptHeaderRunAction] = useState<(() => void | Promise<void>) | null>(null);
  const [leadOptHeaderRunPending, setLeadOptHeaderRunPending] = useState(false);
  const handleRegisterLeadOptHeaderRunAction = useCallback((action: (() => void | Promise<void>) | null) => {
    setLeadOptHeaderRunAction(() => action);
  }, []);
  const {
    loading,
    error,
    setError,
    project,
    draft,
    isPredictionWorkflow,
    isPeptideDesignWorkflow,
    isAffinityWorkflow,
    isLeadOptimizationWorkflow,
    workspaceTab,
    hasIncompleteComponents,
    componentCompletion,
    submitting,
    saving,
    runRedirectTaskId,
    showFloatingRunButton,
    affinityTargetFile,
    affinityPreviewLoading,
    affinityPreviewCurrent,
    affinityPreviewError,
    affinityTargetChainIds,
    affinityLigandChainId,
    affinityLigandSmiles,
    affinityHasLigand,
    affinitySupportsActivity,
    affinityConfidenceOnly,
    affinityConfidenceOnlyLocked,
    chainInfoById,
    componentTypeBuckets,
    setDraft,
    setWorkspaceTab,
    setActiveComponentId,
    setSidebarTypeOpen,
    normalizedDraftComponents,
    setSidebarConstraintsOpen,
    setActiveConstraintId,
    setSelectedContactConstraintIds,
    constraintSelectionAnchorRef,
    activeChainInfos,
    ligandChainOptions,
    isBondOnlyBackend,
    canEnableAffinityFromWorkspace,
    workspaceTargetOptions,
    workspaceLigandSelectableOptions,
    activeConstraintId,
    selectedContactConstraintIds,
    selectedConstraintTemplateComponentId,
    setSelectedConstraintTemplateComponentId,
    resolveTemplateComponentIdForConstraint,
    constraintPickSlotRef,
    selectedTemplatePreview,
    selectedTemplateResidueIndexMap,
    setPickedResidue,
    canEdit,
    structureText,
    structureFormat,
    structureTaskId,
    confidenceBackend,
    projectBackend,
    activeResultTask,
    hasAf3ConfidenceSignals,
    hasProtenixConfidenceSignals,
    selectedResultTargetChainId,
    selectedResultLigandChainId,
    resultChainShortLabelById,
    snapshotPlddt,
    snapshotSelectedLigandChainPlddt,
    snapshotLigandMeanPlddt,
    snapshotPlddtTone,
    snapshotIptm,
    snapshotSelectedPairIptm,
    snapshotIptmTone,
    snapshotIc50Um,
    snapshotIc50Error,
    snapshotIc50Tone,
    snapshotBindingProbability,
    snapshotBindingStd,
    snapshotBindingTone,
    affinityPreviewTargetStructureText,
    affinityPreviewTargetStructureFormat,
    affinityPreview,
    affinityPreviewLigandStructureText,
    affinityPreviewLigandStructureFormat,
    snapshotAffinity,
    snapshotConfidence,
    statusInfo,
    statusContextTaskRow,
    requestedStatusTaskRow,
    projectTasks,
    snapshotLigandAtomPlddts,
    overviewPrimaryLigand,
    selectedResultLigandSequence,
    selectedResultLigandComponent,
    snapshotLigandResiduePlddts,
    setProteinTemplates,
    constraintsWorkspaceRef,
    isConstraintsResizing,
    constraintsGridStyle,
    constraintCount,
    activeConstraintIndex,
    constraintTemplateOptions,
    pickedResidue,
    constraintPickModeEnabled,
    setConstraintPickModeEnabled,
    constraintViewerHighlightResidues,
    constraintViewerActiveResidue,
    handleConstraintsResizerPointerDown,
    handleConstraintsResizerKeyDown,
    allowedConstraintTypes,
    sidebarTypeOpen,
    activeComponentId,
    sidebarConstraintsOpen,
    selectedContactConstraintIdSet,
    selectedWorkspaceTarget,
    selectedWorkspaceLigand,
    affinityEnableDisabledReason,
    isResultsResizing,
    resultsGridRef,
    handleResultsResizerPointerDown,
    handleResultsResizerKeyDown,
    resultsGridStyle,
    resultChainIds,
    onAffinityTargetFileChange,
    onAffinityLigandFileChange,
    onAffinityUseMsaChange,
    onAffinityConfidenceOnlyChange,
    setAffinityLigandSmiles,
    leadOptPrimary,
    leadOptChainContext,
    leadOptPersistedUploads,
    componentsWorkspaceRef,
    isComponentsResizing,
    componentsGridStyle,
    handleComponentsResizerPointerDown,
    handleComponentsResizerKeyDown,
    proteinTemplates,
    displayTaskState,
    isActiveRuntime,
    progressPercent,
    waitingSeconds,
    totalRuntimeSeconds,
    hasUnsavedChanges,
    runMenuOpen,
    runSuccessNotice,
    resultError,
    resultChainConsistencyWarning,
    runActionRef,
    topRunButtonRef,
    patchTask,
    pullResultForViewer,
    persistDraftTaskSnapshot,
    submitTask,
    setRunMenuOpen,
    loadProject,
    saveDraft,
    setRunRedirectTaskId,
    navigate,
    affinityLigandFile
  } = runtime;
  const leadOptMmpTaskRowMapRef = useRef<Record<string, string>>({});
  const leadOptUploadPersistKeyRef = useRef('');
  const leadOptActiveTaskRowIdRef = useRef('');
  const leadOptPredictionPersistKeyRef = useRef('');
  const leadOptUiStatePersistKeyRef = useRef('');
  const leadOptMmpContextByTaskIdRef = useRef<Record<string, Record<string, unknown>>>({});

  const resolveLeadOptTaskRowId = useCallback((): string => {
    const contextRowId = readText(statusContextTaskRow?.id).trim();
    if (contextRowId) return contextRowId;

    const activeResultRowId = readText(activeResultTask?.id).trim();
    if (activeResultRowId) return activeResultRowId;

    const requestedRowId = readText(requestedStatusTaskRow?.id).trim();
    if (requestedRowId) return requestedRowId;

    const cachedRowId = readText(leadOptActiveTaskRowIdRef.current).trim();
    if (cachedRowId) return cachedRowId;

    const fallback = projectTasks.find((row) => {
      const leadOpt = asRecord(asRecord(row.confidence).lead_opt_mmp);
      return Object.keys(leadOpt).length > 0;
    });
    return readText(fallback?.id).trim();
  }, [activeResultTask, projectTasks, requestedStatusTaskRow, statusContextTaskRow]);

  const resolveLeadOptSourceTask = useCallback(
    (taskRowId: string) => {
      const id = readText(taskRowId).trim();
      if (!id) return null;
      if (statusContextTaskRow && String(statusContextTaskRow.id) === id) return statusContextTaskRow;
      if (activeResultTask && String(activeResultTask.id) === id) return activeResultTask;
      if (requestedStatusTaskRow && String(requestedStatusTaskRow.id) === id) return requestedStatusTaskRow;
      return projectTasks.find((row) => String(row.id) === id) || null;
    },
    [activeResultTask, projectTasks, requestedStatusTaskRow, statusContextTaskRow]
  );

  const handleLeadOptMmpTaskQueued = async (payload: {
    taskId: string;
    requestPayload: Record<string, unknown>;
    querySmiles: string;
    referenceUploads: LeadOptPersistedUploads;
  }) => {
    if (!project || !draft) return;
    const taskId = String(payload.taskId || '').trim();
    if (!taskId) return;
    const effectiveLeadOptLigandSmiles =
      readText(payload.querySmiles).trim() || readText(leadOptPrimary.ligandSmiles).trim();
    const snapshotComponents = buildLeadOptUploadSnapshotComponents(
      draft.inputConfig.components,
      payload.referenceUploads,
      effectiveLeadOptLigandSmiles
    );
    const queuedAt = new Date().toISOString();
    const selection = buildLeadOptSelectionFromPayload(payload.requestPayload || {}, {
      querySmiles: payload.querySmiles || leadOptPrimary.ligandSmiles,
      targetChain: leadOptChainContext.targetChain,
      ligandChain: leadOptChainContext.ligandChain
    });
    const mmpContext = {
      query_payload: payload.requestPayload || {},
      selection,
      target_chain: readText(leadOptChainContext.targetChain).trim(),
      ligand_chain: readText(leadOptChainContext.ligandChain).trim()
    } as Record<string, unknown>;
    const draftTaskRow = await persistDraftTaskSnapshot(draft.inputConfig, {
      statusText: 'Lead optimization MMP query queued',
      reuseTaskRowId: null,
      snapshotComponents,
      proteinSequenceOverride: leadOptPrimary.proteinSequence,
      ligandSmilesOverride: effectiveLeadOptLigandSmiles
    });
    leadOptMmpTaskRowMapRef.current[taskId] = draftTaskRow.id;
    leadOptActiveTaskRowIdRef.current = draftTaskRow.id;
    leadOptMmpContextByTaskIdRef.current[taskId] = mmpContext;
    await patchTask(draftTaskRow.id, {
      task_id: taskId,
      task_state: 'QUEUED',
      status_text: 'MMP query queued',
      error_text: '',
      submitted_at: queuedAt,
      completed_at: null,
      duration_seconds: null,
      components: snapshotComponents,
      confidence: {
        lead_opt_mmp: {
          stage: 'queued',
          prediction_stage: 'idle',
          prediction_summary: {
            total: 0,
            queued: 0,
            running: 0,
            success: 0,
            failure: 0
          },
          prediction_by_smiles: {},
          reference_prediction_by_backend: {},
          ...mmpContext
        }
      }
    });
    setRunRedirectTaskId(taskId);
  };

  const handleLeadOptMmpTaskCompleted = async (payload: {
    taskId: string;
    queryId: string;
    transformCount: number;
    candidateCount: number;
    elapsedSeconds: number;
    resultSnapshot?: Record<string, unknown>;
  }) => {
    const taskId = String(payload.taskId || '').trim();
    if (!taskId) return;
    const taskRowId = leadOptMmpTaskRowMapRef.current[taskId];
    if (!taskRowId) return;
    leadOptActiveTaskRowIdRef.current = taskRowId;
    const completedAt = new Date().toISOString();
    const mmpContext = asRecord(leadOptMmpContextByTaskIdRef.current[taskId]);
    const snapshot = asRecord(payload.resultSnapshot);
    const queryResult = asRecord(snapshot.query_result);
    const enumeratedCandidates = Array.isArray(snapshot.enumerated_candidates) ? snapshot.enumerated_candidates : [];
    const uiState = asRecord(snapshot.ui_state);
    const compactQueryResult = {
      query_id: readText(payload.queryId).trim(),
      query_mode: readText(queryResult.query_mode).trim() || 'one-to-many',
      mmp_database_id: readText(queryResult.mmp_database_id).trim(),
      mmp_database_label: readText(queryResult.mmp_database_label).trim(),
      mmp_database_schema: readText(queryResult.mmp_database_schema).trim(),
      property_targets: asRecord(queryResult.property_targets),
      rule_env_radius: Number.isFinite(Number(queryResult.rule_env_radius)) ? Number(queryResult.rule_env_radius) : 1,
      grouped_by_environment:
        readBooleanToken(queryResult.grouped_by_environment) === null
          ? undefined
          : readBooleanToken(queryResult.grouped_by_environment),
      count: Number.isFinite(Number(queryResult.count)) ? Number(queryResult.count) : payload.transformCount,
      global_count: Number.isFinite(Number(queryResult.global_count)) ? Number(queryResult.global_count) : payload.transformCount,
      min_pairs: Number.isFinite(Number(queryResult.min_pairs)) ? Number(queryResult.min_pairs) : 1,
      stats: asRecord(queryResult.stats),
      transforms: Array.isArray(queryResult.transforms) ? queryResult.transforms : [],
      global_transforms: Array.isArray(queryResult.global_transforms) ? queryResult.global_transforms : [],
      clusters: Array.isArray(queryResult.clusters) ? queryResult.clusters : []
    } as Record<string, unknown>;
    await patchTask(taskRowId, {
      task_state: 'SUCCESS',
      status_text: `MMP complete (${payload.transformCount} transforms, ${payload.candidateCount} rows)`,
      error_text: '',
      completed_at: completedAt,
      duration_seconds: Number.isFinite(payload.elapsedSeconds) ? payload.elapsedSeconds : null,
      confidence: {
        lead_opt_mmp: {
          stage: 'completed',
          query_id: payload.queryId,
          transform_count: payload.transformCount,
          candidate_count: payload.candidateCount,
          query_result: compactQueryResult,
          result_storage: 'server_query_cache',
          enumerated_candidates: enumeratedCandidates,
          ui_state: uiState,
          prediction_stage: 'idle',
          prediction_summary: {
            total: 0,
            queued: 0,
            running: 0,
            success: 0,
            failure: 0
          },
          prediction_by_smiles: {},
          reference_prediction_by_backend: {},
          ...mmpContext
        }
      }
    });
    delete leadOptMmpTaskRowMapRef.current[taskId];
    delete leadOptMmpContextByTaskIdRef.current[taskId];
  };

  const handleLeadOptMmpTaskFailed = async (payload: { taskId: string; error: string }) => {
    const taskId = String(payload.taskId || '').trim();
    if (!taskId) return;
    const taskRowId = leadOptMmpTaskRowMapRef.current[taskId];
    if (!taskRowId) return;
    leadOptActiveTaskRowIdRef.current = taskRowId;
    const completedAt = new Date().toISOString();
    const mmpContext = asRecord(leadOptMmpContextByTaskIdRef.current[taskId]);
    await patchTask(taskRowId, {
      task_state: 'FAILURE',
      status_text: 'MMP query failed',
      error_text: payload.error || 'MMP query failed.',
      completed_at: completedAt,
      confidence: {
        lead_opt_mmp: {
          stage: 'failed',
          prediction_stage: 'idle',
          prediction_summary: {
            total: 0,
            queued: 0,
            running: 0,
            success: 0,
            failure: 0
          },
          prediction_by_smiles: {},
          reference_prediction_by_backend: {},
          ...mmpContext
        }
      }
    });
    delete leadOptMmpTaskRowMapRef.current[taskId];
    delete leadOptMmpContextByTaskIdRef.current[taskId];
  };

  const handleLeadOptPredictionQueued = useCallback(
    async (payload: { taskId: string; backend: string; candidateSmiles: string }) => {
      if (!project) return;
      const taskId = readText(payload.taskId).trim();
      if (!taskId) return;
      const taskRowId = resolveLeadOptTaskRowId();
      if (!taskRowId) return;
      leadOptActiveTaskRowIdRef.current = taskRowId;

      const candidateSmiles = readText(payload.candidateSmiles).trim() || leadOptPrimary.ligandSmiles;
      const sourceTask = resolveLeadOptSourceTask(taskRowId);
      const sourceConfidence = asRecord(sourceTask?.confidence);
      const sourceLeadOpt = asRecord(sourceConfidence.lead_opt_mmp);
      const mergedRecords = compactLeadOptPredictionMap(asPredictionRecordMap(sourceLeadOpt.prediction_by_smiles));
      const mergedReferenceByBackend = compactLeadOptPredictionMap(
        asPredictionRecordMap(sourceLeadOpt.reference_prediction_by_backend)
      );
      if (!mergedRecords[candidateSmiles]) {
        mergedRecords[candidateSmiles] = {
          taskId,
          state: 'QUEUED',
          backend: readText(payload.backend).trim().toLowerCase() || 'boltz',
          pairIptm: null,
          pairPae: null,
          pairIptmResolved: false,
          ligandPlddt: null,
          ligandAtomPlddts: [],
          structureText: '',
          structureFormat: 'cif',
          structureName: '',
          error: '',
          updatedAt: Date.now()
        };
      }
      const summary = summarizeLeadOptPredictions(mergedRecords);
      const unresolved = summary.queued + summary.running;
      await patchTask(taskRowId, {
        task_state: 'QUEUED',
        status_text: unresolved > 0 ? `Scoring ${unresolved} queued (${summary.success}/${summary.total} done)` : 'Scoring queued',
        error_text: '',
        completed_at: null,
        confidence: {
          ...sourceConfidence,
          lead_opt_mmp: {
            ...sourceLeadOpt,
            stage: 'prediction_queued',
            prediction_candidate_smiles: candidateSmiles,
            prediction_task_id: taskId,
            prediction_state: 'QUEUED',
            prediction_stage: 'queued',
            prediction_summary: {
              ...summary,
              latest_task_id: taskId
            },
            bucket_count: summary.total,
            prediction_by_smiles: mergedRecords,
            reference_prediction_by_backend: mergedReferenceByBackend,
            query_id: readText(sourceLeadOpt.query_id).trim(),
            selection: asRecord(sourceLeadOpt.selection),
            target_chain: readText(sourceLeadOpt.target_chain).trim(),
            ligand_chain: readText(sourceLeadOpt.ligand_chain).trim()
          }
        }
      });
    },
    [
      leadOptPrimary.ligandSmiles,
      patchTask,
      project,
      resolveLeadOptSourceTask,
      resolveLeadOptTaskRowId
    ]
  );

  const handleLeadOptPredictionStateChange = useCallback(
    async (payload: {
      records: Record<string, LeadOptPredictionRecord>;
      referenceRecords: Record<string, LeadOptPredictionRecord>;
      summary: {
        total: number;
        queued: number;
        running: number;
        success: number;
        failure: number;
        latestTaskId: string;
      };
    }) => {
      const taskRowId = resolveLeadOptTaskRowId();
      if (!taskRowId) return;
      leadOptActiveTaskRowIdRef.current = taskRowId;

      const records = compactLeadOptPredictionMap(asPredictionRecordMap(payload.records));
      const referenceRecords = compactLeadOptPredictionMap(asPredictionRecordMap(payload.referenceRecords));
      const summary = summarizeLeadOptPredictions(records);
      const unresolved = summary.queued + summary.running;
      const unresolvedState = summary.running > 0 ? 'RUNNING' : summary.queued > 0 ? 'QUEUED' : null;
      const nextState =
        unresolvedState || (summary.total > 0 && summary.success === 0 && summary.failure > 0 ? 'FAILURE' : 'SUCCESS');
      const statusText =
        unresolved > 0
          ? unresolvedState === 'RUNNING'
            ? `Scoring ${unresolved} running (${summary.success}/${summary.total} done)`
            : `Scoring ${unresolved} queued (${summary.success}/${summary.total} done)`
          : summary.total > 0
            ? `Scoring complete (${summary.success}/${summary.total})`
            : 'MMP complete';
      const errorText = summary.total > 0 && summary.success === 0 && summary.failure > 0 ? 'All candidate scoring jobs failed.' : '';
      const completedAt = unresolved > 0 ? null : new Date().toISOString();

      const sourceTask = resolveLeadOptSourceTask(taskRowId);
      const sourceConfidence = asRecord(sourceTask?.confidence);
      const sourceLeadOpt = asRecord(sourceConfidence.lead_opt_mmp);
      const nextConfidence = {
        ...sourceConfidence,
        lead_opt_mmp: {
          ...sourceLeadOpt,
          stage:
            unresolved > 0
              ? unresolvedState === 'RUNNING'
                ? 'prediction_running'
                : 'prediction_queued'
              : summary.failure > 0 && summary.success === 0
                ? 'prediction_failed'
                : 'prediction_completed',
          prediction_stage: unresolved > 0 ? (unresolvedState === 'RUNNING' ? 'running' : 'queued') : 'completed',
          prediction_summary: {
            ...summary,
            latest_task_id: readText(payload.summary?.latestTaskId).trim()
          },
          bucket_count: summary.total,
          prediction_by_smiles: records,
          reference_prediction_by_backend: referenceRecords
        }
      };

      const persistKey = [
        taskRowId,
        nextState,
        statusText,
        errorText,
        summary.total,
        summary.queued,
        summary.running,
        summary.success,
        summary.failure,
        readText(payload.summary?.latestTaskId).trim(),
        buildLeadOptPredictionPersistSignature(records),
        buildLeadOptPredictionPersistSignature(referenceRecords)
      ].join('|');
      if (leadOptPredictionPersistKeyRef.current === persistKey) return;
      leadOptPredictionPersistKeyRef.current = persistKey;

      await patchTask(taskRowId, {
        task_state: nextState,
        status_text: statusText,
        error_text: errorText,
        completed_at: completedAt,
        confidence: nextConfidence
      });
    },
    [patchTask, resolveLeadOptSourceTask, resolveLeadOptTaskRowId]
  );

  const handleLeadOptUiStateChange = useCallback(
    async (payload: { uiState: LeadOptCandidatesUiState }) => {
      if (!project) return;
      const taskRowId = resolveLeadOptTaskRowId();
      if (!taskRowId) return;
      leadOptActiveTaskRowIdRef.current = taskRowId;
      const sourceTask = resolveLeadOptSourceTask(taskRowId);
      const sourceConfidence = asRecord(sourceTask?.confidence);
      const sourceLeadOpt = asRecord(sourceConfidence.lead_opt_mmp);
      if (Object.keys(sourceLeadOpt).length === 0) return;
      const compactUiState = compactLeadOptCandidatesUiState(payload.uiState);
      const persistKey = [
        taskRowId,
        buildLeadOptCandidatesUiStateSignature(compactUiState),
        readText(sourceLeadOpt.query_id || asRecord(sourceLeadOpt.query_result).query_id).trim()
      ].join('|');
      if (leadOptUiStatePersistKeyRef.current === persistKey) return;
      leadOptUiStatePersistKeyRef.current = persistKey;
      await patchTask(taskRowId, {
        confidence: {
          ...sourceConfidence,
          lead_opt_mmp: {
            ...sourceLeadOpt,
            ui_state: compactUiState
          }
        }
      });
    },
    [patchTask, project, resolveLeadOptSourceTask, resolveLeadOptTaskRowId]
  );

  const handleLeadOptReferenceUploadsChange = useCallback(
    async (uploads: LeadOptPersistedUploads) => {
      if (!project || !draft || !canEdit) return;
      if (workspaceTab !== 'components') return;
      const targetName = readText(uploads.target?.fileName).trim();
      const targetSize = readText(uploads.target?.content).length;
      const ligandName = readText(uploads.ligand?.fileName).trim();
      const ligandSize = readText(uploads.ligand?.content).length;
      const contextDraftRowId =
        String(statusContextTaskRow?.task_state || '').toUpperCase() === 'DRAFT'
          ? readText(statusContextTaskRow?.id).trim()
          : '';
      const editableDraftRowId = contextDraftRowId;
      const effectiveLeadOptLigandSmiles = readText(leadOptPrimary.ligandSmiles).trim();
      const dedupeKey = `${project.id}|${editableDraftRowId}|${targetName}:${targetSize}|${ligandName}:${ligandSize}|${effectiveLeadOptLigandSmiles}`;
      if (leadOptUploadPersistKeyRef.current === dedupeKey) return;
      leadOptUploadPersistKeyRef.current = dedupeKey;

      const snapshotComponents = buildLeadOptUploadSnapshotComponents(
        draft.inputConfig.components,
        uploads,
        effectiveLeadOptLigandSmiles
      );

      if (editableDraftRowId) {
        await patchTask(editableDraftRowId, {
          components: snapshotComponents,
          protein_sequence: leadOptPrimary.proteinSequence,
          ligand_smiles: effectiveLeadOptLigandSmiles
        });
      }
    },
    [
      canEdit,
      draft,
      leadOptPrimary.ligandSmiles,
      leadOptPrimary.proteinSequence,
      patchTask,
      project,
      statusContextTaskRow,
      workspaceTab
    ]
  );

  const workflow = getWorkflowDefinition(project.task_type);
  const affinityUseMsa = computeUseMsaFlag(draft.inputConfig.components, draft.use_msa);
  const runSubmitting = submitting || (isLeadOptimizationWorkflow && leadOptHeaderRunPending);
  const leadOptInitialMmpSnapshot = (() => {
    const sourceTask =
      statusContextTaskRow ||
      activeResultTask ||
      null;
    const leadOptMmp = asRecord(asRecord(sourceTask?.confidence).lead_opt_mmp);
    if (!leadOptMmp || Object.keys(leadOptMmp).length === 0) return null;
    const queryResult = asRecord(leadOptMmp.query_result);
    const queryId = readText(leadOptMmp.query_id || queryResult.query_id).trim();
    if (!queryId) return null;
    return {
      query_result: {
        ...queryResult,
        query_id: queryId,
        query_mode: readText(queryResult.query_mode || 'one-to-many') || 'one-to-many',
        transforms: Array.isArray(queryResult.transforms) ? queryResult.transforms : [],
        global_transforms: Array.isArray(queryResult.global_transforms) ? queryResult.global_transforms : [],
        clusters: Array.isArray(queryResult.clusters) ? queryResult.clusters : [],
        stats: asRecord(queryResult.stats),
        count: Number(queryResult.count || 0),
        global_count: Number(queryResult.global_count || 0)
      },
      enumerated_candidates: Array.isArray(leadOptMmp.enumerated_candidates) ? leadOptMmp.enumerated_candidates : [],
      prediction_by_smiles: compactLeadOptPredictionMap(asPredictionRecordMap(leadOptMmp.prediction_by_smiles)),
      reference_prediction_by_backend: compactLeadOptPredictionMap(
        asPredictionRecordMap(leadOptMmp.reference_prediction_by_backend)
      ),
      ui_state: asRecord(leadOptMmp.ui_state),
      selection: asRecord(leadOptMmp.selection),
      query_payload: asRecord(leadOptMmp.query_payload),
      target_chain: readText(leadOptMmp.target_chain).trim(),
      ligand_chain: readText(leadOptMmp.ligand_chain).trim()
    } as Record<string, unknown>;
  })();
  const {
    componentStepLabel,
    isRunRedirecting,
    showQuickRunFab,
    affinityConfidenceOnlyUiValue,
    affinityConfidenceOnlyUiLocked,
    runBlockedReason,
    runDisabled,
    canOpenRunMenu,
    sidebarTypeOrder
  } = useProjectRunState({
    workspaceTab,
    isPredictionWorkflow,
    isPeptideDesignWorkflow,
    isAffinityWorkflow,
    isLeadOptimizationWorkflow,
    hasIncompleteComponents,
    componentCompletion,
    submitting: runSubmitting,
    saving,
    runRedirectTaskId,
    showFloatingRunButton,
    affinityTargetFilePresent: Boolean(affinityTargetFile),
    affinityPreviewLoading,
    affinityPreviewCurrent,
    affinityPreviewError: String(affinityPreviewError || ''),
    affinityTargetChainCount: affinityTargetChainIds.length,
    affinityLigandChainId,
    affinityLigandSmiles,
    affinityHasLigand,
    affinitySupportsActivity,
    affinityConfidenceOnly,
    affinityConfidenceOnlyLocked,
    draftBackend: draft.backend
  });
  const formatConstraintCombo = (constraint: PredictionConstraint) =>
    formatConstraintComboForWorkspace(constraint, chainInfoById, componentTypeBuckets);
  const formatConstraintDetail = (constraint: PredictionConstraint) =>
    formatConstraintDetailForWorkspace(constraint);
  const {
    addComponentToDraft,
    addConstraintFromSidebar,
    setAffinityEnabledFromWorkspace,
    setAffinityComponentFromWorkspace,
    jumpToComponent
  } = useProjectSidebarActions({
    draft,
    setDraft,
    setWorkspaceTab,
    setActiveComponentId,
    setSidebarTypeOpen,
    normalizedDraftComponents,
    setSidebarConstraintsOpen,
    setActiveConstraintId,
    setSelectedContactConstraintIds,
    constraintSelectionAnchorRef,
    activeChainInfos,
    ligandChainOptions,
    isBondOnlyBackend,
    canEnableAffinityFromWorkspace,
    workspaceTargetOptions,
    workspaceLigandSelectableOptions,
    createInputComponent
  });
  const {
    clearConstraintSelection,
    selectConstraint,
    jumpToConstraint,
    navigateConstraint,
    applyPickToSelectedConstraint
  } = useConstraintWorkspaceActions({
    draft,
    setDraft,
    activeConstraintId,
    setActiveConstraintId,
    selectedContactConstraintIds,
    setSelectedContactConstraintIds,
    selectedConstraintTemplateComponentId,
    setSelectedConstraintTemplateComponentId,
    resolveTemplateComponentIdForConstraint,
    constraintSelectionAnchorRef,
    setWorkspaceTab,
    setSidebarConstraintsOpen,
    scrollToEditorBlock,
    constraintPickSlotRef,
    activeChainInfos,
    selectedTemplatePreview,
    selectedTemplateResidueIndexMap,
    setPickedResidue,
    canEdit,
    ligandChainOptions,
    isBondOnlyBackend
  });
  const {
    displayStructureText,
    displayStructureFormat,
    displayStructureName,
    displayStructureColorMode,
    constraintStructureText,
    constraintStructureFormat,
    hasConstraintStructure,
    snapshotCards,
    affinityPreviewStructureText,
    affinityPreviewStructureFormat,
    affinityPreviewLigandOverlayText,
    affinityPreviewLigandOverlayFormat,
    affinityResultLigandSmiles,
    predictionLigandPreview,
    predictionLigandRadarSmiles,
    affinityDisplayStructureText,
    affinityDisplayStructureFormat,
    hasAffinityDisplayStructure,
  } = useProjectResultDisplay({
    structureText,
    structureFormat,
    confidenceBackend,
    projectBackend,
    activeResultTaskStructureName: activeResultTask?.structure_name || '',
    projectStructureName: project.structure_name || '',
    draftColorMode: draft.color_mode,
    hasAf3ConfidenceSignals,
    hasProtenixConfidenceSignals,
    selectedTemplatePreviewContent: selectedTemplatePreview?.content || '',
    selectedTemplatePreviewFormat: selectedTemplatePreview?.format || 'pdb',
    selectedResultTargetChainId,
    selectedResultLigandChainId,
    resultChainShortLabelById,
    snapshotPlddt,
    snapshotSelectedLigandChainPlddt,
    snapshotLigandMeanPlddt,
    snapshotPlddtTone,
    snapshotIptm,
    snapshotSelectedPairIptm,
    snapshotIptmTone,
    snapshotIc50Um,
    snapshotIc50Error,
    snapshotIc50Tone,
    snapshotBindingProbability,
    snapshotBindingStd,
    snapshotBindingTone,
    affinityPreviewTargetStructureText,
    affinityPreviewTargetStructureFormat,
    affinityPreviewLigandStructureText,
    affinityPreviewLigandStructureFormat,
    snapshotAffinity: snapshotAffinity || null,
    snapshotConfidence: snapshotConfidence || null,
    statusContextLigandSmiles: String(statusContextTaskRow?.ligand_smiles || ''),
    activeResultLigandSmiles: String(activeResultTask?.ligand_smiles || ''),
    snapshotLigandAtomPlddts: snapshotLigandAtomPlddts || [],
    affinityLigandSmiles,
    overviewPrimaryLigand,
    selectedResultLigandSequence,
    selectedResultLigandComponentType: selectedResultLigandComponent?.type || null,
    snapshotLigandResiduePlddts,
  });
  const {
    handlePredictionComponentsChange,
    handlePredictionProteinTemplateChange,
    handlePredictionTemplateResiduePick,
    handleRuntimeBackendChange,
    handleRuntimeSeedChange,
    handleRuntimePeptideDesignModeChange,
    handleRuntimePeptideBinderLengthChange,
    handleRuntimePeptideUseInitialSequenceChange,
    handleRuntimePeptideInitialSequenceChange,
    handleRuntimePeptideSequenceMaskChange,
    handleRuntimePeptideIterationsChange,
    handleRuntimePeptidePopulationSizeChange,
    handleRuntimePeptideEliteSizeChange,
    handleRuntimePeptideMutationRateChange,
    handleRuntimePeptideBicyclicLinkerCcdChange,
    handleRuntimePeptideBicyclicCysPositionModeChange,
    handleRuntimePeptideBicyclicFixTerminalCysChange,
    handleRuntimePeptideBicyclicIncludeExtraCysChange,
    handleRuntimePeptideBicyclicCys1PosChange,
    handleRuntimePeptideBicyclicCys2PosChange,
    handleRuntimePeptideBicyclicCys3PosChange,
    handleTaskNameChange,
    handleTaskSummaryChange
  } = useProjectEditorHandlers({
    setDraft,
    setPickedResidue,
    setProteinTemplates,
    filterConstraintsByBackend
  });
  const { predictionConstraintsWorkspaceProps, predictionComponentsSidebarProps } = usePredictionWorkspaceProps({
    draft,
    setDraft,
    filterConstraintsByBackend,
    constraintsWorkspaceRef,
    isConstraintsResizing,
    constraintsGridStyle,
    constraintCount,
    activeConstraintIndex,
    constraintTemplateOptions: constraintTemplateOptions || [],
    selectedTemplatePreview,
    setSelectedConstraintTemplateComponentId,
    constraintPickModeEnabled,
    setConstraintPickModeEnabled,
    canEdit,
    setWorkspaceTab,
    navigateConstraint,
    pickedResidue,
    hasConstraintStructure,
    constraintStructureText,
    constraintStructureFormat,
    constraintViewerHighlightResidues,
    constraintViewerActiveResidue,
    applyPickToSelectedConstraint,
    handleConstraintsResizerPointerDown,
    handleConstraintsResizerKeyDown,
    clearConstraintSelection,
    activeConstraintId,
    selectedContactConstraintIds,
    selectConstraint,
    allowedConstraintTypes,
    isBondOnlyBackend,
    hasIncompleteComponents,
    componentCompletion,
    sidebarTypeOrder,
    componentTypeBuckets,
    sidebarTypeOpen,
    setSidebarTypeOpen,
    addComponentToDraft,
    activeComponentId,
    jumpToComponent,
    sidebarConstraintsOpen,
    setSidebarConstraintsOpen,
    addConstraintFromSidebar,
    hasActiveChains: activeChainInfos.length > 0,
    selectedContactConstraintIdSet,
    jumpToConstraint,
    constraintLabel,
    formatConstraintCombo,
    formatConstraintDetail,
    canEnableAffinityFromWorkspace,
    setAffinityEnabledFromWorkspace,
    selectedWorkspaceTarget,
    selectedWorkspaceLigand,
    workspaceTargetOptions,
    workspaceLigandSelectableOptions,
    setAffinityComponentFromWorkspace,
    affinityEnableDisabledReason,
    showAffinityComputeToggle: !isPeptideDesignWorkflow
  });
  const {
    projectResultsSectionProps,
    affinityWorkflowSectionProps,
    leadOptimizationWorkflowSectionProps,
    predictionWorkflowSectionProps,
    workflowRuntimeSettingsSectionProps
  } = useProjectWorkflowSectionProps({
    isPredictionWorkflow,
    isPeptideDesignWorkflow,
    isAffinityWorkflow,
    workflowTitle: workflow.title,
    workflowShortTitle: workflow.shortTitle,
    projectTaskState: displayTaskState || project.task_state || '',
    projectTaskId: project.task_id || '',
    statusInfo: statusInfo || null,
    progressPercent,
    onPeptideRequestStructure: async () => {
      const contextTask = statusContextTaskRow || activeResultTask;
      const taskId = String(contextTask?.task_id || project.task_id || '').trim();
      if (!taskId) return;
      await pullResultForViewer(taskId, {
        taskRowId: contextTask?.id || undefined,
        persistProject: String(project.task_id || '').trim() === taskId,
        resultMode: 'view'
      });
    },
    resultsGridRef,
    isResultsResizing,
    resultsGridStyle,
    onResultsResizerPointerDown: handleResultsResizerPointerDown,
    onResultsResizerKeyDown: handleResultsResizerKeyDown,
    snapshotCards,
    snapshotConfidence: snapshotConfidence || null,
    resultChainIds,
    selectedResultTargetChainId,
    selectedResultLigandChainId,
    displayStructureText,
    displayStructureFormat,
    displayStructureColorMode,
    displayStructureName,
    confidenceBackend,
    projectBackend,
    predictionLigandPreview,
    predictionLigandRadarSmiles,
    hasAffinityDisplayStructure,
    affinityDisplayStructureText,
    affinityDisplayStructureFormat,
    affinityResultLigandSmiles,
    affinityTargetChainIds,
    affinityLigandChainId,
    snapshotLigandAtomPlddts,
    snapshotPlddt,
    snapshotIptm,
    snapshotSelectedPairIptm,
    selectedResultLigandSequence,
    canEdit,
    submitting,
    affinityTargetFileName: affinityTargetFile?.name || '',
    affinityLigandFileName: affinityLigandFile?.name || '',
    affinityLigandSmiles,
    affinityPreviewLigandSmiles: String(affinityPreview?.ligandSmiles || ''),
    affinityUseMsa,
    affinityConfidenceOnlyUiValue,
    affinityConfidenceOnlyUiLocked,
    affinityHasLigand,
    affinityPreviewStructureText,
    affinityPreviewStructureFormat,
    affinityPreviewLigandOverlayText,
    affinityPreviewLigandOverlayFormat,
    onAffinityTargetFileChange,
    onAffinityLigandFileChange,
    onAffinityUseMsaChange,
    onAffinityConfidenceOnlyChange,
    setAffinityLigandSmiles,
    leadOptProteinSequence: leadOptPrimary.proteinSequence,
    leadOptLigandSmiles: leadOptPrimary.ligandSmiles,
    leadOptTargetChain: leadOptChainContext.targetChain,
    leadOptLigandChain: leadOptChainContext.ligandChain,
    leadOptReferenceScopeKey: `${project.id}:${statusContextTaskRow?.id || 'new'}`,
    leadOptPersistedReferenceUploads: leadOptPersistedUploads,
    onLeadOptReferenceUploadsChange: handleLeadOptReferenceUploadsChange,
    onLeadOptMmpTaskQueued: handleLeadOptMmpTaskQueued,
    onLeadOptMmpTaskCompleted: handleLeadOptMmpTaskCompleted,
    onLeadOptMmpTaskFailed: handleLeadOptMmpTaskFailed,
    onLeadOptUiStateChange: handleLeadOptUiStateChange,
    onLeadOptPredictionQueued: handleLeadOptPredictionQueued,
    onLeadOptPredictionStateChange: handleLeadOptPredictionStateChange,
    onLeadOptNavigateToResults: () => {},
    leadOptInitialMmpSnapshot,
    setDraft,
    setWorkspaceTab,
    onRegisterLeadOptHeaderRunAction: handleRegisterLeadOptHeaderRunAction,
    workspaceTab,
    componentsWorkspaceRef,
    isComponentsResizing,
    componentsGridStyle,
    onComponentsResizerPointerDown: handleComponentsResizerPointerDown,
    onComponentsResizerKeyDown: handleComponentsResizerKeyDown,
    components: draft.inputConfig.components,
    onComponentsChange: handlePredictionComponentsChange,
    proteinTemplates,
    onProteinTemplateChange: handlePredictionProteinTemplateChange,
    activeComponentId,
    setActiveComponentId,
    onProteinTemplateResiduePick: handlePredictionTemplateResiduePick,
    predictionConstraintsWorkspaceProps,
    predictionComponentsSidebarProps,
    backend: draft.backend,
    seed: draft.inputConfig.options.seed ?? null,
    peptideDesignMode: draft.inputConfig.options.peptideDesignMode ?? 'cyclic',
    peptideBinderLength: draft.inputConfig.options.peptideBinderLength ?? 20,
    peptideUseInitialSequence: draft.inputConfig.options.peptideUseInitialSequence ?? false,
    peptideInitialSequence: draft.inputConfig.options.peptideInitialSequence ?? '',
    peptideSequenceMask:
      draft.inputConfig.options.peptideSequenceMask ??
      'X'.repeat(Math.max(1, draft.inputConfig.options.peptideBinderLength ?? 20)),
    peptideIterations: draft.inputConfig.options.peptideIterations ?? 12,
    peptidePopulationSize: draft.inputConfig.options.peptidePopulationSize ?? 16,
    peptideEliteSize: draft.inputConfig.options.peptideEliteSize ?? 5,
    peptideMutationRate: draft.inputConfig.options.peptideMutationRate ?? 0.25,
    peptideBicyclicLinkerCcd: draft.inputConfig.options.peptideBicyclicLinkerCcd ?? 'SEZ',
    peptideBicyclicCysPositionMode: draft.inputConfig.options.peptideBicyclicCysPositionMode ?? 'auto',
    peptideBicyclicFixTerminalCys: draft.inputConfig.options.peptideBicyclicFixTerminalCys ?? true,
    peptideBicyclicIncludeExtraCys: draft.inputConfig.options.peptideBicyclicIncludeExtraCys ?? false,
    peptideBicyclicCys1Pos: draft.inputConfig.options.peptideBicyclicCys1Pos ?? 3,
    peptideBicyclicCys2Pos: draft.inputConfig.options.peptideBicyclicCys2Pos ?? 8,
    peptideBicyclicCys3Pos:
      draft.inputConfig.options.peptideBicyclicCys3Pos ??
      (draft.inputConfig.options.peptideBinderLength ?? 20),
    onBackendChange: handleRuntimeBackendChange,
    onSeedChange: handleRuntimeSeedChange,
    onPeptideDesignModeChange: handleRuntimePeptideDesignModeChange,
    onPeptideBinderLengthChange: handleRuntimePeptideBinderLengthChange,
    onPeptideUseInitialSequenceChange: handleRuntimePeptideUseInitialSequenceChange,
    onPeptideInitialSequenceChange: handleRuntimePeptideInitialSequenceChange,
    onPeptideSequenceMaskChange: handleRuntimePeptideSequenceMaskChange,
    onPeptideIterationsChange: handleRuntimePeptideIterationsChange,
    onPeptidePopulationSizeChange: handleRuntimePeptidePopulationSizeChange,
    onPeptideEliteSizeChange: handleRuntimePeptideEliteSizeChange,
    onPeptideMutationRateChange: handleRuntimePeptideMutationRateChange,
    onPeptideBicyclicLinkerCcdChange: handleRuntimePeptideBicyclicLinkerCcdChange,
    onPeptideBicyclicCysPositionModeChange: handleRuntimePeptideBicyclicCysPositionModeChange,
    onPeptideBicyclicFixTerminalCysChange: handleRuntimePeptideBicyclicFixTerminalCysChange,
    onPeptideBicyclicIncludeExtraCysChange: handleRuntimePeptideBicyclicIncludeExtraCysChange,
    onPeptideBicyclicCys1PosChange: handleRuntimePeptideBicyclicCys1PosChange,
    onPeptideBicyclicCys2PosChange: handleRuntimePeptideBicyclicCys2PosChange,
    onPeptideBicyclicCys3PosChange: handleRuntimePeptideBicyclicCys3PosChange
  });
  const taskHistoryPath = `/projects/${project.id}/tasks`;
  const {
    handleRunAction,
    handleRunCurrentDraft,
    handleRestoreSavedDraft,
    handleResetFromHeader,
    handleWorkspaceFormSubmit,
    handleOpenTaskHistory,
  } = useProjectRunHandlers({
    runDisabled,
    submitTask,
    setRunMenuOpen,
    loadProject,
    saving,
    submitting,
    loading,
    hasUnsavedChanges,
    saveDraft,
    taskHistoryPath,
    setRunRedirectTaskId,
    navigate,
  });

  const leadOptHeaderActionMissing = isLeadOptimizationWorkflow && !leadOptHeaderRunAction;
  const effectiveRunDisabled = runDisabled || leadOptHeaderActionMissing;
  const effectiveRunBlockedReason = leadOptHeaderActionMissing
    ? workspaceTab === 'components'
      ? 'Select at least one fragment to run.'
      : 'Run action is only available in Lead Optimization Components view.'
    : runBlockedReason;
  const handleHeaderRunAction = () => {
    if (isLeadOptimizationWorkflow) {
      if (!leadOptHeaderRunAction || leadOptHeaderRunPending) return;
      setLeadOptHeaderRunPending(true);
      void Promise.resolve(leadOptHeaderRunAction())
        .catch(() => {
          // Lead opt workspace already surfaces query errors.
        })
        .finally(() => {
          setLeadOptHeaderRunPending(false);
        });
      return;
    }
    handleRunAction();
  };

  return (
    <ProjectDetailLayout
      projectName={project.name}
      canDownloadResult={Boolean(
        readText(structureTaskId).trim() ||
          (readText(activeResultTask?.structure_name).trim() && readText(activeResultTask?.task_id).trim()) ||
          (!isLeadOptimizationWorkflow && readText(project.task_id).trim())
      )}
      workflow={{
        shortTitle: workflow.shortTitle,
        runLabel: workflow.runLabel,
        description: workflow.description
      }}
      workspaceTab={workspaceTab}
      componentStepLabel={componentStepLabel}
      taskName={draft.taskName}
      taskSummary={draft.taskSummary}
      isPredictionWorkflow={isPredictionWorkflow}
      isAffinityWorkflow={isAffinityWorkflow}
      isLeadOptimizationWorkflow={isLeadOptimizationWorkflow}
      displayTaskState={displayTaskState}
      isActiveRuntime={isActiveRuntime}
      progressPercent={progressPercent}
      waitingSeconds={waitingSeconds}
      totalRuntimeSeconds={totalRuntimeSeconds}
      canEdit={canEdit}
      loading={loading}
      saving={saving}
      submitting={submitting}
      runSubmitting={runSubmitting}
      hasUnsavedChanges={hasUnsavedChanges}
      runMenuOpen={runMenuOpen}
      runDisabled={effectiveRunDisabled}
      runBlockedReason={effectiveRunBlockedReason}
      isRunRedirecting={isRunRedirecting}
      canOpenRunMenu={canOpenRunMenu}
      showHeaderRunAction
      showQuickRunFab={showQuickRunFab}
      taskHistoryPath={taskHistoryPath}
      runSuccessNotice={runSuccessNotice}
      error={error}
      resultError={resultError}
      affinityPreviewError={affinityPreviewError}
      resultChainConsistencyWarning={resultChainConsistencyWarning}
      projectResultsSectionProps={projectResultsSectionProps}
      affinitySectionProps={affinityWorkflowSectionProps}
      leadOptimizationSectionProps={leadOptimizationWorkflowSectionProps}
      predictionSectionProps={predictionWorkflowSectionProps}
      runtimeSettingsProps={workflowRuntimeSettingsSectionProps}
      runActionRef={runActionRef as RefObject<HTMLDivElement>}
      topRunButtonRef={topRunButtonRef as RefObject<HTMLButtonElement>}
      onOpenTaskHistory={handleOpenTaskHistory}
      onDownloadResult={() => {
        const viewerTaskId = readText(structureTaskId).trim();
        const activeTaskId = readText(activeResultTask?.task_id).trim();
        const activeStructureName = readText(activeResultTask?.structure_name).trim();
        const projectTaskId = readText(project.task_id).trim();
        const downloadTaskId =
          viewerTaskId ||
          (activeStructureName ? activeTaskId : '') ||
          (isLeadOptimizationWorkflow ? '' : projectTaskId);
        if (!downloadTaskId) return;
        setError(null);
        void downloadResultFile(downloadTaskId).catch((err) => {
          setError(err instanceof Error ? err.message : 'Failed to download result archive.');
        });
      }}
      onSaveDraft={() => {
        void saveDraft();
      }}
      onReset={handleResetFromHeader}
      onRunAction={handleHeaderRunAction}
      onRestoreSavedDraft={handleRestoreSavedDraft}
      onRunCurrentDraft={handleRunCurrentDraft}
      onWorkspaceTabChange={setWorkspaceTab}
      onTaskNameChange={handleTaskNameChange}
      onTaskSummaryChange={handleTaskSummaryChange}
      onWorkspaceFormSubmit={handleWorkspaceFormSubmit}
    />
  );
}
