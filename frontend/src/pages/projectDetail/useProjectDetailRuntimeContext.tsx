import { useEffect, useMemo, useRef } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import type { InputComponent } from '../../types/models';
import { enumerateLeadOptimizationMmp, getTaskStatus } from '../../api/backendApi';
import { useAuth } from '../../hooks/useAuth';
import {
  getProjectTaskById,
  insertProjectTask,
  listProjectTasksCompact,
  listProjectTasksForList,
  updateProject,
  updateProjectTask
} from '../../api/supabaseLite';
import { canEditProject } from '../../utils/accessControl';
import { saveProjectInputConfig } from '../../utils/projectInputs';
import { validateComponents } from '../../utils/inputValidation';
import { getWorkflowDefinition, isPredictionLikeWorkflowKey } from '../../utils/workflows';
import { createWorkflowSubmitters } from './workflowSubmitters';
import { useEntryRoutingResolution } from './useEntryRoutingResolution';
import { useProjectTaskActions } from './useProjectTaskActions';
import { useProjectAffinityWorkspace } from './useProjectAffinityWorkspace';
import {
  addTemplatesToTaskSnapshotComponents,
  buildAffinityUploadSnapshotComponents,
  isDraftTaskSnapshot,
  mergeTaskSnapshotIntoConfig,
  readLeadOptUploadsFromComponents,
} from './projectTaskSnapshot';
import {
  computeUseMsaFlag,
  createComputationFingerprint,
  createDraftFingerprint,
  createProteinTemplatesFingerprint,
  listIncompleteComponentOrders,
  nonEmptyComponents,
  normalizeConfigForBackend,
  sortProjectTasks
} from './projectDraftUtils';
import { inferTaskStateFromStatusPayload, readStatusText } from './projectMetrics';
import { useResultSnapshot } from './useResultSnapshot';
import { useProjectRunUiEffects } from './useProjectRunUiEffects';
import { useProjectRuntimeEffects } from './useProjectRuntimeEffects';
import { useProjectTaskStatusContext } from './useProjectTaskStatusContext';
import { useProjectWorkflowContext } from './useProjectWorkflowContext';
import { useConstraintTemplateContext } from './useConstraintTemplateContext';
import { useWorkspaceAffinitySelection } from './useWorkspaceAffinitySelection';
import { useProjectDraftSynchronizers } from './useProjectDraftSynchronizers';
import { useProjectWorkspaceRuntimeUi } from './useProjectWorkspaceRuntimeUi';
import { useProjectWorkspaceLoader } from './useProjectWorkspaceLoader';
import {
  showRunQueuedNotice as showRunQueuedNoticeControl,
  submitTaskByWorkflow
} from './runControls';
import { useProjectDirtyState } from './useProjectDirtyState';
import { useProjectConfidenceSignals } from './useProjectConfidenceSignals';
import { useComponentTypeBuckets } from './useComponentTypeBuckets';
import { useProjectDetailLocalState } from './useProjectDetailLocalState';
import { hasLeadOptPredictionRuntime, readLeadOptTaskSummary } from '../projectTasks/taskDataUtils';

function buildTaskRuntimeSignature(
  rows: Array<{
    id?: string | null;
    task_id?: string | null;
    task_state?: string | null;
    status_text?: string | null;
    error_text?: string | null;
    updated_at?: string | null;
    completed_at?: string | null;
    duration_seconds?: number | null;
    properties?: unknown;
    confidence?: unknown;
  }>
): string {
  return rows
    .map((row) => {
      const summary = readLeadOptTaskSummary(row as any);
      const properties = asObjectRecord((row as any).properties);
      const leadOptList = asObjectRecord(properties.lead_opt_list);
      const leadOptState = asObjectRecord(properties.lead_opt_state);
      const queryId = String(
        leadOptList.query_id ||
          asObjectRecord(leadOptList.query_result).query_id ||
          leadOptState.query_id ||
          ''
      ).trim();
      const enumeratedCount = Array.isArray(leadOptList.enumerated_candidates)
        ? leadOptList.enumerated_candidates.length
        : 0;
      const predictionCount = Object.keys(asObjectRecord(leadOptState.prediction_by_smiles)).length;
      const referencePredictionCount = Object.keys(asObjectRecord(leadOptState.reference_prediction_by_backend)).length;
      const leadOptSignature = [
        queryId,
        String(summary?.stage || '').trim().toLowerCase(),
        summary?.transformCount ?? '',
        summary?.candidateCount ?? '',
        summary?.predictionTotal ?? '',
        enumeratedCount,
        predictionCount,
        referencePredictionCount
      ].join('~');
      return `${String(row.id || '').trim()}|${String(row.task_id || '').trim()}|${String(row.task_state || '').trim()}|${String(row.status_text || '').trim()}|${String(row.error_text || '').trim()}|${String(row.updated_at || '').trim()}|${String(row.completed_at || '').trim()}|${Number.isFinite(Number(row.duration_seconds)) ? Number(row.duration_seconds) : ''}|${leadOptSignature}`;
    })
    .join('\n');
}

function isRuntimeTaskState(value: unknown): boolean {
  const token = String(value || '').trim().toUpperCase();
  return token === 'QUEUED' || token === 'RUNNING';
}

function buildRuntimePollingSignature(rows: Array<{
  id?: string | null;
  task_id?: string | null;
  task_state?: string | null;
  updated_at?: string | null;
  properties?: unknown;
  confidence?: unknown;
}>): string {
  return rows
    .filter((row) => {
      const hasRuntimeTaskState = isRuntimeTaskState(row.task_state) && String(row.task_id || '').trim();
      if (hasRuntimeTaskState) return true;
      return hasLeadOptPredictionRuntime(row as any);
    })
    .map((row) => {
      if (hasLeadOptPredictionRuntime(row as any)) {
        const summary = readLeadOptTaskSummary(row as any);
        const queued = Math.max(0, summary?.predictionQueued || 0);
        const running = Math.max(0, summary?.predictionRunning || 0);
        const stage = String(summary?.stage || '').trim().toLowerCase();
        return `leadopt|${String(row.id || '').trim()}|${queued}|${running}|${stage}|${String(row.updated_at || '').trim()}`;
      }
      const taskId = String(row.task_id || '').trim();
      const taskState = String(row.task_state || '').trim().toUpperCase();
      const updatedAt = String(row.updated_at || '').trim();
      return `${taskId}|${taskState}|${updatedAt}`;
    })
    .sort((a, b) => a.localeCompare(b))
    .join('\n');
}

const TASK_STATE_PRIORITY: Record<string, number> = {
  DRAFT: 0,
  QUEUED: 1,
  RUNNING: 2,
  SUCCESS: 3,
  FAILURE: 3,
  REVOKED: 3,
};
const RUNTIME_STATUS_LIGHT_POLL_MAX_TASKS = 3;
const LEADOPT_CANDIDATE_HYDRATION_RETRY_MS = 15000;
const LEADOPT_CANDIDATE_REPAIR_RETRY_MS = 60000;

function taskStatePriority(value: unknown): number {
  return TASK_STATE_PRIORITY[String(value || '').trim().toUpperCase()] ?? 0;
}

function deriveLeadOptRuntimeState(row: {
  task_state?: string | null;
  status_text?: string | null;
  error_text?: string | null;
  confidence?: unknown;
  properties?: unknown;
}): {
  task_state: string;
  status_text: string;
  error_text: string;
} | null {
  const properties = asObjectRecord(row.properties);
  const stateMeta = asObjectRecord(properties.lead_opt_state);
  const predictionMap = asObjectRecord(stateMeta.prediction_by_smiles);
  const predictionRecords = Object.values(predictionMap)
    .filter((item) => item && typeof item === 'object' && !Array.isArray(item))
    .map((item) => asObjectRecord(item));
  if (predictionRecords.length === 0) return null;

  let queued = 0;
  let running = 0;
  let success = 0;
  let failure = 0;
  for (const record of predictionRecords) {
    const state = String(record.state || '').trim().toUpperCase();
    if (state === 'RUNNING') running += 1;
    else if (state === 'SUCCESS') success += 1;
    else if (state === 'FAILURE') failure += 1;
    else queued += 1;
  }
  const unresolved = queued + running;
  const total = Math.max(0, predictionRecords.length);

  if (unresolved > 0) {
    return {
      task_state: running > 0 ? 'RUNNING' : 'QUEUED',
      status_text:
        running > 0
          ? `Scoring ${unresolved} running (${success}/${Math.max(1, total)} done)`
          : `Scoring ${unresolved} queued (${success}/${Math.max(1, total)} done)`,
      error_text: ''
    };
  }

  if (total > 0) {
    const allFailed = success === 0 && failure > 0;
    return {
      task_state: allFailed ? 'FAILURE' : 'SUCCESS',
      status_text: allFailed
        ? `Scoring complete (0/${Math.max(1, total)})`
        : `Scoring complete (${success}/${Math.max(1, total)})`,
      error_text: allFailed ? 'All candidate scoring jobs failed.' : ''
    };
  }

  return null;
}

