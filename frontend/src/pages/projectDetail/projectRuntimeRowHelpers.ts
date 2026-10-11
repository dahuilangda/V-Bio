// Pure helpers behind useProjectDetailRuntimeContext: runtime-row overlays,
// option normalization/merging, lead-opt runtime state derivation and the
// polling signatures. No React imports — everything here is directly unit
// testable, and the hook file stays focused on hook wiring.
import type { AffinityScoringMode, ProjectTask } from '../../types/models';
import {
  inferTaskStateFromStatusPayload,
  readTaskRuntimeStatusText as readStatusText,
  taskStatePriority
} from '../../utils/taskRuntime';
import { buildTaskRuntimeFailureMessage } from '../../utils/taskRuntime';
import { hasLeadOptPredictionRuntime, readLeadOptTaskSummary } from '../projectTasks/taskDataUtils';
import { hasObjectContent, asRecord as asObjectRecord } from '../projectTasks/recordReaders';
import { mergeLeadOptPredictionMapsByKey } from '../../components/project/leadopt/hooks/leadOptPredictionHelpers';
import { mergeConfidencePreservingPeptideCandidates } from '../projectTasks/taskDataPeptide';
import { mergeTaskPropertiesPreservingInputOptions, readLeadOptUploadsFromComponents } from './projectTaskSnapshot';

export function normalizeAffinityMode(value: unknown): AffinityScoringMode {
  const normalized = String(value || '').trim().toLowerCase();
  if (normalized === 'score' || normalized === 'pose' || normalized === 'refine' || normalized === 'interface' || normalized === 'dock') {
    return normalized;
  }
  return 'dock';
}

export function buildTaskRuntimeSignature(
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

export function isRuntimeTaskState(value: unknown): boolean {
  const token = String(value || '').trim().toUpperCase();
  return token === 'QUEUED' || token === 'RUNNING';
}

export function buildRuntimePollingSignature(rows: Array<{
  id?: string | null;
  task_id?: string | null;
  task_state?: string | null;
  properties?: unknown;
  confidence?: unknown;
}>): string {
  // identity + runtime state only; updated_at excluded so progress-text
  // changes never re-arm the poller
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
        return `leadopt|${String(row.id || '').trim()}|${queued}|${running}|${stage}`;
      }
      const taskId = String(row.task_id || '').trim();
      const taskState = String(row.task_state || '').trim().toUpperCase();
      return `${taskId}|${taskState}`;
    })
    .sort((a, b) => a.localeCompare(b))
    .join('\n');
}

export const RUNTIME_STATUS_LIGHT_POLL_MAX_TASKS = 24;
export const LEADOPT_CANDIDATE_HYDRATION_RETRY_MS = 15000;

export type ProjectTaskDetailOptions = {
  includeComponents?: boolean;
  includeConstraints?: boolean;
  includeProperties?: boolean;
  includeLeadOptSummary?: boolean;
  includeLeadOptCandidates?: boolean;
  includeConfidence?: boolean;
  includeAffinity?: boolean;
  includeProteinSequence?: boolean;
};

export type NormalizedProjectTaskDetailOptions = Required<ProjectTaskDetailOptions>;

export type CachedProjectTaskDetail = {
  updatedAt: string;
  options: NormalizedProjectTaskDetailOptions;
  task: ProjectTask;
};

export function normalizeProjectTaskDetailOptions(options?: ProjectTaskDetailOptions): NormalizedProjectTaskDetailOptions {
  return {
    includeComponents: options?.includeComponents !== false,
    includeConstraints: options?.includeConstraints !== false,
    includeProperties: options?.includeProperties !== false,
    includeLeadOptSummary: options?.includeLeadOptSummary === true,
    includeLeadOptCandidates: options?.includeLeadOptCandidates === true,
    includeConfidence: options?.includeConfidence !== false,
    includeAffinity: options?.includeAffinity !== false,
    includeProteinSequence: options?.includeProteinSequence !== false,
  };
}

