import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from 'react';
import { Link } from 'react-router-dom';
import type {
  InputComponent,
  PredictionConstraint,
  ProjectInputConfig,
  ProjectTask,
  ProteinTemplateUpload,
  VirtualScreeningPredictionRecord
} from '../../types/models';
import { downloadResultFile, terminateTask as terminateBackendTask } from '../../api/backendApi';
import { deleteProjectTask } from '../../api/supabaseLite';
import { createInputComponent, saveProjectInputConfig } from '../../utils/projectInputs';
import { normalizeTaskSummary } from '../../utils/taskMetadata';
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
import { useAuth } from '../../hooks/useAuth';
import {
  buildLeadOptUploadSnapshotComponents,
  mergeTaskInputOptionsIntoProperties,
  type LeadOptPersistedUploads
} from './projectTaskSnapshot';
import {
  buildLeadOptCandidatesUiStateSignature,
  type LeadOptCandidatesUiState
} from '../../components/project/leadopt/LeadOptCandidatesPanel';
import {
  buildLeadOptPredictionRecordKey,
  parseLeadOptPredictionRecordKey,
  type LeadOptPredictionRecord
} from '../../components/project/leadopt/hooks/useLeadOptMmpQueryMachine';
import {
  ProjectCopilotModal,
  type CopilotAttachmentApplication,
  type CopilotUploadedAttachment,
  clearStoredCopilotTaskPrefill,
  readStoredCopilotOpen,
  readStoredCopilotTaskPrefill,
  writeStoredCopilotOpen
} from '../../components/copilot/ProjectCopilotModal';
import type { CopilotPlanAction } from '../../types/models';
import { detectStructureFormat, extractProteinChainSequences } from '../../utils/structureParser';
import { useCopilotAvailability } from '../../hooks/useCopilotAvailability';