function normalizeLeadOptRuntimeRow<
  T extends {
    task_state?: string | null;
    status_text?: string | null;
    error_text?: string | null;
    confidence?: unknown;
    properties?: unknown;
  }
>(row: T): T {
  const derived = deriveLeadOptRuntimeState(row);
  if (!derived) return row;
  const leadOptRuntime = hasLeadOptPredictionRuntime(row as unknown as any);
  if (leadOptRuntime) {
    const nextTaskState = String(derived.task_state || '').trim() || String(row.task_state || '').trim();
    const nextStatusText = String(derived.status_text || '').trim() || String(row.status_text || '').trim();
    const nextErrorText = String(derived.error_text || '').trim();
    if (
      String(row.task_state || '') === nextTaskState &&
      String(row.status_text || '') === nextStatusText &&
      String(row.error_text || '') === nextErrorText
    ) {
      return row;
    }
    return {
      ...row,
      task_state: nextTaskState,
      status_text: nextStatusText,
      error_text: nextErrorText
    };
  }
  const currentState = String(row.task_state || '').trim().toUpperCase();
  const nextState = String(derived.task_state || '').trim().toUpperCase();
  const currentPriority = taskStatePriority(currentState);
  const nextPriority = taskStatePriority(nextState);
  const shouldPromoteState = nextPriority > currentPriority || (currentState === 'QUEUED' && nextState === 'SUCCESS');
  const nextTaskState = shouldPromoteState ? derived.task_state : String(row.task_state || '').trim() || derived.task_state;
  const nextStatusText = String(derived.status_text || '').trim() || String(row.status_text || '').trim();
  const nextErrorText = String(derived.error_text || '').trim();
  if (
    String(row.task_state || '') === nextTaskState &&
    String(row.status_text || '') === nextStatusText &&
    String(row.error_text || '') === nextErrorText
  ) {
    return row;
  }
  return {
    ...row,
    task_state: nextTaskState,
    status_text: nextStatusText,
    error_text: nextErrorText
  };
}

function hasObjectContent(value: unknown): boolean {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value as Record<string, unknown>).length > 0);
}

function asObjectRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function readRecordUpdatedAt(value: unknown): number {
  const record = asObjectRecord(value);
  const raw = record.updatedAt ?? record.updated_at;
  const numeric = typeof raw === 'number' ? raw : typeof raw === 'string' ? Number(raw) : Number.NaN;
  return Number.isFinite(numeric) ? numeric : 0;
}

function mergeLeadOptPredictionMapsByKey(nextValue: unknown, prevValue: unknown): Record<string, unknown> {
  const next = asObjectRecord(nextValue);
  const prev = asObjectRecord(prevValue);
  if (Object.keys(next).length === 0 && Object.keys(prev).length === 0) return {};
  const merged: Record<string, unknown> = { ...prev };
  for (const [key, nextRecord] of Object.entries(next)) {
    const prevRecord = merged[key];
    if (!prevRecord) {
      merged[key] = nextRecord;
      continue;
    }
    const nextUpdatedAt = readRecordUpdatedAt(nextRecord);
    const prevUpdatedAt = readRecordUpdatedAt(prevRecord);
    merged[key] = nextUpdatedAt >= prevUpdatedAt ? nextRecord : prevRecord;
  }
  return merged;
}

function mergeLeadOptProperties(nextValue: unknown, prevValue: unknown): Record<string, unknown> | null {
  const next = asObjectRecord(nextValue);
  const prev = asObjectRecord(prevValue);
  const nextList = asObjectRecord(next.lead_opt_list);
  const prevList = asObjectRecord(prev.lead_opt_list);
  const nextState = asObjectRecord(next.lead_opt_state);
  const prevState = asObjectRecord(prev.lead_opt_state);
  if (
    Object.keys(nextList).length === 0 &&
    Object.keys(prevList).length === 0 &&
    Object.keys(nextState).length === 0 &&
    Object.keys(prevState).length === 0
  ) {
    return null;
  }
  return {
    ...prev,
    ...next,
    lead_opt_list: {
      ...prevList,
      ...nextList,
      query_result:
        Object.keys(asObjectRecord(nextList.query_result)).length > 0
          ? asObjectRecord(nextList.query_result)
          : asObjectRecord(prevList.query_result),
      ui_state: {},
      selection:
        Object.keys(asObjectRecord(nextList.selection)).length > 0
          ? asObjectRecord(nextList.selection)
          : asObjectRecord(prevList.selection),
      enumerated_candidates:
        Array.isArray(nextList.enumerated_candidates) && nextList.enumerated_candidates.length > 0
          ? nextList.enumerated_candidates
          : Array.isArray(prevList.enumerated_candidates)
            ? prevList.enumerated_candidates
            : []
    },
    lead_opt_state: {
      ...prevState,
      ...nextState,
      prediction_by_smiles: mergeLeadOptPredictionMapsByKey(
        nextState.prediction_by_smiles,
        prevState.prediction_by_smiles
      ),
      reference_prediction_by_backend: mergeLeadOptPredictionMapsByKey(
        nextState.reference_prediction_by_backend,
        prevState.reference_prediction_by_backend
      )
    }
  };
}

function mergePayloadFields<T extends object, U extends object>(next: T, prev: U): T {
  const nextAny = next as Record<string, unknown>;
  const prevAny = prev as Record<string, unknown>;
  const merged = { ...nextAny };
  if (Object.prototype.hasOwnProperty.call(nextAny, 'confidence') || Object.prototype.hasOwnProperty.call(prevAny, 'confidence')) {
    merged.confidence = hasObjectContent(nextAny.confidence) ? nextAny.confidence : prevAny.confidence;
  }
  if (Object.prototype.hasOwnProperty.call(nextAny, 'affinity') || Object.prototype.hasOwnProperty.call(prevAny, 'affinity')) {
    merged.affinity = hasObjectContent(nextAny.affinity) ? nextAny.affinity : prevAny.affinity;
  }
  if (Object.prototype.hasOwnProperty.call(nextAny, 'properties') || Object.prototype.hasOwnProperty.call(prevAny, 'properties')) {
    merged.properties =
      mergeLeadOptProperties(nextAny.properties, prevAny.properties) ||
      (hasObjectContent(nextAny.properties) ? nextAny.properties : prevAny.properties);
  }
  if (Object.prototype.hasOwnProperty.call(nextAny, 'components') || Object.prototype.hasOwnProperty.call(prevAny, 'components')) {
    const nextComponents = Array.isArray(nextAny.components) ? nextAny.components : [];
    merged.components = nextComponents.length > 0 ? nextComponents : prevAny.components;
  }
  if (Object.prototype.hasOwnProperty.call(nextAny, 'constraints') || Object.prototype.hasOwnProperty.call(prevAny, 'constraints')) {
    const nextConstraints = Array.isArray(nextAny.constraints) ? nextAny.constraints : [];
    merged.constraints = nextConstraints.length > 0 ? nextConstraints : prevAny.constraints;
  }
  return merged as T;
}