export function mergeProjectTaskDetailOptions(
  left?: ProjectTaskDetailOptions,
  right?: ProjectTaskDetailOptions
): NormalizedProjectTaskDetailOptions {
  const a = normalizeProjectTaskDetailOptions(left);
  const b = normalizeProjectTaskDetailOptions(right);
  return {
    includeComponents: a.includeComponents || b.includeComponents,
    includeConstraints: a.includeConstraints || b.includeConstraints,
    includeProperties: a.includeProperties || b.includeProperties,
    includeLeadOptSummary: a.includeLeadOptSummary || b.includeLeadOptSummary,
    includeLeadOptCandidates: a.includeLeadOptCandidates || b.includeLeadOptCandidates,
    includeConfidence: a.includeConfidence || b.includeConfidence,
    includeAffinity: a.includeAffinity || b.includeAffinity,
    includeProteinSequence: a.includeProteinSequence || b.includeProteinSequence,
  };
}

export function projectTaskDetailOptionsCover(
  available: NormalizedProjectTaskDetailOptions,
  requested?: ProjectTaskDetailOptions
): boolean {
  const need = normalizeProjectTaskDetailOptions(requested);
  return (
    (!need.includeComponents || available.includeComponents) &&
    (!need.includeConstraints || available.includeConstraints) &&
    (!need.includeProperties || available.includeProperties) &&
    (!need.includeLeadOptSummary || available.includeLeadOptSummary) &&
    (!need.includeLeadOptCandidates || available.includeLeadOptCandidates) &&
    (!need.includeConfidence || available.includeConfidence) &&
    (!need.includeAffinity || available.includeAffinity) &&
    (!need.includeProteinSequence || available.includeProteinSequence)
  );
}