import {
  readText,
  asRecord,
  hasExplicitPeptideResiduePool,
  summarizeCopilotComponents,
  summarizeCopilotConstraints,
  summarizeCopilotTask,
  normalizeCopilotPrefillComponents,
  applyCopilotComponentPatchOperations,
  readFiniteNumber,
  asPredictionRecordMap,
  hasPersistedIpsaeMetric,
  mergePredictionRecordMaps,
  summarizeLeadOptPredictions,
  hydratePredictionRecordMapFromHistory,
  readBooleanToken,
  normalizePredictionBackendStrict,
  readSessionIdentityFromLocalStorage,
  buildLeadOptUiStateScopeKey,
  readLeadOptUiStateFromLocal,
  writeLeadOptUiStateToLocal,
  compactLeadOptPredictionMap,
  compactLeadOptEnumeratedCandidates,
  compactLeadOptQueryResult,
  mergeLeadOptStateMetaIntoProperties,
  mergeLeadOptMetaIntoProperties,
  compactLeadOptForConfidenceWrite,
  buildLeadOptPredictionPersistSignature,
  mergeLeadOptSnapshotForPersist,
  mergeLeadOptPatchPayloadForPersist,
  compactLeadOptCandidatesUiState,
  resolveLeadOptSnapshotFromTask,
  resolveLeadOptDownloadTaskId,
  collectLeadOptDownloadRecords,
  downloadLeadOptCombinedArchive,
  pickPreferredLeadOptTask,
  buildLeadOptAggregatedSnapshot,
  buildLeadOptSelectionFromPayload
} from './workspaceViewHelpers';

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
    const requestedTaskRowId =
      String(query.get('task_row_id') || '').trim() || String(query.get('source_task_row_id') || '').trim();
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
  const { session } = useAuth();
  const copilotAvailable = useCopilotAvailability();
  const [leadOptHeaderRunAction, setLeadOptHeaderRunAction] = useState<(() => void | Promise<void>) | null>(null);
  const [leadOptHeaderRunPending, setLeadOptHeaderRunPending] = useState(false);
  const [headerStopRunPending, setHeaderStopRunPending] = useState(false);
  const [copilotOpen, setCopilotOpen] = useState(() => readStoredCopilotOpen({ contextType: 'task_detail', userId: session?.userId || null }));
  useEffect(() => {
    writeStoredCopilotOpen({ contextType: 'task_detail', userId: session?.userId || null }, copilotOpen);
  }, [copilotOpen, session?.userId]);

  const explicitRequestedTaskRowId = useMemo(
    () => {
      const query = new URLSearchParams(runtime.locationSearch);
      return String(query.get('task_row_id') || '').trim() || String(query.get('source_task_row_id') || '').trim();
    },
    [runtime.locationSearch]
  );
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
    isVirtualScreeningWorkflow,
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
    affinityMode,
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
    updateConstraintPickSlot,
    constraintPickSlot,
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
    constraintViewerHighlightResidues,
    constraintViewerActiveResidue,
    constraintSelectedAtomRefs,
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
    onAffinityModeChange,
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
    customResidueLibrary,
    setCustomResidueLibrary,
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

  const copilotSequenceAppliedRef = useRef(false);
  const [copilotPrefillSave, setCopilotPrefillSave] = useState<{ components: InputComponent[] } | null>(null);
  useEffect(() => {
    if (copilotSequenceAppliedRef.current) return;
    const query = new URLSearchParams(runtime.locationSearch);
    const copilotComponentsRaw = String(query.get('copilot_components') || '').trim();
    const copilotSequence = String(query.get('copilot_sequence') || '').trim();
    const copilotParameterPatchRaw = String(query.get('copilot_parameter_patch') || '').trim();
    const storedCopilotPrefill =
      session?.userId && project?.id
        ? readStoredCopilotTaskPrefill(session.userId, project.id)
        : null;
    if ((!copilotComponentsRaw && !copilotSequence && !copilotParameterPatchRaw && !storedCopilotPrefill) || !draft || !project) return;
    copilotSequenceAppliedRef.current = true;
    const aminoAcidPattern = /^[ACDEFGHIKLMNPQRSTVWY]+$/i;
    query.delete('copilot_components');
    query.delete('copilot_sequence');
    query.delete('copilot_parameter_patch');
    const nextSearch = query.toString();
    navigate(
      { pathname: window.location.pathname, search: nextSearch ? `?${nextSearch}` : '' },
      { replace: true }
    );
    let nextComponents: InputComponent[] = [];
    if (copilotComponentsRaw) {
      try {
        nextComponents = normalizeCopilotPrefillComponents(JSON.parse(copilotComponentsRaw));
      } catch {
        nextComponents = [];
      }
    }
    if (storedCopilotPrefill) {
      const storedComponents = normalizeCopilotPrefillComponents(storedCopilotPrefill.components);
      if (storedComponents.length > nextComponents.length) {
        nextComponents = storedComponents;
      }
    }
    if (nextComponents.length === 0 && aminoAcidPattern.test(copilotSequence)) {
      nextComponents = [{
        id: `copilot-protein-1`,
        type: 'protein',
        sequence: copilotSequence.toUpperCase(),
        numCopies: 1,
        useMsa: true,
      }];
    }
    const parameterPatch = (() => {
      if (!copilotParameterPatchRaw) return {};
      try {
        return asRecord(JSON.parse(copilotParameterPatchRaw));
      } catch {
        return {};
      }
    })();
    const replacement = asRecord(parameterPatch.componentsReplacement);
    const replacementComponents = normalizeCopilotPrefillComponents(replacement.components);
    const addedComponents = normalizeCopilotPrefillComponents(parameterPatch.componentsAdd);
    const patchedByOperations = applyCopilotComponentPatchOperations(draft.inputConfig.components, parameterPatch.componentsPatch);
    const componentOperationsChanged = patchedByOperations !== draft.inputConfig.components;
    const backendPatch = readText(parameterPatch.backend).trim().toLowerCase();
    const backendPatchAllowed = isVirtualScreeningWorkflow
      ? backendPatch === 'nesso'
      : backendPatch === 'boltz' || backendPatch === 'alphafold3' || backendPatch === 'protenix';
    const seedPatch = readFiniteNumber(parameterPatch.seed);
    const hasPatch =
      replacementComponents.length > 0 ||
      addedComponents.length > 0 ||
      componentOperationsChanged ||
      backendPatchAllowed ||
      seedPatch !== null;
    if (nextComponents.length === 0 && !hasPatch) return;
    setDraft((prev) =>
      {
        if (!prev) return prev;
        const patchedComponents =
          replacementComponents.length > 0
            ? replacementComponents
            : nextComponents.length > 0
              ? nextComponents
              : componentOperationsChanged
                ? applyCopilotComponentPatchOperations(prev.inputConfig.components, parameterPatch.componentsPatch)
              : addedComponents.length > 0
                ? [...prev.inputConfig.components, ...addedComponents]
                : prev.inputConfig.components;
        return {
          ...prev,
          backend: backendPatchAllowed ? backendPatch : prev.backend,
          inputConfig: {
            ...prev.inputConfig,
            version: 1,
            components: patchedComponents,
            constraints:
              nextComponents.length > 0 || (replacementComponents.length > 0 && replacement.clearConstraints !== false)
                ? []
                : prev.inputConfig.constraints,
            options: {
              ...prev.inputConfig.options,
              ...(seedPatch !== null ? { seed: Math.max(0, Math.floor(seedPatch)) } : {})
            }
          },
        };
      }
    );
    if (storedCopilotPrefill && session?.userId && project.id) {
      clearStoredCopilotTaskPrefill(session.userId, project.id);
    }
    setCopilotPrefillSave({
      components:
        replacementComponents.length > 0
          ? replacementComponents
          : nextComponents.length > 0
            ? nextComponents
            : componentOperationsChanged
              ? patchedByOperations
            : addedComponents.length > 0
              ? [...draft.inputConfig.components, ...addedComponents]
              : draft.inputConfig.components
    });
  }, [draft, isVirtualScreeningWorkflow, navigate, project, runtime.locationSearch, session?.userId, setDraft]);

  useEffect(() => {
    if (!copilotPrefillSave || !draft) return;
    const hasAllComponents = copilotPrefillSave.components.every((expected) =>
      draft.inputConfig.components.some(
        (component) =>
          component.type === expected.type &&
          readText(component.sequence).trim() === readText(expected.sequence).trim()
      )
    );
    if (!hasAllComponents) return;
    setCopilotPrefillSave(null);
    void saveDraft();
  }, [copilotPrefillSave, draft, saveDraft]);

  const sessionIdentity =
    readText(session?.userId).trim() ||
    readText(session?.username).trim().toLowerCase() ||
    readSessionIdentityFromLocalStorage();
  const leadOptMmpTaskRowMapRef = useRef<Record<string, string>>({});
  const leadOptPredictionTaskRowMapRef = useRef<Record<string, string>>({});
  const leadOptUploadPersistKeyRef = useRef('');
  const leadOptActiveTaskRowIdRef = useRef('');
  const leadOptPredictionPersistKeyRef = useRef('');
  const leadOptPredictionPersistQueueRef = useRef<Promise<void>>(Promise.resolve());
  const leadOptPredictionPersistTimerRef = useRef<number | null>(null);
  const resultViewHydrationAttemptedRef = useRef<Set<string>>(new Set());
  const leadOptPredictionPersistPendingByTaskRowRef = useRef<Record<string, {
    taskRowId: string;
    patchPayload: Record<string, unknown>;
  }>>({});
  const leadOptPredictionPersistShadowByTaskRowRef = useRef<Record<string, Record<string, unknown>>>({});
  const leadOptPersistSnapshotByTaskRowRef = useRef<Record<string, Record<string, unknown>>>({});
  const leadOptUiStatePersistKeyRef = useRef('');
  const leadOptMmpContextByTaskIdRef = useRef<Record<string, Record<string, unknown>>>({});
  const virtualScreeningPredictionPersistSignatureRef = useRef('');
  const virtualScreeningPredictionPersistQueueRef = useRef<Promise<void>>(Promise.resolve());

  const flushLeadOptPredictionPersistQueue = useCallback(() => {
    const pendingEntries = Object.values(leadOptPredictionPersistPendingByTaskRowRef.current);
    leadOptPredictionPersistPendingByTaskRowRef.current = {};
    if (pendingEntries.length === 0) return;
    const nextPersist = leadOptPredictionPersistQueueRef.current
      .catch(() => undefined)
      .then(async () => {
        for (const entry of pendingEntries) {
          await patchTask(entry.taskRowId, entry.patchPayload as any);
        }
      });
    leadOptPredictionPersistQueueRef.current = nextPersist;
  }, [patchTask]);

  const flushLeadOptPredictionPersistQueueNow = useCallback(() => {
    if (leadOptPredictionPersistTimerRef.current !== null) {
      window.clearTimeout(leadOptPredictionPersistTimerRef.current);
      leadOptPredictionPersistTimerRef.current = null;
    }
    flushLeadOptPredictionPersistQueue();
  }, [flushLeadOptPredictionPersistQueue]);

  const queueLeadOptPredictionPersistPatch = useCallback(
    (
      taskRowId: string,
      patchPayload: Record<string, unknown>,
      options?: { immediate?: boolean; debounceMs?: number }
    ) => {
      const normalizedTaskRowId = readText(taskRowId).trim();
      if (!normalizedTaskRowId) return;
      const pendingForRow = leadOptPredictionPersistPendingByTaskRowRef.current[normalizedTaskRowId];
      const shadowForRow = leadOptPredictionPersistShadowByTaskRowRef.current[normalizedTaskRowId];
      const mergedPatchPayload = mergeLeadOptPatchPayloadForPersist(
        patchPayload,
        pendingForRow?.patchPayload || shadowForRow || {}
      );
      leadOptPredictionPersistPendingByTaskRowRef.current[normalizedTaskRowId] = {
        taskRowId: normalizedTaskRowId,
        patchPayload: mergedPatchPayload
      };
      leadOptPredictionPersistShadowByTaskRowRef.current[normalizedTaskRowId] = mergedPatchPayload;
      if (options?.immediate) {
        flushLeadOptPredictionPersistQueueNow();
        return;
      }
      if (leadOptPredictionPersistTimerRef.current !== null) return;
      const debounceMsRaw = Number(options?.debounceMs);
      const debounceMs = Number.isFinite(debounceMsRaw)
        ? Math.max(0, Math.floor(debounceMsRaw))
        : 900;
      leadOptPredictionPersistTimerRef.current = window.setTimeout(() => {
        leadOptPredictionPersistTimerRef.current = null;
        flushLeadOptPredictionPersistQueue();
      }, debounceMs);
    },
    [flushLeadOptPredictionPersistQueue, flushLeadOptPredictionPersistQueueNow]
  );

  useEffect(() => {
    return () => {
      flushLeadOptPredictionPersistQueueNow();
    };
  }, [flushLeadOptPredictionPersistQueueNow]);

  useEffect(() => {
    if (!isLeadOptimizationWorkflow) return;
    if (workspaceTab === 'results' || workspaceTab === 'components') return;
    flushLeadOptPredictionPersistQueueNow();
  }, [flushLeadOptPredictionPersistQueueNow, isLeadOptimizationWorkflow, workspaceTab]);

  useEffect(() => {
    const handleVisibilityChange = () => {
      if (document.visibilityState !== 'hidden') return;
      flushLeadOptPredictionPersistQueueNow();
    };
    const handlePageHide = () => {
      flushLeadOptPredictionPersistQueueNow();
    };
    document.addEventListener('visibilitychange', handleVisibilityChange);
    window.addEventListener('pagehide', handlePageHide);
    return () => {
      document.removeEventListener('visibilitychange', handleVisibilityChange);
      window.removeEventListener('pagehide', handlePageHide);
    };
  }, [flushLeadOptPredictionPersistQueueNow]);

  useEffect(() => {
    if (workspaceTab !== 'results') return;
    if (isVirtualScreeningWorkflow) return;
    if (!isPredictionWorkflow && !isAffinityWorkflow) return;
    const contextTask = activeResultTask || statusContextTaskRow;
    const taskId = readText(contextTask?.task_id || project.task_id).trim();
    const taskRowId = readText(contextTask?.id).trim();
    const taskState = readText(contextTask?.task_state || project.task_state).trim().toUpperCase();
    if (!taskId || taskState !== 'SUCCESS') return;
    const snapshotConfidenceSource = contextTask?.confidence ?? project.confidence;
    if (hasPersistedIpsaeMetric(snapshotConfidenceSource)) return;
    const hydrationKey = `${taskRowId || '__project__'}:${taskId}`;
    if (resultViewHydrationAttemptedRef.current.has(hydrationKey)) return;
    resultViewHydrationAttemptedRef.current.add(hydrationKey);
    void pullResultForViewer(taskId, {
      taskRowId: taskRowId || undefined,
      persistProject: readText(project.task_id).trim() === taskId,
      resultMode: 'view'
    });
  }, [
    activeResultTask,
    isAffinityWorkflow,
    isPredictionWorkflow,
    isVirtualScreeningWorkflow,
    project.confidence,
    project.task_id,
    project.task_state,
    pullResultForViewer,
    statusContextTaskRow,
    workspaceTab
  ]);

  const preferredLeadOptSnapshotTask = useMemo(
    () => pickPreferredLeadOptTask(projectTasks),
    [projectTasks]
  );

  const resolveLeadOptTaskRowId = useCallback((): string => {
    const explicitRequestedRowId = readText(explicitRequestedTaskRowId).trim();
    if (explicitRequestedRowId) {
      const explicitRequestedRow = projectTasks.find((row) => readText(row?.id).trim() === explicitRequestedRowId) || null;
      if (explicitRequestedRow) return explicitRequestedRowId;
    }

    const requestedRowId = readText(requestedStatusTaskRow?.id).trim();
    if (requestedRowId) return requestedRowId;

    const preferredLeadOptTaskRowId = readText(preferredLeadOptSnapshotTask?.id).trim();
    if (preferredLeadOptTaskRowId) return preferredLeadOptTaskRowId;

    const rememberedRowId = readText(leadOptActiveTaskRowIdRef.current).trim();
    if (rememberedRowId) return rememberedRowId;

    const contextRowId = readText(statusContextTaskRow?.id).trim();
    if (contextRowId) return contextRowId;

    const activeResultRowId = readText(activeResultTask?.id).trim();
    if (activeResultRowId) return activeResultRowId;

    const latestRuntimeTaskRow = projectTasks.find((row) => readText(row?.task_id).trim().length > 0);
    const latestRuntimeTaskRowId = readText(latestRuntimeTaskRow?.id).trim();
    if (latestRuntimeTaskRowId) return latestRuntimeTaskRowId;

    const firstTaskRowId = readText(projectTasks[0]?.id).trim();
    if (firstTaskRowId) return firstTaskRowId;

    return '';
  }, [activeResultTask, explicitRequestedTaskRowId, preferredLeadOptSnapshotTask, projectTasks, requestedStatusTaskRow, statusContextTaskRow]);

  const resolveLeadOptSourceTask = useCallback(
    (taskRowId: string) => {
      const id = readText(taskRowId).trim();
      if (!id) return null;
      if (requestedStatusTaskRow && String(requestedStatusTaskRow.id) === id) return requestedStatusTaskRow;
      if (statusContextTaskRow && String(statusContextTaskRow.id) === id) return statusContextTaskRow;
      if (activeResultTask && String(activeResultTask.id) === id) return activeResultTask;
      const projectTask = projectTasks.find((row) => readText(row?.id).trim() === id);
      if (projectTask) return projectTask;
      return null;
    },
    [activeResultTask, projectTasks, requestedStatusTaskRow, statusContextTaskRow]
  );

  const resolveLeadOptTaskRowIdByPredictionTaskId = useCallback(
    (predictionTaskIdInput: string): string => {
      const predictionTaskId = readText(predictionTaskIdInput).trim();
      if (!predictionTaskId) return '';
      for (const row of projectTasks) {
        const snapshot = resolveLeadOptSnapshotFromTask(row);
        const predictionMap = asPredictionRecordMap(snapshot.prediction_by_smiles);
        for (const record of Object.values(predictionMap)) {
          if (readText(record?.taskId).trim() === predictionTaskId) {
            return readText(row?.id).trim();
          }
        }
      }
      return '';
    },
    [projectTasks]
  );

  const leadOptHistoricalReferenceRecords = useMemo(() => {
    let merged: Record<string, LeadOptPredictionRecord> = {};
    for (const row of projectTasks) {
      const snapshot = resolveLeadOptSnapshotFromTask(row);
      const records = asPredictionRecordMap(snapshot.reference_prediction_by_backend);
      if (Object.keys(records).length === 0) continue;
      merged = mergePredictionRecordMaps(merged, records);
    }
    return compactLeadOptPredictionMap(merged);
  }, [projectTasks]);

  const leadOptDownloadTaskId = useMemo(
    () => resolveLeadOptDownloadTaskId(activeResultTask, structureTaskId),
    [activeResultTask, structureTaskId]
  );
  const defaultDownloadTaskId = useMemo(() => {
    const viewerTaskId = readText(structureTaskId).trim();
    if (viewerTaskId) return viewerTaskId;
    const activeTaskId = readText(activeResultTask?.task_id).trim();
    const activeStructureName = readText(activeResultTask?.structure_name).trim();
    if (activeStructureName && activeTaskId) return activeTaskId;
    return readText(project.task_id).trim();
  }, [activeResultTask?.structure_name, activeResultTask?.task_id, project.task_id, structureTaskId]);

  const aggregatedLeadOptSnapshot = useMemo(
    () =>
      buildLeadOptAggregatedSnapshot({
        projectTasks,
        requestedTaskRow: requestedStatusTaskRow,
        preferRequestedQuery: Boolean(explicitRequestedTaskRowId || requestedStatusTaskRow?.id),
        strictRequestedTaskRow: Boolean(explicitRequestedTaskRowId || requestedStatusTaskRow?.id),
        preferredListTask: preferredLeadOptSnapshotTask,
        historicalReferenceRecords: leadOptHistoricalReferenceRecords
      }),
    [
      explicitRequestedTaskRowId,
      leadOptHistoricalReferenceRecords,
      preferredLeadOptSnapshotTask,
      projectTasks,
      requestedStatusTaskRow
    ]
  );
  const aggregatedLeadOptSnapshotRecord = asRecord(aggregatedLeadOptSnapshot);
  const leadOptDownloadRecords = useMemo(
    () =>
      collectLeadOptDownloadRecords(
        aggregatedLeadOptSnapshotRecord.prediction_by_smiles,
        aggregatedLeadOptSnapshotRecord.selected_backend
      ),
    [aggregatedLeadOptSnapshotRecord]
  );
  const leadOptActiveTaskRowId = resolveLeadOptTaskRowId();
  const leadOptActiveQueryId = readText(
    aggregatedLeadOptSnapshotRecord.query_id || asRecord(aggregatedLeadOptSnapshotRecord.query_result).query_id
  ).trim();
  const leadOptUiStateScopeKey = buildLeadOptUiStateScopeKey({
    sessionIdentity,
    projectId: project.id,
    taskRowId: leadOptActiveTaskRowId,
    queryId: leadOptActiveQueryId
  });
  const leadOptUserScopedUiState = useMemo(
    () => readLeadOptUiStateFromLocal(leadOptUiStateScopeKey),
    [leadOptUiStateScopeKey]
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
    const inheritedReferenceRecords = leadOptHistoricalReferenceRecords;
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
    const leadOptPayload = {
      stage: 'queued',
      task_id: taskId,
      prediction_stage: 'idle',
      prediction_summary: {
        total: 0,
        queued: 0,
        running: 0,
        success: 0,
        failure: 0
      },
      prediction_by_smiles: {},
      reference_prediction_by_backend: inheritedReferenceRecords,
      ...mmpContext
    } as Record<string, unknown>;
    await patchTask(draftTaskRow.id, {
      task_id: taskId,
      task_state: 'QUEUED',
      status_text: 'MMP query queued',
      error_text: '',
      submitted_at: queuedAt,
      completed_at: null,
      duration_seconds: null,
      components: snapshotComponents,
      properties: mergeLeadOptMetaIntoProperties(draft.inputConfig.properties, leadOptPayload) as any,
      confidence: {
        lead_opt_mmp: compactLeadOptForConfidenceWrite(leadOptPayload)
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
    const enumeratedCandidates = compactLeadOptEnumeratedCandidates(snapshot.enumerated_candidates);
    const compactQueryResult = compactLeadOptQueryResult({
      ...queryResult,
      query_id: readText(payload.queryId).trim(),
      task_id: readText(taskId).trim(),
      count: Number.isFinite(Number(queryResult.count)) ? Number(queryResult.count) : payload.transformCount,
      global_count: Number.isFinite(Number(queryResult.global_count)) ? Number(queryResult.global_count) : payload.transformCount
    });
    const inheritedReferenceRecords = hydratePredictionRecordMapFromHistory(
      asPredictionRecordMap(snapshot.reference_prediction_by_backend),
      leadOptHistoricalReferenceRecords
    );
    const leadOptPayload = {
      stage: 'completed',
      query_id: payload.queryId,
      task_id: taskId,
      transform_count: payload.transformCount,
      candidate_count: payload.candidateCount,
      query_result: compactQueryResult,
      result_storage: 'server_query_cache',
      enumerated_candidates: enumeratedCandidates,
      prediction_stage: 'idle',
      prediction_summary: {
        total: 0,
        queued: 0,
        running: 0,
        success: 0,
        failure: 0
      },
      prediction_by_smiles: {},
      reference_prediction_by_backend: inheritedReferenceRecords,
      ...mmpContext
    } as Record<string, unknown>;
    leadOptPersistSnapshotByTaskRowRef.current[taskRowId] = mergeLeadOptSnapshotForPersist(
      leadOptPayload,
      leadOptPersistSnapshotByTaskRowRef.current[taskRowId]
    );
    const sourceTask = resolveLeadOptSourceTask(taskRowId);
    await patchTask(taskRowId, {
      task_state: 'SUCCESS',
      status_text: `MMP complete (${payload.transformCount} transforms, ${payload.candidateCount} rows). Scoring not started.`,
      error_text: '',
      completed_at: completedAt,
      duration_seconds: Number.isFinite(payload.elapsedSeconds) ? payload.elapsedSeconds : null,
      properties: mergeLeadOptMetaIntoProperties(sourceTask?.properties, leadOptPayload) as any,
      confidence: {
        lead_opt_mmp: compactLeadOptForConfidenceWrite(leadOptPayload)
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
    const inheritedReferenceRecords = leadOptHistoricalReferenceRecords;
    const leadOptPayload = {
      stage: 'failed',
      task_id: taskId,
      prediction_stage: 'idle',
      prediction_summary: {
        total: 0,
        queued: 0,
        running: 0,
        success: 0,
        failure: 0
      },
      prediction_by_smiles: {},
      reference_prediction_by_backend: inheritedReferenceRecords,
      ...mmpContext
    } as Record<string, unknown>;
    const sourceTask = resolveLeadOptSourceTask(taskRowId);
    const errorText = readText(payload.error).trim() || 'MMP query failed.';
    const statusText = `MMP query failed${errorText ? `: ${errorText.slice(0, 140)}` : ''}`;
    await patchTask(taskRowId, {
      task_state: 'FAILURE',
      status_text: statusText,
      error_text: errorText,
      completed_at: completedAt,
      properties: mergeLeadOptMetaIntoProperties(sourceTask?.properties, leadOptPayload) as any,
      confidence: {
        lead_opt_mmp: compactLeadOptForConfidenceWrite(leadOptPayload)
      }
    });
    delete leadOptMmpTaskRowMapRef.current[taskId];
    delete leadOptMmpContextByTaskIdRef.current[taskId];
  };

  const handleLeadOptPredictionQueued = useCallback(
    async (payload: { taskId: string; backend: string; candidateSmiles: string }) => {
      const taskId = readText(payload.taskId).trim();
      if (!taskId) return;
      const isLocalTaskId = taskId.startsWith('local:');
      const backend = normalizePredictionBackendStrict(payload.backend);
      if (!backend) return;
      const candidateSmiles = readText(payload.candidateSmiles).trim();
      const predictionKey = buildLeadOptPredictionRecordKey(backend, candidateSmiles);
      if (!predictionKey) return;
      const mappedTaskRowId = readText(leadOptPredictionTaskRowMapRef.current[taskId]).trim();
      const rowIdFromSnapshot = !isLocalTaskId ? resolveLeadOptTaskRowIdByPredictionTaskId(taskId) : '';
      const taskRowId = mappedTaskRowId || rowIdFromSnapshot || resolveLeadOptTaskRowId();
      if (!taskRowId) return;
      leadOptActiveTaskRowIdRef.current = taskRowId;
      leadOptPredictionTaskRowMapRef.current[taskId] = taskRowId;
      const sourceTask = resolveLeadOptSourceTask(taskRowId);
      const sourceLeadOpt = mergeLeadOptSnapshotForPersist(
        resolveLeadOptSnapshotFromTask(sourceTask),
        leadOptPersistSnapshotByTaskRowRef.current[taskRowId]
      );
      const sourceQueryResult = asRecord(sourceLeadOpt.query_result);
      const sourceLeadOptQueryId = readText(sourceLeadOpt.query_id || sourceQueryResult.query_id).trim();
      const nextPredictionMap = compactLeadOptPredictionMap(
        asPredictionRecordMap(sourceLeadOpt.prediction_by_smiles)
      );
      nextPredictionMap[predictionKey] = {
        taskId,
        state: 'QUEUED',
        backend,
        pairIptm: null,
        interfaceMetricValue: null,
        interfaceMetricLabel: 'IPSAE',
        interfaceMetricSource: 'none',
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
      const summary = summarizeLeadOptPredictions(nextPredictionMap);
      const statusText = `Scoring ${Math.max(1, summary.queued + summary.running)} queued (${summary.success}/${Math.max(1, summary.total)} done)`;
      const referenceRecords = compactLeadOptPredictionMap(
        hydratePredictionRecordMapFromHistory(
          asPredictionRecordMap(sourceLeadOpt.reference_prediction_by_backend),
          leadOptHistoricalReferenceRecords
        )
      );
      const nextLeadOpt = {
        ...sourceLeadOpt,
        stage: 'prediction_queued',
        prediction_stage: 'queued',
        prediction_summary: {
          ...summary,
          latest_task_id: taskId
        },
        prediction_task_id: taskId,
        prediction_candidate_smiles: candidateSmiles,
        bucket_count: summary.total,
        prediction_by_smiles: nextPredictionMap,
        reference_prediction_by_backend: referenceRecords
      } as Record<string, unknown>;
      leadOptPersistSnapshotByTaskRowRef.current[taskRowId] = mergeLeadOptSnapshotForPersist(
        nextLeadOpt,
        leadOptPersistSnapshotByTaskRowRef.current[taskRowId]
      );
      const lightweightStateForProperties = {
        stage: nextLeadOpt.stage,
        prediction_stage: nextLeadOpt.prediction_stage,
        query_id: sourceLeadOptQueryId,
        prediction_summary: {
          ...summary,
          latest_task_id: taskId
        },
        prediction_task_id: taskId,
        prediction_candidate_smiles: candidateSmiles,
        bucket_count: summary.total,
        prediction_by_smiles: nextPredictionMap,
        reference_prediction_by_backend: referenceRecords,
        selected_backend: backend,
        target_chain: readText(sourceLeadOpt.target_chain).trim(),
        ligand_chain: readText(sourceLeadOpt.ligand_chain).trim()
      } as Record<string, unknown>;
      const patchPayload = {
        task_state: 'QUEUED',
        status_text: statusText,
        error_text: '',
        confidence: {
          lead_opt_mmp: compactLeadOptForConfidenceWrite(nextLeadOpt)
        },
        properties: mergeLeadOptStateMetaIntoProperties(sourceTask?.properties, lightweightStateForProperties) as any
      };
      queueLeadOptPredictionPersistPatch(taskRowId, patchPayload, { immediate: !isLocalTaskId });
    },
    [
      leadOptHistoricalReferenceRecords,
      queueLeadOptPredictionPersistPatch,
      resolveLeadOptSourceTask,
      resolveLeadOptTaskRowId,
      resolveLeadOptTaskRowIdByPredictionTaskId
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
      const latestTaskId = readText(payload.summary?.latestTaskId).trim();
      const mappedTaskRowId = latestTaskId ? readText(leadOptPredictionTaskRowMapRef.current[latestTaskId]).trim() : '';
      const rowIdFromSnapshot = latestTaskId ? resolveLeadOptTaskRowIdByPredictionTaskId(latestTaskId) : '';
      const taskRowId = mappedTaskRowId || rowIdFromSnapshot || resolveLeadOptTaskRowId();
      if (!taskRowId) return;
      leadOptActiveTaskRowIdRef.current = taskRowId;

      const records = compactLeadOptPredictionMap(asPredictionRecordMap(payload.records));
      const referenceRecords = compactLeadOptPredictionMap(
        hydratePredictionRecordMapFromHistory(
          asPredictionRecordMap(payload.referenceRecords),
          leadOptHistoricalReferenceRecords
        )
      );
      const latestCandidateSmiles = latestTaskId
        ? parseLeadOptPredictionRecordKey(
            Object.entries(records).find(([, record]) => readText(record.taskId).trim() === latestTaskId)?.[0] || ''
          ).smiles
        : '';
      const summary = summarizeLeadOptPredictions(records);
      const latestRecordBackend = latestTaskId
        ? normalizePredictionBackendStrict(
            parseLeadOptPredictionRecordKey(
              Object.entries(records).find(([, record]) => readText(record.taskId).trim() === latestTaskId)?.[0] || ''
            ).backend
          )
        : '';
      const unresolved = summary.queued + summary.running;
      const unresolvedState = summary.running > 0 ? 'RUNNING' : summary.queued > 0 ? 'QUEUED' : null;
      const hasResolvablePendingRecord = Object.values(records).some((record) => {
        const state = readText(record.state).trim().toUpperCase();
        if (state !== 'QUEUED' && state !== 'RUNNING') return false;
        const taskId = readText(record.taskId).trim();
        return taskId.length > 0 && !taskId.startsWith('local:');
      });
      if (unresolved > 0 && !hasResolvablePendingRecord) {
        // Do not persist transient local placeholders; wait for backend-assigned task ids.
        return;
      }
      const sourceTask = resolveLeadOptSourceTask(taskRowId);
      const sourceLeadOpt = mergeLeadOptSnapshotForPersist(
        resolveLeadOptSnapshotFromTask(sourceTask),
        leadOptPersistSnapshotByTaskRowRef.current[taskRowId]
      );
      const sourceQueryResult = asRecord(sourceLeadOpt.query_result);
      const sourceLeadOptQueryId = readText(sourceLeadOpt.query_id || sourceQueryResult.query_id).trim();
      const preferredSelectedBackend =
        latestRecordBackend || normalizePredictionBackendStrict(sourceLeadOpt.selected_backend);
      const nextTaskState: 'QUEUED' | 'RUNNING' | 'SUCCESS' | 'FAILURE' =
        unresolved > 0
          ? unresolvedState === 'RUNNING'
            ? 'RUNNING'
            : 'QUEUED'
          : summary.total > 0 && summary.success === 0 && summary.failure > 0
            ? 'FAILURE'
            : 'SUCCESS';
      const persistedTaskState: 'QUEUED' | 'RUNNING' | 'SUCCESS' | 'FAILURE' = nextTaskState;
      const statusText =
        unresolved > 0
          ? unresolvedState === 'RUNNING'
            ? `Scoring ${unresolved} running (${summary.success}/${Math.max(1, summary.total)} done)`
            : `Scoring ${unresolved} queued (${summary.success}/${Math.max(1, summary.total)} done)`
          : summary.total > 0
            ? `Scoring complete (${summary.success}/${Math.max(1, summary.total)})`
            : 'MMP complete';
      const errorText =
        summary.total > 0 && summary.success === 0 && summary.failure > 0
          ? 'All candidate scoring jobs failed.'
          : '';

      const nextLeadOpt = {
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
          latest_task_id: latestTaskId
        },
        prediction_task_id: latestTaskId,
        prediction_candidate_smiles: latestCandidateSmiles,
        bucket_count: summary.total,
        prediction_by_smiles: records,
        reference_prediction_by_backend: referenceRecords
      } as Record<string, unknown>;
      leadOptPersistSnapshotByTaskRowRef.current[taskRowId] = mergeLeadOptSnapshotForPersist(
        nextLeadOpt,
        leadOptPersistSnapshotByTaskRowRef.current[taskRowId]
      );
      const persistKey = [
        taskRowId,
        statusText,
        errorText,
        summary.total,
        summary.queued,
        summary.running,
        summary.success,
        summary.failure,
        buildLeadOptPredictionPersistSignature(records),
        buildLeadOptPredictionPersistSignature(referenceRecords)
      ].join('|');
      if (leadOptPredictionPersistKeyRef.current === persistKey) return;
      leadOptPredictionPersistKeyRef.current = persistKey;

      const lightweightStateForProperties = {
        stage: nextLeadOpt.stage,
        prediction_stage: nextLeadOpt.prediction_stage,
        query_id: sourceLeadOptQueryId,
        prediction_summary: {
          ...summary,
          latest_task_id: latestTaskId
        },
        prediction_task_id: latestTaskId,
        prediction_candidate_smiles: latestCandidateSmiles,
        bucket_count: summary.total,
        prediction_by_smiles: records,
        reference_prediction_by_backend: referenceRecords,
        ...(preferredSelectedBackend ? { selected_backend: preferredSelectedBackend } : {}),
        target_chain: readText(sourceLeadOpt.target_chain).trim(),
        ligand_chain: readText(sourceLeadOpt.ligand_chain).trim()
      } as Record<string, unknown>;
      const patchPayload = {
        task_state: persistedTaskState,
        status_text: statusText,
        error_text: errorText,
        confidence: {
          lead_opt_mmp: compactLeadOptForConfidenceWrite(nextLeadOpt)
        },
        properties: mergeLeadOptStateMetaIntoProperties(sourceTask?.properties, lightweightStateForProperties) as any
      };
      queueLeadOptPredictionPersistPatch(taskRowId, patchPayload, { immediate: true });
    },
    [
      leadOptHistoricalReferenceRecords,
      queueLeadOptPredictionPersistPatch,
      resolveLeadOptSourceTask,
      resolveLeadOptTaskRowId,
      resolveLeadOptTaskRowIdByPredictionTaskId
    ]
  );

  const handleLeadOptUiStateChange = useCallback(
    (payload: { uiState: LeadOptCandidatesUiState }) => {
      if (!leadOptUiStateScopeKey) return;
      const compactUiState = compactLeadOptCandidatesUiState(payload.uiState);
      const persistKey = [
        leadOptUiStateScopeKey,
        buildLeadOptCandidatesUiStateSignature(compactUiState)
      ].join('|');
      if (leadOptUiStatePersistKeyRef.current === persistKey) return;
      leadOptUiStatePersistKeyRef.current = persistKey;
      writeLeadOptUiStateToLocal(leadOptUiStateScopeKey, compactUiState);
    },
    [leadOptUiStateScopeKey]
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
        String((requestedStatusTaskRow || statusContextTaskRow)?.task_state || '').toUpperCase() === 'DRAFT'
          ? readText((requestedStatusTaskRow || statusContextTaskRow)?.id).trim()
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
      setDraft((prev) =>
        prev
          ? {
              ...prev,
              inputConfig: {
                ...prev.inputConfig,
                components: snapshotComponents
              }
            }
          : prev
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
      setDraft,
      requestedStatusTaskRow,
      statusContextTaskRow,
      workspaceTab
    ]
  );

  const handleCopilotAttachments = useCallback(
    async (attachments: CopilotUploadedAttachment[], _content: string, applications?: CopilotAttachmentApplication[]) => {
      if (!canEdit || attachments.length === 0) return;
      if (!applications || applications.length === 0) return;
      const applicationsById = new Map(
        applications.map((application) => [application.attachmentId, application])
      );
      const roleEntries = attachments.map((attachment) => {
        const application = applicationsById.get(attachment.id);
        if (application && application.fileName !== attachment.name) {
          throw new Error('Copilot attachment declaration does not match the selected file.');
        }
        return {
          attachment,
          role: application?.role || null
        };
      });

      if (isAffinityWorkflow || isPeptideDesignWorkflow) {
        const target = roleEntries.find((entry) => entry.role === 'target')?.attachment || null;
        const ligand = roleEntries.find((entry) => entry.role === 'ligand')?.attachment || null;
        if (target) onAffinityTargetFileChange(target.file);
        if (isAffinityWorkflow && ligand) onAffinityLigandFileChange(ligand.file);
        return;
      }

      if (isLeadOptimizationWorkflow) {
        const target = roleEntries.find((entry) => entry.role === 'target')?.attachment || null;
        const ligand = roleEntries.find((entry) => entry.role === 'ligand')?.attachment || null;
        if (!target && !ligand) return;
        const readUpload = async (attachment: CopilotUploadedAttachment | null) =>
          attachment ? { fileName: attachment.name, content: await attachment.file.text() } : null;
        await handleLeadOptReferenceUploadsChange({
          target: await readUpload(target),
          ligand: await readUpload(ligand)
        });
        return;
      }

      if (isPredictionWorkflow) {
        const template = roleEntries.find((entry) => entry.role === 'template')?.attachment || null;
        if (!template) return;
        const targetProteinComponent = draft.inputConfig.components.find((component) => component.type === 'protein') || null;
        if (!targetProteinComponent) {
          setError('Copilot could not attach the template because the current prediction task has no protein component.');
          return;
        }
        const format = detectStructureFormat(template.name);
        if (!format) {
          setError('Copilot template upload supports .pdb, .cif, or .mmcif files.');
          return;
        }
        try {
          const contentText = await template.file.text();
          const chainSequences = extractProteinChainSequences(contentText, format);
          const chainIds = Object.keys(chainSequences).sort((a, b) => a.localeCompare(b));
          if (chainIds.length === 0) {
            setError('Copilot could not parse a protein chain from the uploaded template.');
            return;
          }
          const upload: ProteinTemplateUpload = {
            fileName: template.name,
            format,
            content: contentText,
            chainId: chainIds[0],
            chainSequences
          };
          setProteinTemplates((prev) => ({ ...prev, [targetProteinComponent.id]: upload }));
          setError(null);
        } catch (err) {
          setError(err instanceof Error ? err.message : 'Failed to read Copilot template upload.');
        }
      }
    },
    [
      canEdit,
      draft.inputConfig.components,
      handleLeadOptReferenceUploadsChange,
      isAffinityWorkflow,
      isLeadOptimizationWorkflow,
      isPeptideDesignWorkflow,
      isPredictionWorkflow,
      onAffinityLigandFileChange,
      onAffinityTargetFileChange,
      setError,
      setProteinTemplates
    ]
  );

  const workflow = getWorkflowDefinition(project.task_type);
  const affinityUseMsa = computeUseMsaFlag(draft.inputConfig.components, draft.use_msa);
  const runSubmitting = submitting || (isLeadOptimizationWorkflow && leadOptHeaderRunPending);
  const leadOptInitialMmpSnapshot = (() => {
    const leadOptMmp = aggregatedLeadOptSnapshot;
    if (!leadOptMmp || Object.keys(leadOptMmp).length === 0) return null;
    const queryResult = asRecord(leadOptMmp.query_result);
    const queryId = readText(leadOptMmp.query_id || queryResult.query_id).trim();
    if (!queryId) return null;
    return {
      query_result: {
        query_id: queryId,
        task_id: readText(leadOptMmp.task_id || queryResult.task_id).trim(),
        query_mode: readText(queryResult.query_mode || 'one-to-many') || 'one-to-many',
        aggregation_type: readText(queryResult.aggregation_type).trim(),
        property_targets: asRecord(queryResult.property_targets),
        rule_env_radius: Number.isFinite(Number(queryResult.rule_env_radius)) ? Number(queryResult.rule_env_radius) : 1,
        grouped_by_environment:
          readBooleanToken(queryResult.grouped_by_environment) === null
            ? undefined
            : readBooleanToken(queryResult.grouped_by_environment),
        mmp_database_id: readText(queryResult.mmp_database_id).trim(),
        mmp_database_label: readText(queryResult.mmp_database_label).trim(),
        mmp_database_schema: readText(queryResult.mmp_database_schema).trim(),
        cluster_group_by: readText(queryResult.cluster_group_by).trim(),
        transforms: Array.isArray(queryResult.transforms) ? queryResult.transforms : [],
        global_transforms: Array.isArray(queryResult.global_transforms) ? queryResult.global_transforms : [],
        clusters: Array.isArray(queryResult.clusters) ? queryResult.clusters : [],
        stats: asRecord(queryResult.stats),
        count: Number(queryResult.count || 0),
        global_count: Number(queryResult.global_count || 0)
      },
      enumerated_candidates: compactLeadOptEnumeratedCandidates(leadOptMmp.enumerated_candidates),
      prediction_by_smiles: compactLeadOptPredictionMap(asPredictionRecordMap(leadOptMmp.prediction_by_smiles)),
      reference_prediction_by_backend: compactLeadOptPredictionMap(
        hydratePredictionRecordMapFromHistory(
          asPredictionRecordMap(leadOptMmp.reference_prediction_by_backend),
          leadOptHistoricalReferenceRecords
        )
      ),
      ui_state: leadOptUserScopedUiState ? compactLeadOptCandidatesUiState(leadOptUserScopedUiState) : {},
      selection: asRecord(leadOptMmp.selection),
      query_payload: asRecord(leadOptMmp.query_payload),
      task_row_id: leadOptActiveTaskRowId,
      task_id: readText(leadOptMmp.task_id || queryResult.task_id).trim(),
      query_cache_state: readText(leadOptMmp.query_cache_state).trim().toLowerCase(),
      candidate_count: Number.isFinite(Number(leadOptMmp.candidate_count)) ? Number(leadOptMmp.candidate_count) : 0,
      transform_count: Number.isFinite(Number(leadOptMmp.transform_count)) ? Number(leadOptMmp.transform_count) : 0,
      target_chain: readText(leadOptMmp.target_chain).trim(),
      ligand_chain: readText(leadOptMmp.ligand_chain).trim()
    } as Record<string, unknown>;
  })();
  const leadOptSnapshotContext = asRecord(leadOptInitialMmpSnapshot || null);
  const leadOptSnapshotSelection = asRecord(leadOptSnapshotContext.selection);
  const leadOptSnapshotQueryPayload = asRecord(leadOptSnapshotContext.query_payload);
  const leadOptSnapshotTargetChain =
    readText(leadOptSnapshotContext.target_chain).trim() ||
    readText(leadOptSnapshotSelection.target_chain).trim() ||
    readText(leadOptSnapshotQueryPayload.target_chain).trim();
  const leadOptSnapshotLigandChain =
    readText(leadOptSnapshotContext.ligand_chain).trim() ||
    readText(leadOptSnapshotSelection.ligand_chain).trim() ||
    readText(leadOptSnapshotQueryPayload.ligand_chain).trim();
  const leadOptWorkspaceTargetChain = leadOptSnapshotTargetChain || readText(leadOptChainContext.targetChain).trim();
  const leadOptWorkspaceLigandChain = leadOptSnapshotLigandChain || readText(leadOptChainContext.ligandChain).trim();
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
    isVirtualScreeningWorkflow,
    isAffinityWorkflow,
    isLeadOptimizationWorkflow,
    hasIncompleteComponents,
    componentCompletion,
    virtualScreeningInput: draft.inputConfig.options.virtualScreeningInput || '',
    virtualScreeningComponents: draft.inputConfig.components,
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
    constraintsSupported: allowedConstraintTypes.length > 0,
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
    focusConstraintPickSlot,
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
    updateConstraintPickSlot,
    activeChainInfos,
    selectedTemplatePreview,
    selectedTemplateResidueIndexMap,
    setPickedResidue,
    canEdit,
    ligandChainOptions,
    constraintsSupported: allowedConstraintTypes.length > 0,
    isBondOnlyBackend
  });
  const {
    displayStructureText,
    displayStructureConfidenceText,
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
    affinityResultLigandAtomPlddts,
    predictionLigandPreview,
    predictionLigandRadarSmiles,
    affinityDisplayStructureText,
    affinityDisplayStructureFormat,
    hasAffinityDisplayStructure,
  } = useProjectResultDisplay({
    shouldPrepareResultStructure: workspaceTab === 'results' && isPredictionWorkflow && !isVirtualScreeningWorkflow,
    shouldPrepareConstraintStructure: workspaceTab === 'constraints',
    shouldPrepareSnapshotCards:
      workspaceTab === 'results' && !isVirtualScreeningWorkflow && (isPredictionWorkflow || isAffinityWorkflow),
    shouldPreparePredictionLigandPreview:
      workspaceTab === 'results' && isPredictionWorkflow && !isVirtualScreeningWorkflow,
    shouldPrepareAffinityResultDisplay: workspaceTab === 'results' && isAffinityWorkflow,
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
    selectedResultLigandModifications: selectedResultLigandComponent?.modifications,
    snapshotLigandResiduePlddts,
  });
  const {
    handlePredictionComponentsChange,
    handlePredictionProteinTemplateChange,
    handlePredictionTemplateResiduePick,
    handleRuntimeBackendChange,
    handleRuntimeSeedChange,
    handleRuntimeLowVramChange,
    handleRuntimePeptideDesignModeChange,
    handleRuntimePeptideBinderLengthChange,
    handleRuntimePeptideUseInitialSequenceChange,
    handleRuntimePeptideInitialSequenceChange,
    handleRuntimePeptideSequenceMaskChange,
    handleRuntimePeptideIterationsChange,
    handleRuntimePeptidePopulationSizeChange,
    handleRuntimePeptideEliteSizeChange,
    handleRuntimePeptideMutationRateChange,
    handleRuntimePeptideResiduePoolChange,
    handleRuntimePeptideNonNaturalRangeChange,
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
    isPeptideDesignWorkflow,
    setDraft,
    setPickedResidue,
    setProteinTemplates,
    filterConstraintsByBackend
  });
  const { predictionConstraintsWorkspaceProps, predictionComponentsSidebarProps } = usePredictionWorkspaceProps({
    workspaceTab,
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
    canEdit,
    setWorkspaceTab,
    navigateConstraint,
    pickedResidue,
    hasConstraintStructure,
    constraintStructureText,
    constraintStructureFormat,
    constraintViewerHighlightResidues,
    constraintViewerActiveResidue,
    constraintSelectedAtomRefs,
    applyPickToSelectedConstraint,
    focusConstraintPickSlot,
    activeConstraintPickSlot: constraintPickSlot[activeConstraintId ?? ''] ?? 'first',
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
  const peptideResiduePoolAvailable = useMemo(() => {
    if (!isPeptideDesignWorkflow) return true;
    const query = new URLSearchParams(runtime.locationSearch);
    const requestedTaskRowId = readText(query.get('task_row_id')).trim();
    const taskRow =
      (requestedTaskRowId
        ? projectTasks.find((row) => readText(row.id).trim() === requestedTaskRowId)
        : null) ||
      requestedStatusTaskRow ||
      statusContextTaskRow ||
      activeResultTask ||
      null;
    const state = readText(taskRow?.task_state).trim().toUpperCase();
    if (state && state !== 'DRAFT') return hasExplicitPeptideResiduePool(taskRow);
    return true;
  }, [activeResultTask, isPeptideDesignWorkflow, projectTasks, requestedStatusTaskRow, runtime.locationSearch, statusContextTaskRow]);

  const handlePeptideRequestStructure = useCallback(async (options?: { preferredStructureName?: string }) => {
    const contextTask = activeResultTask || statusContextTaskRow;
    const taskId = String(contextTask?.task_id || project.task_id || '').trim();
    if (!taskId) return;
    await pullResultForViewer(taskId, {
      taskRowId: contextTask?.id || undefined,
      persistProject: String(project.task_id || '').trim() === taskId,
      resultMode: 'view',
      preferredStructureName: options?.preferredStructureName
    });
  }, [activeResultTask, project.task_id, pullResultForViewer, statusContextTaskRow]);

  const handleVirtualScreeningPredictionsChange = useCallback((
    records: Record<string, VirtualScreeningPredictionRecord>
  ) => {
    if (!isVirtualScreeningWorkflow || !canEdit) return;
    const normalizedRecords = records || {};
    const screeningTaskRow = statusContextTaskRow || requestedStatusTaskRow || activeResultTask;
    const signature = `${project.id}:${readText(screeningTaskRow?.id).trim()}:${JSON.stringify(
      Object.keys(normalizedRecords)
        .sort((left, right) => left.localeCompare(right))
        .map((key) => [key, normalizedRecords[key]])
    )}`;
    if (virtualScreeningPredictionPersistSignatureRef.current === signature) return;
    virtualScreeningPredictionPersistSignatureRef.current = signature;

    const nextOptions: ProjectInputConfig['options'] = {
      ...draft.inputConfig.options,
      virtualScreeningPredictions: normalizedRecords
    };
    const nextConfig: ProjectInputConfig = {
      ...draft.inputConfig,
      options: nextOptions
    };
    setDraft((previous) => {
      if (!previous) return previous;
      return {
        ...previous,
        inputConfig: {
          ...previous.inputConfig,
          options: {
            ...previous.inputConfig.options,
            virtualScreeningPredictions: normalizedRecords
          }
        }
      };
    });
    saveProjectInputConfig(project.id, nextConfig);

    if (!screeningTaskRow?.id) return;
    const sourceProperties = (screeningTaskRow.properties || nextConfig.properties) as ProjectInputConfig['properties'];
    const patchPayload = {
      properties: mergeTaskInputOptionsIntoProperties(sourceProperties, nextOptions)
    };
    virtualScreeningPredictionPersistQueueRef.current =
      virtualScreeningPredictionPersistQueueRef.current
        .catch(() => undefined)
        .then(async () => {
          await patchTask(screeningTaskRow.id, patchPayload);
        })
        .catch((persistError) => {
          setError(
            persistError instanceof Error
              ? persistError.message
              : 'Failed to persist virtual-screening structure jobs.'
          );
        });
  }, [
    activeResultTask,
    canEdit,
    draft.inputConfig,
    isVirtualScreeningWorkflow,
    patchTask,
    project.id,
    requestedStatusTaskRow,
    saveProjectInputConfig,
    setDraft,
    setError,
    statusContextTaskRow
  ]);

  const {
    projectResultsSectionProps,
    affinityWorkflowSectionProps,
    leadOptimizationWorkflowSectionProps,
    predictionWorkflowSectionProps,
    virtualScreeningWorkflowSectionProps,
    workflowRuntimeSettingsSectionProps
  } = useProjectWorkflowSectionProps({
    isPredictionWorkflow,
    isPeptideDesignWorkflow,
    isVirtualScreeningWorkflow,
    isAffinityWorkflow,
    isLeadOptimizationWorkflow,
    workflowTitle: workflow.title,
    workflowShortTitle: workflow.shortTitle,
    projectTaskState: displayTaskState || project.task_state || '',
    projectTaskId:
      String(statusContextTaskRow?.task_id || '').trim() ||
      String(activeResultTask?.task_id || '').trim() ||
      String(project.task_id || '').trim(),
    statusInfo: statusInfo || null,
    progressPercent,
    onPeptideRequestStructure: handlePeptideRequestStructure,
    resultsGridRef,
    isResultsResizing,
    resultsGridStyle,
    onResultsResizerPointerDown: handleResultsResizerPointerDown,
    onResultsResizerKeyDown: handleResultsResizerKeyDown,
    snapshotCards,
    snapshotConfidence: snapshotConfidence || null,
    snapshotAffinity: snapshotAffinity || null,
    resultChainIds,
    selectedResultTargetChainId,
    selectedResultLigandChainId,
    displayStructureText: isPeptideDesignWorkflow ? structureText : displayStructureText,
    displayStructureConfidenceText,
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
    affinityResultLigandAtomPlddts,
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
    affinityMode,
    affinityUseMsa,
    affinityConfidenceOnlyUiValue,
    affinityConfidenceOnlyUiLocked,
    affinityPreviewStructureText,
    affinityPreviewStructureFormat,
    affinityPreviewLigandOverlayText,
    affinityPreviewLigandOverlayFormat,
    onAffinityTargetFileChange,
    onAffinityLigandFileChange,
    onAffinityUseMsaChange,
    onAffinityConfidenceOnlyChange,
    onAffinityModeChange,
    setAffinityLigandSmiles,
    leadOptProteinSequence: leadOptPrimary.proteinSequence,
    leadOptLigandSmiles: leadOptPrimary.ligandSmiles,
    leadOptTargetChain: leadOptWorkspaceTargetChain,
    leadOptLigandChain: leadOptWorkspaceLigandChain,
    leadOptReferenceScopeKey: `${project.id}:leadopt`,
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
    virtualScreeningInput: draft.inputConfig.options.virtualScreeningInput || '',
    virtualScreeningInputMode: draft.inputConfig.options.virtualScreeningInputMode || 'upload',
    virtualScreeningInputFileName: draft.inputConfig.options.virtualScreeningInputFileName || '',
    virtualScreeningPredictionRecords:
      draft.inputConfig.options.virtualScreeningPredictions || {},
    onVirtualScreeningPredictionRecordsChange: handleVirtualScreeningPredictionsChange,
    proteinTemplates,
    customResidueLibrary,
    onCustomResidueLibraryChange: setCustomResidueLibrary,
    onProteinTemplateChange: handlePredictionProteinTemplateChange,
    activeComponentId,
    setActiveComponentId,
    onProteinTemplateResiduePick: handlePredictionTemplateResiduePick,
    predictionConstraintsWorkspaceProps,
    predictionComponentsSidebarProps,
    backend: draft.backend,
    seed: draft.inputConfig.options.seed ?? null,
    lowVram: draft.inputConfig.options.lowVram ?? false,
    peptideDesignMode: draft.inputConfig.options.peptideDesignMode ?? 'linear',
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
    peptideResiduePool: draft.inputConfig.options.peptideResiduePool ?? [],
    peptideResiduePoolAvailable,
    peptideNonNaturalMin: draft.inputConfig.options.peptideNonNaturalMin ?? 0,
    peptideNonNaturalMax: draft.inputConfig.options.peptideNonNaturalMax ?? 0,
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
    onLowVramChange: handleRuntimeLowVramChange,
    onPeptideDesignModeChange: handleRuntimePeptideDesignModeChange,
    onPeptideBinderLengthChange: handleRuntimePeptideBinderLengthChange,
    onPeptideUseInitialSequenceChange: handleRuntimePeptideUseInitialSequenceChange,
    onPeptideInitialSequenceChange: handleRuntimePeptideInitialSequenceChange,
    onPeptideSequenceMaskChange: handleRuntimePeptideSequenceMaskChange,
    onPeptideIterationsChange: handleRuntimePeptideIterationsChange,
    onPeptidePopulationSizeChange: handleRuntimePeptidePopulationSizeChange,
    onPeptideEliteSizeChange: handleRuntimePeptideEliteSizeChange,
    onPeptideMutationRateChange: handleRuntimePeptideMutationRateChange,
    onPeptideResiduePoolChange: handleRuntimePeptideResiduePoolChange,
    onPeptideNonNaturalRangeChange: handleRuntimePeptideNonNaturalRangeChange,
    onPeptideBicyclicLinkerCcdChange: handleRuntimePeptideBicyclicLinkerCcdChange,
    onPeptideBicyclicCysPositionModeChange: handleRuntimePeptideBicyclicCysPositionModeChange,
    onPeptideBicyclicFixTerminalCysChange: handleRuntimePeptideBicyclicFixTerminalCysChange,
    onPeptideBicyclicIncludeExtraCysChange: handleRuntimePeptideBicyclicIncludeExtraCysChange,
    onPeptideBicyclicCys1PosChange: handleRuntimePeptideBicyclicCys1PosChange,
    onPeptideBicyclicCys2PosChange: handleRuntimePeptideBicyclicCys2PosChange,
    onPeptideBicyclicCys3PosChange: handleRuntimePeptideBicyclicCys3PosChange
  });
  const taskListPage = useMemo(() => {
    const query = new URLSearchParams(runtime.locationSearch);
    const parsed = Number(query.get('task_list_page') || '');
    if (!Number.isFinite(parsed)) return 1;
    return Math.max(1, Math.floor(parsed));
  }, [runtime.locationSearch]);
  const taskHistoryPath = useMemo(() => {
    const query = new URLSearchParams();
    if (taskListPage > 1) {
      query.set('page', String(taskListPage));
    }
    const search = query.toString();
    return `/projects/${project.id}/tasks${search ? `?${search}` : ''}`;
  }, [project.id, taskListPage]);
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
  const submitTaskRef = useRef(submitTask);
  const runDisabledRef = useRef(effectiveRunDisabled);
  const runBlockedReasonRef = useRef(effectiveRunBlockedReason);

  useEffect(() => {
    submitTaskRef.current = submitTask;
    runDisabledRef.current = effectiveRunDisabled;
    runBlockedReasonRef.current = effectiveRunBlockedReason;
  }, [effectiveRunBlockedReason, effectiveRunDisabled, submitTask]);

  const headerRuntimeTaskId =
    readText(statusContextTaskRow?.task_id).trim() ||
    readText(activeResultTask?.task_id).trim() ||
    readText(project.task_id).trim();
  const headerRuntimeStateToken = readText(displayTaskState || project.task_state).trim().toUpperCase();
  const showHeaderStopAction =
    isPeptideDesignWorkflow &&
    Boolean(headerRuntimeTaskId) &&
    (headerRuntimeStateToken === 'RUNNING' || headerRuntimeStateToken === 'QUEUED');
  const headerStopRunTitle = headerRuntimeStateToken === 'RUNNING' ? 'Stop run' : 'Cancel queued run';
  const headerStopRunDisabled = !showHeaderStopAction || headerStopRunPending || runSubmitting;
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
  const handleHeaderStopAction = () => {
    if (!showHeaderStopAction || headerStopRunDisabled) return;
    if (!headerRuntimeTaskId) return;
    const actionToken = headerRuntimeStateToken === 'RUNNING' ? 'stop' : 'cancel';
    if (!window.confirm(`Confirm ${actionToken} for task "${headerRuntimeTaskId}"?`)) return;
    setHeaderStopRunPending(true);
    setError(null);
    void terminateBackendTask(headerRuntimeTaskId)
      .then(async (response) => {
        if (response.terminated !== true) {
          throw new Error(`Backend did not confirm ${actionToken} for task "${headerRuntimeTaskId}".`);
        }
        await loadProject();
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : `Failed to ${actionToken} running task.`);
      })
      .finally(() => {
        setHeaderStopRunPending(false);
      });
  };

  const applyTaskDetailCopilotAction = useCallback(async (action: CopilotPlanAction) => {
    const patch = asRecord(action.payload?.parameterPatch);
    const applyMetadataPatch = async () => {
      const metadataPatch = asRecord(action.payload?.metadataPatch);
      const hasNamePatch = Object.prototype.hasOwnProperty.call(metadataPatch, 'taskName');
      const hasSummaryPatch = Object.prototype.hasOwnProperty.call(metadataPatch, 'taskSummary');
      const nextName = readText(metadataPatch.taskName).trim();
      const nextSummary = normalizeTaskSummary(readText(metadataPatch.taskSummary));
      if (!hasNamePatch && !hasSummaryPatch) throw new Error('No task name or description update was provided.');
      if (hasNamePatch && !nextName) throw new Error('Task name cannot be empty.');
      if (!canEdit) throw new Error('This project is read-only for your account.');

      const taskRow = activeResultTask || statusContextTaskRow;
      if (!taskRow?.id) throw new Error('No current task to update.');

      if (hasNamePatch) handleTaskNameChange(nextName);
      if (hasSummaryPatch) handleTaskSummaryChange(nextSummary);

      const payload: Partial<ProjectTask> = {};
      if (hasNamePatch) payload.name = nextName;
      if (hasSummaryPatch) payload.summary = nextSummary;
      await patchTask(taskRow.id, payload);
    };
    const applyPatch = () => {
      const seed = readFiniteNumber(patch.seed);
      if (seed !== null) handleRuntimeSeedChange(Math.floor(seed));
      const backendPatch = readText(patch.backend).trim().toLowerCase();
      const backendPatchAllowed = isVirtualScreeningWorkflow
        ? backendPatch === 'nesso'
        : backendPatch === 'boltz' || backendPatch === 'alphafold3' || backendPatch === 'protenix';
      if (backendPatchAllowed) {
        handleRuntimeBackendChange(backendPatch);
      }
      const affinityModePatch = readText(patch.affinityMode).trim();
      if (affinityModePatch === 'score' || affinityModePatch === 'pose' || affinityModePatch === 'refine' || affinityModePatch === 'interface') {
        onAffinityModeChange(affinityModePatch);
      }
      const peptideDesignMode = readText(patch.peptideDesignMode).trim();
      if (peptideDesignMode === 'linear' || peptideDesignMode === 'cyclic' || peptideDesignMode === 'bicyclic') {
        handleRuntimePeptideDesignModeChange(peptideDesignMode);
      }
      const peptideBinderLength = readFiniteNumber(patch.peptideBinderLength);
      if (peptideBinderLength !== null) handleRuntimePeptideBinderLengthChange(Math.max(1, Math.floor(peptideBinderLength)));
      const peptideIterations = readFiniteNumber(patch.peptideIterations);
      if (peptideIterations !== null) handleRuntimePeptideIterationsChange(Math.max(1, Math.floor(peptideIterations)));
      const peptidePopulationSize = readFiniteNumber(patch.peptidePopulationSize);
      if (peptidePopulationSize !== null) handleRuntimePeptidePopulationSizeChange(Math.max(1, Math.floor(peptidePopulationSize)));
      const peptideEliteSize = readFiniteNumber(patch.peptideEliteSize);
      if (peptideEliteSize !== null) handleRuntimePeptideEliteSizeChange(Math.max(1, Math.floor(peptideEliteSize)));
      const peptideMutationRate = readFiniteNumber(patch.peptideMutationRate);
      if (peptideMutationRate !== null) handleRuntimePeptideMutationRateChange(Math.min(1, Math.max(0, peptideMutationRate)));
      if (typeof patch.peptideUseInitialSequence === 'boolean') {
        handleRuntimePeptideUseInitialSequenceChange(patch.peptideUseInitialSequence);
      }
      const peptideInitialSequence = readText(patch.peptideInitialSequence).trim();
      if (peptideInitialSequence) handleRuntimePeptideInitialSequenceChange(peptideInitialSequence);
      const peptideSequenceMask = readText(patch.peptideSequenceMask).trim();
      if (peptideSequenceMask) handleRuntimePeptideSequenceMaskChange(peptideSequenceMask);
      const peptideBicyclicLinkerCcd = readText(patch.peptideBicyclicLinkerCcd).trim();
      if (peptideBicyclicLinkerCcd === 'SEZ' || peptideBicyclicLinkerCcd === '29N' || peptideBicyclicLinkerCcd === 'BS3') {
        handleRuntimePeptideBicyclicLinkerCcdChange(peptideBicyclicLinkerCcd);
      }
      const peptideBicyclicCysPositionMode = readText(patch.peptideBicyclicCysPositionMode).trim();
      if (peptideBicyclicCysPositionMode === 'auto' || peptideBicyclicCysPositionMode === 'manual') {
        handleRuntimePeptideBicyclicCysPositionModeChange(peptideBicyclicCysPositionMode);
      }
      if (typeof patch.peptideBicyclicFixTerminalCys === 'boolean') {
        handleRuntimePeptideBicyclicFixTerminalCysChange(patch.peptideBicyclicFixTerminalCys);
      }
      if (typeof patch.peptideBicyclicIncludeExtraCys === 'boolean') {
        handleRuntimePeptideBicyclicIncludeExtraCysChange(patch.peptideBicyclicIncludeExtraCys);
      }
      const peptideBicyclicCys1Pos = readFiniteNumber(patch.peptideBicyclicCys1Pos);
      if (peptideBicyclicCys1Pos !== null) handleRuntimePeptideBicyclicCys1PosChange(Math.max(1, Math.floor(peptideBicyclicCys1Pos)));
      const peptideBicyclicCys2Pos = readFiniteNumber(patch.peptideBicyclicCys2Pos);
      if (peptideBicyclicCys2Pos !== null) handleRuntimePeptideBicyclicCys2PosChange(Math.max(1, Math.floor(peptideBicyclicCys2Pos)));
      const peptideBicyclicCys3Pos = readFiniteNumber(patch.peptideBicyclicCys3Pos);
      if (peptideBicyclicCys3Pos !== null) handleRuntimePeptideBicyclicCys3PosChange(Math.max(1, Math.floor(peptideBicyclicCys3Pos)));
      const componentsReplacement = asRecord(patch.componentsReplacement);
      const replacementComponentsRaw = componentsReplacement.components;
      if (Array.isArray(replacementComponentsRaw)) {
        const replacementComponents = replacementComponentsRaw
          .map((component) => (component && typeof component === 'object' ? (component as InputComponent) : null))
          .filter((component): component is InputComponent => Boolean(component?.type && readText(component.sequence).trim()));
        if (replacementComponents.length > 0) {
          setDraft((prev) =>
            prev
              ? {
                  ...prev,
                  inputConfig: {
                    ...prev.inputConfig,
                    version: 1,
                    components: replacementComponents,
                    constraints: componentsReplacement.clearConstraints === false ? prev.inputConfig.constraints : []
                  }
                }
              : prev
          );
        }
      }
    };
    if (action.id === 'task_detail:apply_parameter_patch') {
      applyPatch();
      return;
    }
    if (action.id === 'task_detail:apply_metadata_patch') {
      await applyMetadataPatch();
      return;
    }
    if (action.id === 'task_detail:save_draft') {
      await saveDraft();
      return;
    }
    if (action.id === 'task_detail:apply_patch_and_submit') {
      applyPatch();
      await new Promise((resolve) => window.setTimeout(resolve, 0));
      if (runDisabledRef.current) {
        throw new Error(runBlockedReasonRef.current || 'Current task cannot be submitted yet.');
      }
      await submitTaskRef.current();
      return;
    }
    if (action.id === 'task_detail:submit_current') {
      if (runDisabledRef.current) {
        throw new Error(runBlockedReasonRef.current || 'Current task cannot be submitted yet.');
      }
      await submitTaskRef.current();
      return;
    }
    if (action.id === 'task_detail:cancel_current') {
      const taskRow = statusContextTaskRow || activeResultTask;
      const runtimeTaskId = String(taskRow?.task_id || headerRuntimeTaskId || '').trim();
      const runtimeState = String(taskRow?.task_state || displayTaskState || '').trim().toUpperCase();
      if (!canEdit) throw new Error('This project is read-only for your account.');
      if (!runtimeTaskId) throw new Error('No active runtime task_id is available to cancel.');
      if (runtimeState !== 'QUEUED' && runtimeState !== 'RUNNING') {
        throw new Error('Current task is not running or queued.');
      }
      const response = await terminateBackendTask(runtimeTaskId);
      if (response.terminated !== true) {
        throw new Error(`Backend did not confirm cancellation for task "${runtimeTaskId}".`);
      }
      await loadProject();
      return;
    }
    if (action.id === 'task_detail:delete_current') {
      const taskRow = activeResultTask || statusContextTaskRow;
      if (!taskRow) throw new Error('No current task to delete.');
      if (!canEdit) throw new Error('This project is read-only for your account.');
      const runtimeTaskId = String(taskRow.task_id || '').trim();
      const runtimeState = String(taskRow.task_state || displayTaskState || '').trim().toUpperCase();
      if ((runtimeState === 'QUEUED' || runtimeState === 'RUNNING') && !runtimeTaskId) {
        throw new Error('Task is active but task_id is missing; deletion is blocked to avoid orphan runtime.');
      }
      if (runtimeState === 'QUEUED' || runtimeState === 'RUNNING') {
        const response = await terminateBackendTask(runtimeTaskId);
        if (response.terminated !== true) {
          throw new Error(`Backend did not confirm cancellation for task "${runtimeTaskId}".`);
        }
      }
      await deleteProjectTask(taskRow.id);
      await loadProject();
      navigate(`/projects/${project.id}/tasks`, { replace: true });
    }
  }, [
    activeResultTask,
    canEdit,
    displayTaskState,
    effectiveRunBlockedReason,
    effectiveRunDisabled,
    handleHeaderRunAction,
    handleRuntimePeptideBinderLengthChange,
    handleRuntimeBackendChange,
    handleRuntimePeptideBicyclicCys1PosChange,
    handleRuntimePeptideBicyclicCys2PosChange,
    handleRuntimePeptideBicyclicCys3PosChange,
    handleRuntimePeptideBicyclicCysPositionModeChange,
    handleRuntimePeptideBicyclicFixTerminalCysChange,
    handleRuntimePeptideBicyclicIncludeExtraCysChange,
    handleRuntimePeptideBicyclicLinkerCcdChange,
    handleRuntimePeptideDesignModeChange,
    handleRuntimePeptideEliteSizeChange,
    handleRuntimePeptideInitialSequenceChange,
    handleRuntimePeptideIterationsChange,
    handleRuntimePeptideMutationRateChange,
    handleRuntimePeptideResiduePoolChange,
    handleRuntimePeptideNonNaturalRangeChange,
    handleRuntimePeptidePopulationSizeChange,
    handleRuntimePeptideSequenceMaskChange,
    handleRuntimePeptideUseInitialSequenceChange,
    handleRuntimeSeedChange,
    handleTaskNameChange,
    handleTaskSummaryChange,
    headerRuntimeTaskId,
    isVirtualScreeningWorkflow,
    navigate,
    onAffinityModeChange,
    patchTask,
    project.id,
    loadProject,
    saveDraft,
    setError,
    statusContextTaskRow
  ]);


  const copilotContextPayload = useMemo(() => {
    const statusTaskRowId = readText(statusContextTaskRow?.id).trim();
    const statusTaskId = readText(statusContextTaskRow?.task_id).trim();
    const statusTaskState = readText(statusContextTaskRow?.task_state).trim();
    const activeResultTaskRowId = readText(activeResultTask?.id).trim();
    const activeResultTaskId = readText(activeResultTask?.task_id).trim();
    const activeResultTaskState = readText(activeResultTask?.task_state).trim();
    return {
      page: {
        contextType: 'task_detail',
        workflowKey: workflow.key,
        workflowTitle: workflow.title,
        workflowShortTitle: workflow.shortTitle,
        runLabel: workflow.runLabel,
        supportsSequenceInputs: workflow.supportsSequenceInputs,
        availableActions: [
          'analyze_current_context',
          'plan_confirmed_parameter_patch',
          'plan_confirmed_submit',
          'plan_confirmed_cancel_current_task',
          'plan_confirmed_delete_current_task',
          'plan_confirmed_metadata_update',
          'apply_copilot_uploaded_files_when_supported'
        ]
      },
      project: { id: project.id, name: project.name, task_type: project.task_type, workflow_key: workflow.key },
      draft: {
        taskName: draft.taskName,
        taskSummary: draft.taskSummary,
        backend: draft.backend,
        options: draft.inputConfig?.options,
        components: summarizeCopilotComponents(draft.inputConfig?.components),
        constraints: summarizeCopilotConstraints(draft.inputConfig?.constraints)
      },
      runtime: {
        displayTaskState,
        runDisabled: effectiveRunDisabled,
        runBlockedReason: effectiveRunBlockedReason,
        activeTaskId: headerRuntimeTaskId,
        statusTaskRowId,
        statusTaskId,
        statusTaskState,
        activeResultTaskRowId,
        activeResultTaskId,
        activeResultTaskState,
        authoritativeTaskState:
          readText(displayTaskState).trim() ||
          statusTaskState ||
          activeResultTaskState ||
          readText(project.task_state).trim()
      },
      affinityUploads: isAffinityWorkflow
        ? {
            targetFileName: affinityTargetFile?.name || '',
            ligandFileName: affinityLigandFile?.name || '',
            targetUploaded: Boolean(affinityTargetFile),
            ligandUploaded: Boolean(affinityLigandFile)
          }
        : undefined,
      currentTask: summarizeCopilotTask(statusContextTaskRow || activeResultTask || null)
    };
  }, [
    activeResultTask,
    affinityLigandFile,
    affinityTargetFile,
    displayTaskState,
    draft.inputConfig?.components,
    draft.inputConfig?.constraints,
    draft.inputConfig?.options,
    draft.backend,
    draft.taskName,
    draft.taskSummary,
    effectiveRunBlockedReason,
    effectiveRunDisabled,
    headerRuntimeTaskId,
    isAffinityWorkflow,
    project.id,
    project.name,
    project.task_state,
    project.task_type,
    statusContextTaskRow,
    workflow.key,
    workflow.runLabel,
    workflow.shortTitle,
    workflow.supportsSequenceInputs,
    workflow.title
  ]);

  return (
    <>
    <ProjectDetailLayout
      projectName={project.name}
      canDownloadResult={Boolean(
        isLeadOptimizationWorkflow ? (leadOptDownloadRecords.length > 0 || leadOptDownloadTaskId) : defaultDownloadTaskId
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
      isVirtualScreeningWorkflow={isVirtualScreeningWorkflow}
      isAffinityWorkflow={isAffinityWorkflow}
      isLeadOptimizationWorkflow={isLeadOptimizationWorkflow}
      constraintsSupported={allowedConstraintTypes.length > 0}
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
      showStopAction={showHeaderStopAction}
      stopSubmitting={headerStopRunPending}
      stopDisabled={headerStopRunDisabled}
      stopTitle={headerStopRunTitle}
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
      virtualScreeningSectionProps={virtualScreeningWorkflowSectionProps}
      runtimeSettingsProps={workflowRuntimeSettingsSectionProps}
      runActionRef={runActionRef as RefObject<HTMLDivElement>}
      topRunButtonRef={topRunButtonRef as RefObject<HTMLButtonElement>}
      onOpenTaskHistory={handleOpenTaskHistory}
      onDownloadResult={() => {
        setError(null);
        if (isLeadOptimizationWorkflow) {
          void downloadLeadOptCombinedArchive({
            predictionMap: aggregatedLeadOptSnapshotRecord.prediction_by_smiles,
            preferredBackend: aggregatedLeadOptSnapshotRecord.selected_backend,
            projectName: project.name,
            queryId:
              readText(aggregatedLeadOptSnapshotRecord.query_id).trim() ||
              readText(asRecord(aggregatedLeadOptSnapshotRecord.query_result).query_id).trim(),
            fallbackTaskId: leadOptDownloadTaskId,
          }).catch((err) => {
            setError(err instanceof Error ? err.message : 'Failed to download lead-opt result archive.');
          });
          return;
        }
        if (!defaultDownloadTaskId) return;
        void downloadResultFile(defaultDownloadTaskId).catch((err) => {
          setError(err instanceof Error ? err.message : 'Failed to download result archive.');
        });
      }}
      onSaveDraft={() => {
        void saveDraft();
      }}
      onReset={handleResetFromHeader}
      onRunAction={handleHeaderRunAction}
      onStopAction={handleHeaderStopAction}
      onRestoreSavedDraft={handleRestoreSavedDraft}
      onRunCurrentDraft={handleRunCurrentDraft}
      onWorkspaceTabChange={setWorkspaceTab}
      onTaskNameChange={handleTaskNameChange}
      onTaskSummaryChange={handleTaskSummaryChange}
      onWorkspaceFormSubmit={handleWorkspaceFormSubmit}
    />
    {copilotAvailable && session?.userId ? (
      <ProjectCopilotModal
        open={copilotOpen}
        title="Copilot"
        subtitle={`${workflow.shortTitle} · ${project.name}`}
        contextType="task_detail"
        projectId={project.id}
        projectTaskId={readText((activeResultTask || statusContextTaskRow)?.id).trim() || null}
        currentUserId={session.userId}
        currentUsername={session.username}
        contextPayload={copilotContextPayload}
        onApplyPlanAction={applyTaskDetailCopilotAction}
        onSendAttachments={handleCopilotAttachments}
        onOpen={() => setCopilotOpen(true)}
        onClose={() => setCopilotOpen(false)}
      />
    ) : null}
    </>
  );
}