function mergeTaskRuntimeFields<
  T extends {
    task_id?: string | null;
    task_state?: string | null;
    status_text?: string | null;
    error_text?: string | null;
    completed_at?: string | null;
    duration_seconds?: number | null;
  },
  U extends {
  task_id?: string | null;
  task_state?: string | null;
  status_text?: string | null;
  error_text?: string | null;
  completed_at?: string | null;
  duration_seconds?: number | null;
}
>(next: T, prev: U): T {
  const nextTaskId = String(next.task_id || '').trim();
  const prevTaskId = String(prev.task_id || '').trim();
  if (!nextTaskId || !prevTaskId || nextTaskId !== prevTaskId) return mergePayloadFields(next, prev);
  // Lead-opt scoring can start after an MMP query row is already marked SUCCESS.
  // Allow QUEUED/RUNNING updates to replace stale SUCCESS for the same task row.
  if (hasLeadOptPredictionRuntime(next as unknown as any)) {
    const nextTaskState = String(next.task_state || '').trim().toUpperCase();
    const prevTaskState = String(prev.task_state || '').trim().toUpperCase();
    const isRuntimeState = nextTaskState === 'QUEUED' || nextTaskState === 'RUNNING';
    const prevIsTerminal = prevTaskState === 'SUCCESS' || prevTaskState === 'FAILURE' || prevTaskState === 'REVOKED';
    const nextLooksLikeScoring = String(next.status_text || '').trim().toLowerCase().includes('scoring');
    const shouldBlockTerminalDowngrade = prevIsTerminal && isRuntimeState && !nextLooksLikeScoring;
    const effectiveRuntimeState = isRuntimeState && !shouldBlockTerminalDowngrade;
    return mergePayloadFields({
      ...next,
      task_state: shouldBlockTerminalDowngrade ? prevTaskState : next.task_state,
      status_text:
        shouldBlockTerminalDowngrade
          ? String(prev.status_text || '').trim() || String(next.status_text || '').trim()
          : String(next.status_text || '').trim() || prev.status_text,
      error_text:
        shouldBlockTerminalDowngrade
          ? String(prev.error_text || '').trim()
          : String(next.error_text || '').trim(),
      completed_at: effectiveRuntimeState ? null : next.completed_at || prev.completed_at,
      duration_seconds: effectiveRuntimeState ? null : next.duration_seconds ?? prev.duration_seconds,
    }, prev);
  }
  const nextPriority = taskStatePriority(next.task_state);
  const prevPriority = taskStatePriority(prev.task_state);
  if (prevPriority < nextPriority) return mergePayloadFields(next, prev);
  if (prevPriority > nextPriority) {
    return mergePayloadFields({
      ...next,
      task_state: prev.task_state,
      status_text: prev.status_text,
      error_text: prev.error_text,
      completed_at: prev.completed_at || next.completed_at,
      duration_seconds: prev.duration_seconds ?? next.duration_seconds
    }, prev);
  }
  return mergePayloadFields({
    ...next,
    completed_at: next.completed_at || prev.completed_at,
    duration_seconds: next.duration_seconds ?? prev.duration_seconds,
    status_text: String(next.status_text || '').trim() || prev.status_text,
    error_text: String(next.error_text || '').trim() || prev.error_text
  }, prev);
}

function hasLeadOptResultSummaryPayload(row: { properties?: unknown; confidence?: unknown } | null | undefined): boolean {
  if (!row) return false;
  const summary = readLeadOptTaskSummary(row as any);
  if (summary) {
    if ((summary.candidateCount || 0) > 0) return true;
    if ((summary.transformCount || 0) > 0) return true;
    if ((summary.bucketCount || 0) > 0) return true;
    if ((summary.predictionTotal || 0) > 0) return true;
    if (String(summary.databaseId || '').trim()) return true;
    if (String(summary.databaseLabel || '').trim()) return true;
    if (String(summary.databaseSchema || '').trim()) return true;
  }
  const properties = asObjectRecord(row.properties);
  const listMeta = asObjectRecord(properties.lead_opt_list);
  const stateMeta = asObjectRecord(properties.lead_opt_state);
  const queryResult = asObjectRecord(listMeta.query_result);
  const queryId = String(listMeta.query_id || queryResult.query_id || stateMeta.query_id || '').trim();
  return Boolean(queryId);
}

function readLeadOptQueryIdFromRow(row: { properties?: unknown; confidence?: unknown } | null | undefined): string {
  if (!row) return '';
  const properties = asObjectRecord(row.properties);
  const listMeta = asObjectRecord(properties.lead_opt_list);
  const stateMeta = asObjectRecord(properties.lead_opt_state);
  const listQueryResult = asObjectRecord(listMeta.query_result);
  const confidence = asObjectRecord(row.confidence);
  const leadOptConfidence = asObjectRecord(confidence.lead_opt_mmp);
  const confidenceQueryResult = asObjectRecord(leadOptConfidence.query_result);
  return String(
    listMeta.query_id ||
      listQueryResult.query_id ||
      stateMeta.query_id ||
      leadOptConfidence.query_id ||
      confidenceQueryResult.query_id ||
      ''
  ).trim();
}

function readLeadOptRuntimeTaskIdFromRow(
  row: { task_id?: string | null; properties?: unknown; confidence?: unknown } | null | undefined
): string {
  if (!row) return '';
  const directTaskId = String(row.task_id || '').trim();
  if (directTaskId) return directTaskId;
  const properties = asObjectRecord(row.properties);
  const listMeta = asObjectRecord(properties.lead_opt_list);
  const stateMeta = asObjectRecord(properties.lead_opt_state);
  const listQueryResult = asObjectRecord(listMeta.query_result);
  const confidence = asObjectRecord(row.confidence);
  const leadOptConfidence = asObjectRecord(confidence.lead_opt_mmp);
  const confidenceQueryResult = asObjectRecord(leadOptConfidence.query_result);
  return String(
    listMeta.task_id ||
      stateMeta.task_id ||
      listQueryResult.task_id ||
      leadOptConfidence.task_id ||
      confidenceQueryResult.task_id ||
      ''
  ).trim();
}

function readLeadOptEnumeratedCandidateCount(row: { properties?: unknown; confidence?: unknown } | null | undefined): number {
  if (!row) return 0;
  const properties = asObjectRecord(row.properties);
  const listMeta = asObjectRecord(properties.lead_opt_list);
  if (Array.isArray(listMeta.enumerated_candidates)) return listMeta.enumerated_candidates.length;
  const confidence = asObjectRecord(row.confidence);
  const leadOptConfidence = asObjectRecord(confidence.lead_opt_mmp);
  if (Array.isArray(leadOptConfidence.enumerated_candidates)) return leadOptConfidence.enumerated_candidates.length;
  return 0;
}

function countLeadOptUploadPayloads(task: { components?: unknown } | null | undefined): number {
  const uploads = readLeadOptUploadsFromComponents(task?.components);
  let count = 0;
  if (uploads.target?.fileName && uploads.target?.content) count += 1;
  if (uploads.ligand?.fileName && uploads.ligand?.content) count += 1;
  return count;
}

function overlayRowsWithRuntimeStatus<
  T extends {
    task_id?: string | null;
    task_state?: string | null;
    status_text?: string | null;
    error_text?: string | null;
  }
>(rows: T[], statusByTaskId: Record<string, { task_id: string; state: string; info?: Record<string, unknown> }>): T[] {
  return rows.map((row) => {
    const taskId = String(row.task_id || '').trim();
    if (!taskId) return row;
    const runtimeStatus = statusByTaskId[taskId];
    if (!runtimeStatus) return row;

    const inferredState = inferTaskStateFromStatusPayload(runtimeStatus, row.task_state);
    const runtimeStatusText = String(readStatusText(runtimeStatus) || '').trim();
    const nextStatusText = runtimeStatusText || String(row.status_text || '');
    const nextErrorText =
      inferredState === 'FAILURE' ? runtimeStatusText || String(row.error_text || '') : '';

    if (
      String(row.task_state || '').toUpperCase() === inferredState &&
      String(row.status_text || '') === nextStatusText &&
      String(row.error_text || '') === nextErrorText
    ) {
      return row;
    }

    return {
      ...row,
      task_state: inferredState,
      status_text: nextStatusText,
      error_text: nextErrorText
    } as T;
  });
}