export function deriveLeadOptRuntimeState(row: {
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

export function normalizeLeadOptRuntimeRow<
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


export function normalizeCustomResidueDefinition(value: unknown): { ccd: string; smiles: string; baseResidue?: string; label?: string } | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const raw = value as Record<string, unknown>;
  const ccd = String(raw.ccd || '').replace(/[^A-Za-z0-9_-]/g, '').toUpperCase().slice(0, 12);
  const smiles = String(raw.smiles || '').trim();
  if (!ccd || !smiles) return null;
  return {
    ccd,
    smiles,
    baseResidue: String(raw.baseResidue || '').trim().toUpperCase().slice(0, 1) || undefined,
    label: String(raw.label || '').trim().slice(0, 80) || undefined
  };
}

export function hasTaskInputSnapshotPayload(
  task: {
    protein_sequence?: string | null;
    ligand_smiles?: string | null;
    components?: unknown;
  } | null | undefined
): boolean {
  if (!task) return false;
  if (String(task.protein_sequence || '').trim()) return true;
  if (String(task.ligand_smiles || '').trim()) return true;
  return Array.isArray(task.components) && task.components.length > 0;
}


export function mergeLeadOptProperties(nextValue: unknown, prevValue: unknown): Record<string, unknown> | null {
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

export function mergePayloadFields<T extends object, U extends object>(next: T, prev: U): T {
  const nextAny = next as Record<string, unknown>;
  const prevAny = prev as Record<string, unknown>;
  const merged = { ...nextAny };
  const preserveNonEmptyScalarField = (key: string) => {
    if (!Object.prototype.hasOwnProperty.call(nextAny, key) && !Object.prototype.hasOwnProperty.call(prevAny, key)) {
      return;
    }
    const nextValue = nextAny[key];
    if (typeof nextValue === 'string') {
      merged[key] = nextValue.trim().length > 0 ? nextValue : prevAny[key];
      return;
    }
    if (nextValue !== undefined && nextValue !== null) {
      merged[key] = nextValue;
      return;
    }
    merged[key] = prevAny[key];
  };
  if (Object.prototype.hasOwnProperty.call(nextAny, 'confidence') || Object.prototype.hasOwnProperty.call(prevAny, 'confidence')) {
    merged.confidence = hasObjectContent(nextAny.confidence)
      ? mergeConfidencePreservingPeptideCandidates(nextAny.confidence, prevAny.confidence)
      : prevAny.confidence;
  }
  if (Object.prototype.hasOwnProperty.call(nextAny, 'affinity') || Object.prototype.hasOwnProperty.call(prevAny, 'affinity')) {
    merged.affinity = hasObjectContent(nextAny.affinity) ? nextAny.affinity : prevAny.affinity;
  }
  if (Object.prototype.hasOwnProperty.call(nextAny, 'properties') || Object.prototype.hasOwnProperty.call(prevAny, 'properties')) {
    merged.properties =
      mergeLeadOptProperties(nextAny.properties, prevAny.properties) ||
      mergeTaskPropertiesPreservingInputOptions(nextAny.properties, prevAny.properties);
  }
  if (Object.prototype.hasOwnProperty.call(nextAny, 'components') || Object.prototype.hasOwnProperty.call(prevAny, 'components')) {
    const nextComponents = Array.isArray(nextAny.components) ? nextAny.components : [];
    merged.components = nextComponents.length > 0 ? nextComponents : prevAny.components;
  }
  if (Object.prototype.hasOwnProperty.call(nextAny, 'constraints') || Object.prototype.hasOwnProperty.call(prevAny, 'constraints')) {
    const nextConstraints = Array.isArray(nextAny.constraints) ? nextAny.constraints : [];
    merged.constraints = nextConstraints.length > 0 ? nextConstraints : prevAny.constraints;
  }
  preserveNonEmptyScalarField('ligand_smiles');
  preserveNonEmptyScalarField('protein_sequence');
  preserveNonEmptyScalarField('structure_name');
  return merged as T;
}

export function mergeTaskRuntimeFields<
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
  // lead-opt scoring can start after the row is already SUCCESS; let QUEUED/RUNNING
  // replace the stale terminal state
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

export function hasLeadOptResultSummaryPayload(row: { properties?: unknown; confidence?: unknown } | null | undefined): boolean {
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

export function readLeadOptQueryIdFromRow(row: { properties?: unknown; confidence?: unknown } | null | undefined): string {
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

export function readLeadOptEnumeratedCandidateCount(row: { properties?: unknown; confidence?: unknown } | null | undefined): number {
  if (!row) return 0;
  const properties = asObjectRecord(row.properties);
  const listMeta = asObjectRecord(properties.lead_opt_list);
  if (Array.isArray(listMeta.enumerated_candidates)) return listMeta.enumerated_candidates.length;
  const confidence = asObjectRecord(row.confidence);
  const leadOptConfidence = asObjectRecord(confidence.lead_opt_mmp);
  if (Array.isArray(leadOptConfidence.enumerated_candidates)) return leadOptConfidence.enumerated_candidates.length;
  return 0;
}

export function countLeadOptUploadPayloads(task: { components?: unknown } | null | undefined): number {
  const uploads = readLeadOptUploadsFromComponents(task?.components);
  let count = 0;
  if (uploads.target?.fileName && uploads.target?.content) count += 1;
  if (uploads.ligand?.fileName && uploads.ligand?.content) count += 1;
  return count;
}

export function overlayRowsWithRuntimeStatus<
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
    const runtimeFailureText =
      inferredState === 'FAILURE' ? buildTaskRuntimeFailureMessage(runtimeStatus, runtimeStatusText || 'Task failed.').trim() : '';
    const nextStatusText = inferredState === 'FAILURE' ? runtimeFailureText || runtimeStatusText || String(row.status_text || '') : runtimeStatusText || String(row.status_text || '');
    const nextErrorText = inferredState === 'FAILURE' ? runtimeFailureText || runtimeStatusText || String(row.error_text || '') : '';

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