export function useProjectDetailRuntimeContext() {
  const { projectId = '' } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const { session } = useAuth();
  const hasExplicitWorkspaceQuery = useMemo(() => {
    const query = new URLSearchParams(location.search);
    if (query.get('new_task') === '1') return true;
    return query.has('tab') || query.has('task_row_id');
  }, [location.search]);
  const requestNewTask = useMemo(() => {
    const query = new URLSearchParams(location.search);
    return query.get('new_task') === '1';
  }, [location.search]);

  const local = useProjectDetailLocalState();
  const leadOptTabHydrationRef = useRef<Record<string, string>>({});
  const peptideResultHydrationRef = useRef<Record<string, string>>({});
  const {
    project,
    setProject,
    projectTasks,
    setProjectTasks,
    draft,
    setDraft,
    setLoading,
    saving,
    setSaving,
    submitting,
    setSubmitting,
    setError,
    setResultError,
    runRedirectTaskId,
    setRunRedirectTaskId,
    setRunSuccessNotice,
    setShowFloatingRunButton,
    structureText,
    setStructureText,
    setStructureFormat,
    structureTaskId,
    setStructureTaskId,
    statusInfo,
    setStatusInfo,
    nowTs,
    setNowTs,
    workspaceTab,
    setWorkspaceTab,
    savedDraftFingerprint,
    setSavedDraftFingerprint,
    savedComputationFingerprint,
    setSavedComputationFingerprint,
    savedTemplateFingerprint,
    setSavedTemplateFingerprint,
    runMenuOpen,
    setRunMenuOpen,
    proteinTemplates,
    setProteinTemplates,
    taskProteinTemplates,
    setTaskProteinTemplates,
    taskAffinityUploads,
    setTaskAffinityUploads,
    rememberTemplatesForTaskRow,
    rememberAffinityUploadsForTaskRow,
    setPickedResidue,
    activeConstraintId,
    setActiveConstraintId,
    selectedContactConstraintIds,
    setSelectedContactConstraintIds,
    selectedConstraintTemplateComponentId,
    setSelectedConstraintTemplateComponentId,
    constraintPickModeEnabled,
    constraintPickSlotRef,
    constraintSelectionAnchorRef,
    statusRefreshInFlightRef,
    submitInFlightRef,
    runRedirectTimerRef,
    runSuccessNoticeTimerRef,
    runActionRef,
    topRunButtonRef,
    activeComponentId,
    setActiveComponentId,
  } = local;
  const entryRoutingResolved = useEntryRoutingResolution({
    projectId,
    hasExplicitWorkspaceQuery,
    navigate,
    listProjectTasksCompact,
  });
  useEffect(() => {
    const tab = new URLSearchParams(location.search).get('tab');
    if (tab === 'inputs') {
      setWorkspaceTab('basics');
      return;
    }
    if (tab === 'results' || tab === 'basics' || tab === 'components' || tab === 'constraints') {
      setWorkspaceTab(tab);
    }
  }, [location.search, projectId]);

  const canEdit = useMemo(() => {
    if (!project || !session) return false;
    return canEditProject(project);
  }, [project, session]);
  const workflowKey = useMemo(() => getWorkflowDefinition(project?.task_type).key, [project?.task_type]);
  const isPredictionWorkflow = isPredictionLikeWorkflowKey(workflowKey);
  const isPeptideDesignWorkflow = workflowKey === 'peptide_design';
  const isAffinityWorkflow = workflowKey === 'affinity';
  const isLeadOptimizationWorkflow = workflowKey === 'lead_optimization';
  const runtimePollingSignature = useMemo(() => buildRuntimePollingSignature(projectTasks), [projectTasks]);
  const runtimePollingSummary = useMemo(() => {
    let hasRuntimeTasks = false;
    let hasRunning = false;
    let hasQueued = false;

    for (const row of projectTasks) {
      const runtimeTaskState = String(row.task_state || '').trim().toUpperCase();
      const hasRuntimeTaskState = isRuntimeTaskState(runtimeTaskState) && String(row.task_id || '').trim();
      if (hasRuntimeTaskState) {
        hasRuntimeTasks = true;
        if (runtimeTaskState === 'RUNNING') hasRunning = true;
        if (runtimeTaskState === 'QUEUED') hasQueued = true;
      }

      if (!hasLeadOptPredictionRuntime(row)) continue;
      hasRuntimeTasks = true;
      const summary = readLeadOptTaskSummary(row);
      const stage = String(summary?.stage || '').trim().toLowerCase();
      const queued = Math.max(0, summary?.predictionQueued || 0);
      const running = Math.max(0, summary?.predictionRunning || 0);
      if (running > 0 || stage === 'running' || stage === 'prediction_running') {
        hasRunning = true;
      } else if (queued > 0 || stage === 'queued' || stage === 'prediction_queued') {
        hasQueued = true;
      }
    }

    return {
      hasRuntimeTasks,
      hasRunning,
      hasQueued
    };
  }, [runtimePollingSignature, projectTasks]);
  const leadOptCandidateHydrationAtRef = useRef<Record<string, number>>({});
  const leadOptCandidateRepairAtRef = useRef<Record<string, number>>({});
  const runtimeTaskStatusCursorRef = useRef(0);
  const runtimeTerminalStatusByTaskIdRef = useRef<Record<string, { task_id: string; state: string; info?: Record<string, unknown> }>>({});

  useEffect(() => {
    const projectIdValue = String(project?.id || '').trim();
    if (!projectIdValue) return;
    const shouldHydrateLeadOptSnapshot = workflowKey === 'lead_optimization';
    if (!runtimePollingSummary.hasRuntimeTasks && !shouldHydrateLeadOptSnapshot) return;
    const requestedTaskRowId = String(new URLSearchParams(location.search).get('task_row_id') || '').trim();
    const activeTaskId = String(project?.task_id || '').trim();
    const resolveFocusedRow = (
      rows: Array<{ id?: string | null; task_id?: string | null; properties?: unknown; confidence?: unknown }>
    ) => {
      const requestedRow = requestedTaskRowId
        ? rows.find((row) => String(row.id || '').trim() === requestedTaskRowId) || null
        : null;
      const activeRow = activeTaskId
        ? rows.find((row) => String(row.task_id || '').trim() === activeTaskId) || null
        : null;
      return requestedRow || activeRow || null;
    };
    const resolveFocusedQueryId = (
      rows: Array<{ id?: string | null; task_id?: string | null; properties?: unknown; confidence?: unknown }>
    ): string => {
      return readLeadOptQueryIdFromRow(resolveFocusedRow(rows));
    };
    const focusedQueryId = resolveFocusedQueryId(projectTasks);
    const hasFocusedQueryCandidates = focusedQueryId
      ? projectTasks.some((row) => readLeadOptQueryIdFromRow(row) === focusedQueryId && readLeadOptEnumeratedCandidateCount(row) > 0)
      : projectTasks.some((row) => readLeadOptEnumeratedCandidateCount(row) > 0);
    const shouldRequestLeadOptCandidates =
      workflowKey === 'lead_optimization' &&
      workspaceTab === 'results' &&
      (!runtimePollingSummary.hasRuntimeTasks || !hasFocusedQueryCandidates);

    let cancelled = false;
    let inFlight = false;
    let timer: number | null = null;

    const computePollDelayMs = () => {
      const isLeadOptOrPeptide = workflowKey === 'lead_optimization' || workflowKey === 'peptide_design';
      if (workflowKey === 'lead_optimization' && !runtimePollingSummary.hasRuntimeTasks) {
        return typeof document !== 'undefined' && document.visibilityState !== 'visible' ? 40000 : 20000;
      }
      const baseDelayMs = runtimePollingSummary.hasRunning
        ? isLeadOptOrPeptide
          ? 4500
          : 4200
        : runtimePollingSummary.hasQueued
          ? isLeadOptOrPeptide
            ? 7500
            : 7000
          : 12000;
      if (typeof document !== 'undefined' && document.visibilityState !== 'visible') {
        return baseDelayMs * 2;
      }
      return baseDelayMs;
    };
    const scheduleNext = (
      hasLeadOptSummaryRows: boolean,
      hasLeadOptCandidates: boolean
    ) => {
      if (cancelled) return;
      if (workflowKey === 'lead_optimization' && !runtimePollingSummary.hasRuntimeTasks && workspaceTab !== 'results') return;
      if (
        workflowKey === 'lead_optimization' &&
        !runtimePollingSummary.hasRuntimeTasks &&
        workspaceTab === 'results' &&
        hasLeadOptSummaryRows &&
        hasLeadOptCandidates
      ) {
        return;
      }
      timer = window.setTimeout(() => {
        void refreshTaskRows();
      }, computePollDelayMs());
    };

    const refreshTaskRows = async () => {
      if (cancelled || inFlight) return;
      inFlight = true;
      let hasLeadOptSummaryRows = false;
      let hasLeadOptCandidates = false;
      try {
        const rowsRaw = await listProjectTasksForList(projectIdValue, {
          includeComponents: false,
          includeConfidence: false,
          includeProperties: false,
          includeLeadOptSummary: workflowKey === 'lead_optimization',
          includeLeadOptCandidates: false,
          taskRowIds:
            String(project?.access_scope || 'owner').trim() === 'task_share'
              ? project?.accessible_task_ids
              : undefined,
          accessScope: project?.access_scope || 'owner',
          accessLevel: project?.access_level || 'owner',
          editableTaskIds: project?.editable_task_ids || []
        });
        if (cancelled) return;
        const nextRows = sortProjectTasks(rowsRaw).map((row) => normalizeLeadOptRuntimeRow(row));
        const terminalStatusByTaskId = runtimeTerminalStatusByTaskIdRef.current;
        const cachedStatusByTaskId: Record<string, { task_id: string; state: string; info?: Record<string, unknown> }> = {};
        for (const row of nextRows) {
          const taskId = String(row.task_id || '').trim();
          if (!taskId) continue;
          const cached = terminalStatusByTaskId[taskId];
          if (!cached) continue;
          cachedStatusByTaskId[taskId] = cached;
        }
        let runtimeEnhancedRows =
          Object.keys(cachedStatusByTaskId).length > 0
            ? overlayRowsWithRuntimeStatus(nextRows, cachedStatusByTaskId).map((row) => normalizeLeadOptRuntimeRow(row))
            : nextRows;
        const runtimeRowByTaskId = new Map(
          runtimeEnhancedRows
            .map((row) => [String(row.task_id || '').trim(), row] as const)
            .filter(([taskId]) => Boolean(taskId))
        );

        const runtimeTaskIds = Array.from(
          new Set(
            runtimeEnhancedRows
              .map((row) => ({
                taskId: String(row.task_id || '').trim(),
                taskState: String(row.task_state || '').trim().toUpperCase()
              }))
              .filter(
                (row) =>
                  row.taskId &&
                  (row.taskState === 'QUEUED' || row.taskState === 'RUNNING') &&
                  !terminalStatusByTaskId[row.taskId]
              )
              .map((row) => row.taskId)
          )
        );

        if (runtimeTaskIds.length > 0) {
          try {
            const pollSize = Math.min(RUNTIME_STATUS_LIGHT_POLL_MAX_TASKS, runtimeTaskIds.length);
            const startCursor = ((runtimeTaskStatusCursorRef.current % runtimeTaskIds.length) + runtimeTaskIds.length) % runtimeTaskIds.length;
            const taskIdsForPoll: string[] = [];
            for (let i = 0; i < pollSize; i += 1) {
              taskIdsForPoll.push(runtimeTaskIds[(startCursor + i) % runtimeTaskIds.length]);
            }
            runtimeTaskStatusCursorRef.current = (startCursor + pollSize) % runtimeTaskIds.length;
            const statusEntries = await Promise.all(
              taskIdsForPoll.map(async (taskId) => {
                try {
                  const status = await getTaskStatus(taskId);
                  return [taskId, status] as const;
                } catch {
                  return null;
                }
              })
            );
            const statusByTaskId: Record<string, { task_id: string; state: string; info?: Record<string, unknown> }> = {};
            for (const entry of statusEntries) {
              if (!entry) continue;
              const [taskId, status] = entry;
              const inferred = inferTaskStateFromStatusPayload(status);
              if (inferred === 'SUCCESS' || inferred === 'FAILURE' || inferred === 'REVOKED') {
                runtimeTerminalStatusByTaskIdRef.current[taskId] = status;
                const runtimeRow = runtimeRowByTaskId.get(taskId);
                const runtimeRowId = String(runtimeRow?.id || '').trim();
                const runtimeRowState = String(runtimeRow?.task_state || '').trim().toUpperCase();
                const shouldPersistRuntimeTerminal =
                  runtimeRowId &&
                  (runtimeRowState === 'QUEUED' || runtimeRowState === 'RUNNING');
                if (shouldPersistRuntimeTerminal) {
                  const runtimeStatusText = String(readStatusText(status) || '').trim();
                  try {
                    await updateProjectTask(
                      runtimeRowId,
                      {
                        task_state: inferred,
                        status_text:
                          runtimeStatusText ||
                          (inferred === 'SUCCESS' ? 'Task completed.' : 'Task unavailable or expired.'),
                        error_text:
                          inferred === 'FAILURE'
                            ? runtimeStatusText || 'Task unavailable or expired.'
                            : ''
                      },
                      { minimalReturn: true }
                    );
                  } catch {
                    // Keep runtime overlay in memory even if persistence is temporarily unavailable.
                  }
                }
              }
              statusByTaskId[taskId] = status;
            }
            if (!cancelled && Object.keys(statusByTaskId).length > 0) {
              runtimeEnhancedRows = overlayRowsWithRuntimeStatus(nextRows, {
                ...cachedStatusByTaskId,
                ...statusByTaskId
              }).map((row) => normalizeLeadOptRuntimeRow(row));
            }
          } catch {
            // Keep DB snapshot if runtime overlay fails.
          }
        }

        let rowsForUi = runtimeEnhancedRows;
        if (workflowKey === 'lead_optimization' && workspaceTab === 'results') {
          hasLeadOptSummaryRows = rowsForUi.some((row) => hasLeadOptResultSummaryPayload(row));
          const focusedQueryIdInRows = resolveFocusedQueryId(rowsForUi);
          const focusedRow = resolveFocusedRow(rowsForUi);
          const focusedRowId = String(focusedRow?.id || '').trim();
          const focusedRuntimeTaskId = readLeadOptRuntimeTaskIdFromRow(focusedRow);
          hasLeadOptCandidates = focusedQueryIdInRows
            ? rowsForUi.some(
                (row) => readLeadOptQueryIdFromRow(row) === focusedQueryIdInRows && readLeadOptEnumeratedCandidateCount(row) > 0
              )
            : rowsForUi.some((row) => readLeadOptEnumeratedCandidateCount(row) > 0);
          if (
            shouldRequestLeadOptCandidates &&
            hasLeadOptSummaryRows &&
            !hasLeadOptCandidates &&
            focusedRowId
          ) {
            const lastAt = Number(leadOptCandidateHydrationAtRef.current[focusedRowId] || 0);
            const nowTs = Date.now();
            if (!Number.isFinite(lastAt) || nowTs - lastAt >= LEADOPT_CANDIDATE_HYDRATION_RETRY_MS) {
              leadOptCandidateHydrationAtRef.current[focusedRowId] = nowTs;
              try {
                const detailRow = await getProjectTaskById(focusedRowId, {
                  includeComponents: false,
                  includeConstraints: false,
                  includeProperties: false,
                  includeLeadOptSummary: true,
                  includeLeadOptCandidates: true,
                  includeConfidence: false,
                  includeAffinity: false,
                  includeProteinSequence: false
                });
                if (!cancelled && detailRow) {
                  const normalizedDetailRow = normalizeLeadOptRuntimeRow(detailRow);
                  rowsForUi = rowsForUi.map((row) =>
                    String(row.id || '').trim() === focusedRowId ? mergeTaskRuntimeFields(normalizedDetailRow, row) : row
                  );
                  hasLeadOptCandidates = focusedQueryIdInRows
                    ? rowsForUi.some(
                        (row) =>
                          readLeadOptQueryIdFromRow(row) === focusedQueryIdInRows && readLeadOptEnumeratedCandidateCount(row) > 0
                      )
                    : rowsForUi.some((row) => readLeadOptEnumeratedCandidateCount(row) > 0);
                }
              } catch {
                // Keep lightweight summary rows and retry with cooldown.
              }
            }
          }
          if (
            shouldRequestLeadOptCandidates &&
            hasLeadOptSummaryRows &&
            !hasLeadOptCandidates &&
            focusedRowId &&
            focusedQueryIdInRows
          ) {
            const repairKey = `${focusedRowId}|${focusedQueryIdInRows}`;
            const lastRepairAt = Number(leadOptCandidateRepairAtRef.current[repairKey] || 0);
            const nowTs = Date.now();
            if (!Number.isFinite(lastRepairAt) || nowTs - lastRepairAt >= LEADOPT_CANDIDATE_REPAIR_RETRY_MS) {
              leadOptCandidateRepairAtRef.current[repairKey] = nowTs;
              try {
                const enumerate = await enumerateLeadOptimizationMmp({
                  query_id: focusedQueryIdInRows,
                  task_id: focusedRuntimeTaskId,
                  property_constraints: {},
                  max_candidates: 360,
                  compact: true
                });
                const repairedCandidates = Array.isArray(enumerate.candidates)
                  ? enumerate.candidates.filter((item) => item && typeof item === 'object' && !Array.isArray(item))
                  : [];
                if (repairedCandidates.length > 0) {
                  const targetRow = rowsForUi.find((row) => String(row.id || '').trim() === focusedRowId) || null;
                  if (targetRow) {
                    const rowProperties = asObjectRecord(targetRow.properties);
                    const rowLeadOptList = asObjectRecord(rowProperties.lead_opt_list);
                    const rowLeadOptState = asObjectRecord(rowProperties.lead_opt_state);
                    const nextProperties = {
                      ...rowProperties,
                      lead_opt_list: {
                        ...rowLeadOptList,
                        query_id:
                          String(rowLeadOptList.query_id || focusedQueryIdInRows).trim() || focusedQueryIdInRows,
                        candidate_count: Math.max(
                          Number(rowLeadOptList.candidate_count || 0) || 0,
                          repairedCandidates.length
                        ),
                        enumerated_candidates: repairedCandidates
                      },
                      lead_opt_state: {
                        ...rowLeadOptState,
                        query_id:
                          String(rowLeadOptState.query_id || focusedQueryIdInRows).trim() || focusedQueryIdInRows
                      }
                    } as Record<string, unknown>;
                    try {
                      await updateProjectTask(
                        focusedRowId,
                        {
                          properties: nextProperties as any
                        },
                        { minimalReturn: true }
                      );
                    } catch {
                      // Keep repaired candidates in-memory even if persistence fails.
                    }
                    rowsForUi = rowsForUi.map((row) =>
                      String(row.id || '').trim() === focusedRowId
                        ? ({ ...row, properties: nextProperties as any } as any)
                        : row
                    );
                    hasLeadOptCandidates = true;
                  }
                }
              } catch {
                // Keep lightweight snapshot when backend query cache is unavailable/expired.
              }
            }
          }
        }

        setProjectTasks((prev) => {
          const prevById = new Map(prev.map((item) => [item.id, item]));
          const mergedRows = rowsForUi.map((row) => {
            const prevRow = prevById.get(row.id);
            if (!prevRow) return row;
            return mergeTaskRuntimeFields(row, prevRow);
          });
          return buildTaskRuntimeSignature(prev) === buildTaskRuntimeSignature(mergedRows) ? prev : mergedRows;
        });

        const activeProjectTaskId = String(project?.task_id || '').trim();
        if (!activeProjectTaskId) return;
        const activeRow = rowsForUi.find((row) => String(row.task_id || '').trim() === activeProjectTaskId) || null;
        if (!activeRow) return;
        setProject((prev) => {
          if (!prev) return prev;
          const mergedActiveRow = mergeTaskRuntimeFields(activeRow, prev);
          const rawTaskState = String(mergedActiveRow.task_state || '').trim().toUpperCase();
          const nextTaskState = (
            rawTaskState === 'QUEUED' ||
            rawTaskState === 'RUNNING' ||
            rawTaskState === 'SUCCESS' ||
            rawTaskState === 'FAILURE' ||
            rawTaskState === 'REVOKED' ||
            rawTaskState === 'DRAFT'
              ? rawTaskState
              : prev.task_state
          ) as typeof prev.task_state;
          const nextStatusText = String(mergedActiveRow.status_text || '').trim();
          const nextErrorText = String(mergedActiveRow.error_text || '').trim();
          const nextCompletedAt = mergedActiveRow.completed_at || null;
          const nextDurationCandidate = Number(mergedActiveRow.duration_seconds);
          const nextDurationSeconds = Number.isFinite(nextDurationCandidate) ? nextDurationCandidate : null;
          if (
            prev.task_state === nextTaskState &&
            String(prev.status_text || '') === nextStatusText &&
            String(prev.error_text || '') === nextErrorText &&
            (prev.completed_at || null) === nextCompletedAt &&
            (prev.duration_seconds ?? null) === nextDurationSeconds
          ) {
            return prev;
          }
          return {
            ...prev,
            task_state: nextTaskState,
            status_text: nextStatusText,
            error_text: nextErrorText,
            completed_at: nextCompletedAt,
            duration_seconds: nextDurationSeconds
          };
        });
      } catch {
        // keep local state and retry on next cycle
      } finally {
        inFlight = false;
        scheduleNext(hasLeadOptSummaryRows, hasLeadOptCandidates);
      }
    };

    void refreshTaskRows();

    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [
    project?.access_level,
    project?.access_scope,
    project?.accessible_task_ids,
    project?.editable_task_ids,
    location.search,
    project?.id,
    project?.task_id,
    runtimePollingSignature,
    runtimePollingSummary.hasQueued,
    runtimePollingSummary.hasRunning,
    runtimePollingSummary.hasRuntimeTasks,
    setProject,
    setProjectTasks,
    workspaceTab,
    workflowKey
  ]);

  useEffect(() => {
    if (workflowKey !== 'lead_optimization') return;
    if (workspaceTab !== 'constraints' && workspaceTab !== 'components') return;
    if (!draft) return;

    const needsConstraints = workspaceTab === 'constraints' && (draft.inputConfig.constraints?.length || 0) === 0;
    const needsLeadOptUploads =
      workspaceTab === 'components' && countLeadOptUploadPayloads({ components: draft.inputConfig.components }) === 0;
    if (!needsConstraints && !needsLeadOptUploads) return;

    const query = new URLSearchParams(location.search);
    const requestedTaskRowId = String(query.get('task_row_id') || '').trim();
    const activeTaskId = String(project?.task_id || '').trim();
    const sourceRow =
      (requestedTaskRowId
        ? projectTasks.find((row) => String(row.id || '').trim() === requestedTaskRowId)
        : undefined) ||
      (activeTaskId ? projectTasks.find((row) => String(row.task_id || '').trim() === activeTaskId) : undefined) ||
      null;
    const sourceRowId = String(sourceRow?.id || '').trim();
    if (!sourceRowId) return;
    const marker = `${workspaceTab}|${sourceRowId}|${String(sourceRow?.updated_at || '').trim()}`;
    if (leadOptTabHydrationRef.current[sourceRowId] === marker) return;

    let cancelled = false;
    void (async () => {
      try {
        const detailRow = await getProjectTaskById(sourceRowId, {
          includeComponents: true,
          includeConstraints: true,
          includeProperties: true,
          includeLeadOptSummary: true,
          includeLeadOptCandidates: false,
          includeConfidence: false,
          includeAffinity: false,
          includeProteinSequence: true
        });
        if (cancelled || !detailRow) return;
        leadOptTabHydrationRef.current[sourceRowId] = marker;
        setProjectTasks((prev) =>
          prev.map((row) =>
            String(row.id || '').trim() === sourceRowId ? mergeTaskRuntimeFields(detailRow, row) : row
          )
        );
        setDraft((prev) => {
          if (!prev) return prev;
          const mergedConfig = normalizeConfigForBackend(
            mergeTaskSnapshotIntoConfig(prev.inputConfig, detailRow),
            prev.backend
          );
          return {
            ...prev,
            taskName: String(detailRow.name || prev.taskName || '').trim(),
            taskSummary: String(detailRow.summary || prev.taskSummary || '').trim(),
            inputConfig: mergedConfig
          };
        });
      } catch {
        // Keep existing editor state if on-demand task hydration fails.
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [
    draft,
    location.search,
    project?.task_id,
    projectTasks,
    setDraft,
    setProjectTasks,
    workflowKey,
    workspaceTab
  ]);

  const {
    requestedStatusTaskRow,
    activeStatusTaskRow,
    statusContextTaskRow,
    displayTaskState,
    progressPercent,
    waitingSeconds,
    isActiveRuntime,
    totalRuntimeSeconds
  } = useProjectTaskStatusContext({
    project,
    projectTasks,
    locationSearch: location.search,
    statusInfo,
    nowTs
  });

  useEffect(() => {
    const templateContextTask = requestedStatusTaskRow || activeStatusTaskRow;
    if (!templateContextTask || !isDraftTaskSnapshot(templateContextTask)) return;
    rememberTemplatesForTaskRow(templateContextTask.id, proteinTemplates);
  }, [requestedStatusTaskRow, activeStatusTaskRow, proteinTemplates, rememberTemplatesForTaskRow]);

  const {
    normalizedDraftComponents,
    leadOptPrimary,
    leadOptChainContext,
    componentCompletion,
    hasIncompleteComponents,
    allowedConstraintTypes,
    isBondOnlyBackend
  } = useProjectWorkflowContext({
    draft,
    fallbackBackend: project?.backend || 'boltz',
    isPeptideDesignWorkflow
  });

  const componentTypeBuckets = useComponentTypeBuckets(normalizedDraftComponents);

  const constraintCount = draft?.inputConfig.constraints.length || 0;
  const {
    runtimeResultTask,
    activeResultTask,
    affinityUploadScopeTaskRowId,
    resultChainIds,
    resultChainShortLabelById,
    selectedResultTargetChainId,
    selectedResultLigandChainId,
    selectedResultLigandComponent,
    selectedResultLigandSequence,
    overviewPrimaryLigand,
    snapshotConfidence,
    resultChainConsistencyWarning,
    snapshotAffinity,
    snapshotLigandAtomPlddts,
    snapshotLigandResiduePlddts,
    snapshotLigandMeanPlddt,
    snapshotSelectedLigandChainPlddt,
    snapshotPlddt,
    snapshotSelectedPairIptm,
    snapshotIptm,
    snapshotBindingProbability,
    snapshotBindingStd,
    snapshotIc50Um,
    snapshotIc50Error,
    snapshotPlddtTone,
    snapshotIptmTone,
    snapshotIc50Tone,
    snapshotBindingTone,
  } = useResultSnapshot({
    project,
    projectTasks,
    draftProperties: draft?.inputConfig.properties,
    statusContextTaskRow,
    requestedStatusTaskRow,
    normalizedDraftComponents,
    workflowKey,
    isDraftTaskSnapshot: (task) => isDraftTaskSnapshot(task ?? null),
  });

  useEffect(() => {
    if (workflowKey !== 'peptide_design') return;
    if (workspaceTab !== 'results') return;

    const sourceRow = requestedStatusTaskRow || statusContextTaskRow || activeResultTask || null;
    const sourceRowId = String(sourceRow?.id || '').trim();
    if (!sourceRowId || sourceRowId.startsWith('local-')) return;

    const sourceTaskState = String(sourceRow?.task_state || '').trim().toUpperCase();
    if (sourceTaskState !== 'SUCCESS') return;

    const missingConfidence = !hasObjectContent(sourceRow?.confidence);
    const missingAffinity = !hasObjectContent(sourceRow?.affinity);
    if (!missingConfidence && !missingAffinity) return;

    const marker = [
      sourceRowId,
      String(sourceRow?.updated_at || '').trim(),
      missingConfidence ? 'confidence' : '',
      missingAffinity ? 'affinity' : ''
    ].join('|');
    if (peptideResultHydrationRef.current[sourceRowId] === marker) return;

    let cancelled = false;
    void (async () => {
      try {
        const detailRow = await getProjectTaskById(sourceRowId, {
          includeComponents: false,
          includeConstraints: false,
          includeProperties: true,
          includeConfidence: true,
          includeAffinity: true,
          includeProteinSequence: false
        });
        if (cancelled || !detailRow) return;
        if (!hasObjectContent(detailRow.confidence) && !hasObjectContent(detailRow.affinity)) return;
        peptideResultHydrationRef.current[sourceRowId] = marker;
        setProjectTasks((prev) =>
          prev.map((row) =>
            String(row.id || '').trim() === sourceRowId ? mergeTaskRuntimeFields(detailRow, row) : row
          )
        );
      } catch {
        // Keep current snapshot; a later row update can retry hydration.
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [
    activeResultTask,
    getProjectTaskById,
    requestedStatusTaskRow,
    setProjectTasks,
    statusContextTaskRow,
    workflowKey,
    workspaceTab
  ]);
  const leadOptPersistedUploads = useMemo(() => {
    const draftUploadSource =
      normalizedDraftComponents.length > 0
        ? ({ components: normalizedDraftComponents } as { components: unknown })
        : null;
    const sourceCandidates = [
      requestedStatusTaskRow,
      statusContextTaskRow,
      activeResultTask,
      draftUploadSource
    ];
    const hasText = (value: unknown) => String(value || '').trim().length > 0;
    let sourceTask: { components?: unknown } | null = null;
    let bestScore = -1;
    for (const candidate of sourceCandidates) {
      const score = countLeadOptUploadPayloads(candidate);
      if (score <= bestScore) continue;
      sourceTask = candidate;
      bestScore = score;
      if (score >= 2) break;
    }
    if (!sourceTask) {
      return { target: null, ligand: null };
    }
    const uploads = readLeadOptUploadsFromComponents(sourceTask.components);
    return {
      target:
        hasText(uploads.target?.fileName) && hasText(uploads.target?.content)
          ? { ...uploads.target! }
          : null,
      ligand:
        hasText(uploads.ligand?.fileName) && hasText(uploads.ligand?.content)
          ? { ...uploads.ligand! }
          : null
    };
  }, [activeResultTask, normalizedDraftComponents, requestedStatusTaskRow, statusContextTaskRow]);
  const activeConstraintIndex = useMemo(() => {
    if (!draft || !activeConstraintId) return -1;
    return draft.inputConfig.constraints.findIndex((item) => item.id === activeConstraintId);
  }, [draft, activeConstraintId]);
  const selectedContactConstraintIdSet = useMemo(() => {
    return new Set(selectedContactConstraintIds);
  }, [selectedContactConstraintIds]);

  const {
    activeChainInfos,
    chainInfoById,
    ligandChainOptions,
    workspaceTargetOptions,
    selectedWorkspaceTarget,
    workspaceLigandSelectableOptions,
    selectedWorkspaceLigand,
    canEnableAffinityFromWorkspace,
    affinityEnableDisabledReason,
  } = useWorkspaceAffinitySelection({
    normalizedDraftComponents,
    draftProperties: draft?.inputConfig.properties,
    isPeptideDesignWorkflow,
  });
  const {
    constraintTemplateOptions,
    selectedTemplatePreview,
    selectedTemplateResidueIndexMap,
    resolveTemplateComponentIdForConstraint,
    constraintViewerHighlightResidues,
    constraintViewerActiveResidue
  } = useConstraintTemplateContext({
    draft,
    proteinTemplates,
    selectedConstraintTemplateComponentId,
    setSelectedConstraintTemplateComponentId,
    activeConstraintId,
    selectedContactConstraintIds,
    chainInfoById,
    activeChainInfos
  });

  useProjectDraftSynchronizers({
    draft,
    setDraft,
    proteinTemplates,
    setProteinTemplates,
    activeConstraintId,
    setActiveConstraintId,
    selectedContactConstraintIds,
    setSelectedContactConstraintIds,
    constraintSelectionAnchorRef,
    constraintPickModeEnabled,
    constraintPickSlotRef,
    activeComponentId,
    setActiveComponentId,
    workflowKey,
    isPeptideDesignWorkflow,
    selectedWorkspaceLigandChainId: selectedWorkspaceLigand.chainId,
    selectedWorkspaceTargetChainId: selectedWorkspaceTarget.chainId,
    canEnableAffinityFromWorkspace,
  });

  const loadProject = useProjectWorkspaceLoader({
    entryRoutingResolved,
    projectId,
    locationSearch: location.search,
    requestNewTask,
    sessionUserId: session?.userId,
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
  });

  useProjectWorkspaceRuntimeUi({
    project,
    workspaceTab,
    setWorkspaceTab,
    setNowTs,
    proteinTemplates,
    taskProteinTemplates,
    taskAffinityUploads,
    activeConstraintId,
    selectedConstraintTemplateComponentId,
  });

  const {
    targetFile: affinityTargetFile,
    ligandFile: affinityLigandFile,
    ligandSmiles: affinityLigandSmiles,
    targetChainIds: affinityTargetChainIds,
    ligandChainId: affinityLigandChainId,
    preview: affinityPreview,
    previewTargetStructureText: affinityPreviewTargetStructureText,
    previewTargetStructureFormat: affinityPreviewTargetStructureFormat,
    previewLigandStructureText: affinityPreviewLigandStructureText,
    previewLigandStructureFormat: affinityPreviewLigandStructureFormat,
    previewLoading: affinityPreviewLoading,
    previewError: affinityPreviewError,
    isPreviewCurrent: affinityPreviewCurrent,
    hasLigand: affinityHasLigand,
    supportsActivity: affinitySupportsActivity,
    confidenceOnly: affinityConfidenceOnly,
    confidenceOnlyLocked: affinityConfidenceOnlyLocked,
    persistedUploads: affinityCurrentUploads,
    onTargetFileChange: onAffinityTargetFileChange,
    onLigandFileChange: onAffinityLigandFileChange,
    onConfidenceOnlyChange: onAffinityConfidenceOnlyChange,
    setLigandSmiles: setAffinityLigandSmiles,
    onAffinityUseMsaChange
  } = useProjectAffinityWorkspace({
    isAffinityWorkflow,
    workspaceTab,
    projectId: project?.id || null,
    draft,
    setDraft,
    affinityUploadScopeTaskRowId,
    taskAffinityUploads,
    statusContextTaskRow,
    activeResultTask,
    computeUseMsaFlag,
    rememberAffinityUploadsForTaskRow
  });

  const { metadataOnlyDraftDirty, hasUnsavedChanges } = useProjectDirtyState({
    draft,
    proteinTemplates,
    savedDraftFingerprint,
    savedComputationFingerprint,
    savedTemplateFingerprint,
    createDraftFingerprint,
    createComputationFingerprint,
    createProteinTemplatesFingerprint
  });

  const {
    patch,
    patchTask,
    resolveEditableDraftTaskRowId,
    persistDraftTaskSnapshot,
    saveDraft,
    pullResultForViewer,
    refreshStatus
  } = useProjectTaskActions({
    project,
    projectTasks,
    draft,
    requestNewTask,
    locationSearch: location.search,
    workspaceTab,
    metadataOnlyDraftDirty,
    affinityLigandSmiles,
    affinityPreviewLigandSmiles: String(affinityPreview?.ligandSmiles || ''),
    affinityTargetFile,
    affinityLigandFile,
    affinityCurrentUploads,
    proteinTemplates,
    requestedStatusTaskRowId: requestedStatusTaskRow?.id || null,
    activeStatusTaskRowId: activeStatusTaskRow?.id || null,
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
    setDraft: (value) => setDraft(value),
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
  });

  const showRunQueuedNotice = (message: string) => {
    showRunQueuedNoticeControl({
      message,
      runSuccessNoticeTimerRef,
      setRunSuccessNotice
    });
  };

  const { submitAffinityTask, submitPredictionTask } = createWorkflowSubmitters({
    project,
    draft,
    isPeptideDesignWorkflow,
    workspaceTab,
    affinityTargetFile,
    affinityLigandFile,
    affinityPreviewLoading,
    affinityPreviewCurrent,
    affinityPreview,
    affinityPreviewError: String(affinityPreviewError || ''),
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
    computeUseMsaFlag,
    createDraftFingerprint,
    createComputationFingerprint,
    createProteinTemplatesFingerprint,
    buildAffinityUploadSnapshotComponents,
    addTemplatesToTaskSnapshotComponents,
    persistDraftTaskSnapshot,
    resolveEditableDraftTaskRowId,
    rememberAffinityUploadsForTaskRow,
    rememberTemplatesForTaskRow,
    patch,
    patchTask,
    updateProjectTask,
    sortProjectTasks,
    saveProjectInputConfig,
    listIncompleteComponentOrders: (components: InputComponent[]) =>
      listIncompleteComponentOrders(components, {
        ignoreEmptyLigand: isPeptideDesignWorkflow
      }),
    validateComponents
  });

  const submitTask = async () => {
    await submitTaskByWorkflow({
      project,
      draft,
      submitInFlightRef,
      workflowKey,
      getWorkflowDefinition,
      setError,
      submitAffinityTask,
      submitPredictionTask
    });
  };

  useProjectRuntimeEffects({
    projectTaskId: statusContextTaskRow?.task_id || project?.task_id || null,
    projectTaskState: displayTaskState || project?.task_state || null,
    projectTasksDependency: runtimePollingSignature,
    refreshStatus,
    statusContextTaskRow,
    runtimeResultTask,
    activeResultTask,
    structureTaskId,
    structureText,
    pullResultForViewer,
    isPeptideDesignWorkflow,
    isLeadOptimizationWorkflow,
    workspaceTab,
    activeConstraintId,
    selectedContactConstraintIdsLength: selectedContactConstraintIds.length,
    setActiveConstraintId,
    setSelectedContactConstraintIds,
    constraintSelectionAnchorRef
  });

  useProjectRunUiEffects({
    runRedirectTaskId,
    projectId: project?.id || null,
    navigate: (to: string) => navigate(to),
    runRedirectTimerRef,
    runSuccessNoticeTimerRef,
    runMenuOpen,
    hasUnsavedChanges,
    submitting,
    saving,
    setRunMenuOpen,
    runActionRef,
    isPredictionWorkflow,
    isAffinityWorkflow,
    isLeadOptimizationWorkflow,
    workspaceTab,
    topRunButtonRef,
    setShowFloatingRunButton
  });

  const { confidenceBackend, projectBackend, hasProtenixConfidenceSignals, hasAf3ConfidenceSignals } =
    useProjectConfidenceSignals({
      snapshotConfidence: snapshotConfidence || null,
      projectBackendValue: project?.backend || null,
      draft,
      setDraft
    });

  return {
    ...local,
    projectId,
    locationSearch: location.search,
    navigate,
    hasExplicitWorkspaceQuery,
    requestNewTask,
    entryRoutingResolved,
    canEdit,
    workflowKey,
    isPredictionWorkflow,
    isPeptideDesignWorkflow,
    isAffinityWorkflow,
    isLeadOptimizationWorkflow,
    requestedStatusTaskRow,
    activeStatusTaskRow,
    statusContextTaskRow,
    displayTaskState,
    progressPercent,
    waitingSeconds,
    isActiveRuntime,
    totalRuntimeSeconds,
    normalizedDraftComponents,
    leadOptPrimary,
    leadOptChainContext,
    leadOptPersistedUploads,
    componentCompletion,
    hasIncompleteComponents,
    allowedConstraintTypes,
    isBondOnlyBackend,
    componentTypeBuckets,
    constraintCount,
    runtimeResultTask,
    activeResultTask,
    affinityUploadScopeTaskRowId,
    resultChainIds,
    resultChainShortLabelById,
    selectedResultTargetChainId,
    selectedResultLigandChainId,
    selectedResultLigandComponent,
    selectedResultLigandSequence,
    overviewPrimaryLigand,
    snapshotConfidence,
    resultChainConsistencyWarning,
    snapshotAffinity,
    snapshotLigandAtomPlddts,
    snapshotLigandResiduePlddts,
    snapshotLigandMeanPlddt,
    snapshotSelectedLigandChainPlddt,
    snapshotPlddt,
    snapshotSelectedPairIptm,
    snapshotIptm,
    snapshotBindingProbability,
    snapshotBindingStd,
    snapshotIc50Um,
    snapshotIc50Error,
    snapshotPlddtTone,
    snapshotIptmTone,
    snapshotIc50Tone,
    snapshotBindingTone,
    activeConstraintIndex,
    selectedContactConstraintIdSet,
    activeChainInfos,
    chainInfoById,
    ligandChainOptions,
    workspaceTargetOptions,
    selectedWorkspaceTarget,
    workspaceLigandSelectableOptions,
    selectedWorkspaceLigand,
    canEnableAffinityFromWorkspace,
    affinityEnableDisabledReason,
    constraintTemplateOptions,
    selectedTemplatePreview,
    selectedTemplateResidueIndexMap,
    resolveTemplateComponentIdForConstraint,
    constraintViewerHighlightResidues,
    constraintViewerActiveResidue,
    loadProject,
    affinityTargetFile,
    affinityLigandFile,
    affinityLigandSmiles,
    affinityTargetChainIds,
    affinityLigandChainId,
    affinityPreview,
    affinityPreviewTargetStructureText,
    affinityPreviewTargetStructureFormat,
    affinityPreviewLigandStructureText,
    affinityPreviewLigandStructureFormat,
    affinityPreviewLoading,
    affinityPreviewError,
    affinityPreviewCurrent,
    affinityHasLigand,
    affinitySupportsActivity,
    affinityConfidenceOnly,
    affinityConfidenceOnlyLocked,
    affinityCurrentUploads,
    onAffinityTargetFileChange,
    onAffinityLigandFileChange,
    onAffinityConfidenceOnlyChange,
    setAffinityLigandSmiles,
    onAffinityUseMsaChange,
    metadataOnlyDraftDirty,
    hasUnsavedChanges,
    patch,
    patchTask,
    resolveEditableDraftTaskRowId,
    persistDraftTaskSnapshot,
    saveDraft,
    pullResultForViewer,
    refreshStatus,
    submitAffinityTask,
    submitPredictionTask,
    submitTask,
    confidenceBackend,
    projectBackend,
    hasProtenixConfidenceSignals,
    hasAf3ConfidenceSignals
  };
}
