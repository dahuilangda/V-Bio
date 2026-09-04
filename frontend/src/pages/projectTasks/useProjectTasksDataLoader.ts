import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import type { Project, ProjectTask } from '../../types/models';
import {
  getProjectAccessInfo,
  getProjectById,
  listProjectTasksForList,
  sanitizeProjectForTaskShare
} from '../../api/supabaseLite';
import { normalizeWorkflowKey } from '../../utils/workflows';
import { mergeTaskPropertiesPreservingInputOptions } from '../projectDetail/projectTaskSnapshot';
import {
  hasLeadOptPredictionRuntime,
  isProjectTaskRow,
  sanitizeTaskRows,
  sortProjectTasks,
  type LoadTaskDataOptions,
} from './taskDataUtils';
import { hydrateTaskMetricsFromResultRows, syncInitialRuntimeTaskRows, syncRuntimeTaskRows } from './taskRowSync';

interface UseProjectTasksDataLoaderOptions {
  projectId: string;
  sessionUserId: string | null;
  workspaceView: 'tasks' | 'api';
  priorityTaskRowIds?: string[];
  /** True while a structure (SMILES/SMARTS) search is active — matches ligand data derived
   *  from row components, which lightweight tail rows lack, so they hydrate in the background. */
  structureSearchActive?: boolean;
}

interface UseProjectTasksDataLoaderResult {
  project: Project | null;
  tasks: ProjectTask[];
  loading: boolean;
  refreshing: boolean;
  error: string | null;
  /** False while the paginated background load is still filling the list. */
  allTasksLoaded: boolean;
  setProject: Dispatch<SetStateAction<Project | null>>;
  setTasks: Dispatch<SetStateAction<ProjectTask[]>>;
  setError: Dispatch<SetStateAction<string | null>>;
  loadData: (options?: LoadTaskDataOptions) => Promise<void>;
  /** Await the fully-loaded task list; reports merged row count per chunk. */
  ensureAllTasksLoaded: (onProgress?: (loaded: number) => void) => Promise<void>;
}

interface TaskListAccessContext {
  scope: 'owner' | 'project_share' | 'task_share';
  accessLevel: 'owner' | 'editor' | 'viewer';
  editableTaskIds: string[];
}

const TASK_STATE_PRIORITY: Record<string, number> = {
  DRAFT: 0,
  QUEUED: 1,
  RUNNING: 2,
  SUCCESS: 3,
  FAILURE: 3,
  REVOKED: 3,
};
const TASK_LIST_RUNTIME_CACHE_TTL_MS = 5000;
const LEGACY_TASK_LIST_RUNTIME_CACHE_KEY_PREFIX = 'vbio:project-tasks-runtime:';
const TASK_LIST_INITIAL_FETCH_LIMIT = 120;
const TASK_LIST_BACKGROUND_FETCH_CHUNK_SIZE = 240;
const TASK_LIST_BACKGROUND_FETCH_DELAY_MS = 80;
const TASK_LIST_RUNTIME_CACHE_MAX_ROWS = 180;
const TASK_LIST_RUNTIME_CACHE_MAX_ENTRIES = 8;

interface TaskListRuntimeCacheEntry {
  savedAt: number;
  project: Project;
  tasks: ProjectTask[];
}

const taskListRuntimeCache = new Map<string, TaskListRuntimeCacheEntry>();
let legacyRuntimeCacheCleanupDone = false;

function taskStatePriority(value: unknown): number {
  return TASK_STATE_PRIORITY[String(value || '').trim().toUpperCase()] ?? 0;
}

function hasObjectContent(value: unknown): boolean {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value as Record<string, unknown>).length > 0);
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function hasPeptideCandidateRows(value: unknown): boolean {
  const confidence = asRecord(value);
  if (Object.keys(confidence).length === 0) return false;
  const peptide = asRecord(confidence.peptide_design);
  const progress = asRecord(confidence.progress);
  const peptideProgress = asRecord(peptide.progress);
  const sources = [confidence, peptide, progress, peptideProgress];
  return sources.some(
    (source) =>
      (Array.isArray(source.best_sequences) && source.best_sequences.length > 0) ||
      (Array.isArray(source.current_best_sequences) && source.current_best_sequences.length > 0) ||
      (Array.isArray(source.candidates) && source.candidates.length > 0)
  );
}

function mergeConfidencePreservingPeptideCandidates(nextValue: unknown, prevValue: unknown): unknown {
  const next = asRecord(nextValue);
  const prev = asRecord(prevValue);
  if (Object.keys(next).length === 0) return prevValue;
  if (Object.keys(prev).length === 0) return nextValue;
  if (hasPeptideCandidateRows(prev) && !hasPeptideCandidateRows(next)) return prevValue;
  return nextValue;
}

function readRecordUpdatedAt(value: unknown): number {
  const record = asRecord(value);
  const raw = record.updatedAt ?? record.updated_at;
  const numeric = typeof raw === 'number' ? raw : typeof raw === 'string' ? Number(raw) : Number.NaN;
  return Number.isFinite(numeric) ? numeric : 0;
}

function mergeLeadOptPredictionMapsByKey(nextValue: unknown, prevValue: unknown): Record<string, unknown> {
  const next = asRecord(nextValue);
  const prev = asRecord(prevValue);
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

function mergeLeadOptProperties(nextValue: unknown, prevValue: unknown): ProjectTask['properties'] | null {
  const next = asRecord(nextValue);
  const prev = asRecord(prevValue);
  const nextList = asRecord(next.lead_opt_list);
  const prevList = asRecord(prev.lead_opt_list);
  const nextState = asRecord(next.lead_opt_state);
  const prevState = asRecord(prev.lead_opt_state);
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
      query_result: Object.keys(asRecord(nextList.query_result)).length > 0 ? asRecord(nextList.query_result) : asRecord(prevList.query_result),
      ui_state: {},
      selection: Object.keys(asRecord(nextList.selection)).length > 0 ? asRecord(nextList.selection) : asRecord(prevList.selection),
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
  } as unknown as ProjectTask['properties'];
}

function mergeTaskRuntimeFields(next: ProjectTask, prev: ProjectTask): ProjectTask {
  const nextTaskId = String(next.task_id || '').trim();
  const prevTaskId = String(prev.task_id || '').trim();
  if (!nextTaskId || !prevTaskId || nextTaskId !== prevTaskId) return next;
  const mergedLeadOptProperties = mergeLeadOptProperties(next.properties, prev.properties);
  if (hasLeadOptPredictionRuntime(next)) {
    const nextTaskState = String(next.task_state || '').trim().toUpperCase();
    const isRuntimeState = nextTaskState === 'QUEUED' || nextTaskState === 'RUNNING';
    return {
      ...next,
      confidence: hasObjectContent(next.confidence) ? mergeConfidencePreservingPeptideCandidates(next.confidence, prev.confidence) as ProjectTask['confidence'] : prev.confidence,
      affinity: hasObjectContent(next.affinity) ? next.affinity : prev.affinity,
      components: Array.isArray(next.components) && next.components.length > 0 ? next.components : prev.components,
      properties: mergedLeadOptProperties || mergeTaskPropertiesPreservingInputOptions(next.properties, prev.properties),
      completed_at: isRuntimeState ? null : next.completed_at || prev.completed_at,
      duration_seconds: isRuntimeState ? null : next.duration_seconds ?? prev.duration_seconds,
      status_text: String(next.status_text || '').trim() || prev.status_text,
      error_text: String(next.error_text || '').trim()
    };
  }
  const nextPriority = taskStatePriority(next.task_state);
  const prevPriority = taskStatePriority(prev.task_state);
  if (prevPriority < nextPriority) return next;
  if (prevPriority > nextPriority) {
    return {
      ...next,
      confidence: hasObjectContent(next.confidence) ? mergeConfidencePreservingPeptideCandidates(next.confidence, prev.confidence) as ProjectTask['confidence'] : prev.confidence,
      affinity: hasObjectContent(next.affinity) ? next.affinity : prev.affinity,
      components: Array.isArray(next.components) && next.components.length > 0 ? next.components : prev.components,
      properties: mergedLeadOptProperties || mergeTaskPropertiesPreservingInputOptions(next.properties, prev.properties),
      task_state: prev.task_state,
      status_text: prev.status_text,
      error_text: prev.error_text,
      completed_at: prev.completed_at || next.completed_at,
      duration_seconds:
        prev.duration_seconds ?? next.duration_seconds
    };
  }
  return {
    ...next,
    confidence: hasObjectContent(next.confidence) ? mergeConfidencePreservingPeptideCandidates(next.confidence, prev.confidence) as ProjectTask['confidence'] : prev.confidence,
    affinity: hasObjectContent(next.affinity) ? next.affinity : prev.affinity,
    components: Array.isArray(next.components) && next.components.length > 0 ? next.components : prev.components,
    properties: mergedLeadOptProperties || mergeTaskPropertiesPreservingInputOptions(next.properties, prev.properties),
    completed_at: next.completed_at || prev.completed_at,
    duration_seconds: next.duration_seconds ?? prev.duration_seconds,
    status_text: String(next.status_text || '').trim() || prev.status_text,
    error_text: String(next.error_text || '').trim() || prev.error_text
  };
}

function mergeTaskRowPages(nextRows: ProjectTask[], prevRows: ProjectTask[]): ProjectTask[] {
  const mergedById = new Map<string, ProjectTask>();
  for (const row of sanitizeTaskRows(prevRows)) {
    mergedById.set(row.id, row);
  }
  for (const row of sanitizeTaskRows(nextRows)) {
    const prev = mergedById.get(row.id);
    mergedById.set(row.id, prev ? mergeTaskRuntimeFields(row, prev) : row);
  }
  return sortProjectTasks(Array.from(mergedById.values()));
}

function pickRuntimeCacheTaskRows(taskRows: ProjectTask[]): ProjectTask[] {
  const rows = sanitizeTaskRows(taskRows);
  if (rows.length <= TASK_LIST_RUNTIME_CACHE_MAX_ROWS) return rows;
  const selected = new Map<string, ProjectTask>();
  for (const row of rows) {
    const state = String(row.task_state || '').trim().toUpperCase();
    if (state === 'QUEUED' || state === 'RUNNING' || hasLeadOptPredictionRuntime(row)) {
      selected.set(row.id, row);
    }
  }
  for (const row of sortProjectTasks(rows)) {
    if (selected.size >= TASK_LIST_RUNTIME_CACHE_MAX_ROWS) break;
    selected.set(row.id, row);
  }
  return sortProjectTasks(Array.from(selected.values()));
}

function pruneRuntimeCache(now: number): void {
  for (const [key, entry] of taskListRuntimeCache.entries()) {
    if (now - entry.savedAt > TASK_LIST_RUNTIME_CACHE_TTL_MS) {
      taskListRuntimeCache.delete(key);
    }
  }
  while (taskListRuntimeCache.size > TASK_LIST_RUNTIME_CACHE_MAX_ENTRIES) {
    const oldestKey = taskListRuntimeCache.keys().next().value;
    if (typeof oldestKey !== 'string') break;
    taskListRuntimeCache.delete(oldestKey);
  }
}

function removeLegacyRuntimeCacheEntries(): void {
  if (legacyRuntimeCacheCleanupDone || typeof window === 'undefined') return;
  legacyRuntimeCacheCleanupDone = true;
  try {
    const staleKeys: string[] = [];
    for (let index = 0; index < window.localStorage.length; index += 1) {
      const key = window.localStorage.key(index);
      if (key?.startsWith(LEGACY_TASK_LIST_RUNTIME_CACHE_KEY_PREFIX)) {
        staleKeys.push(key);
      }
    }
    staleKeys.forEach((key) => window.localStorage.removeItem(key));
  } catch {
    // A storage policy must not prevent the task page from loading.
  }
}

function mergeProjectRuntimeFields(next: Project, prev: Project): Project {
  const nextTaskId = String(next.task_id || '').trim();
  const prevTaskId = String(prev.task_id || '').trim();
  if (!nextTaskId || !prevTaskId || nextTaskId !== prevTaskId) return next;
  const nextPriority = taskStatePriority(next.task_state);
  const prevPriority = taskStatePriority(prev.task_state);
  if (prevPriority < nextPriority) return next;
  if (prevPriority > nextPriority) {
    return {
      ...next,
      task_state: prev.task_state,
      status_text: prev.status_text,
      error_text: prev.error_text,
      completed_at: prev.completed_at || next.completed_at,
      duration_seconds: prev.duration_seconds ?? next.duration_seconds
    };
  }
  return {
    ...next,
    completed_at: next.completed_at || prev.completed_at,
    duration_seconds: next.duration_seconds ?? prev.duration_seconds,
    status_text: String(next.status_text || '').trim() || prev.status_text,
    error_text: String(next.error_text || '').trim() || prev.error_text
  };
}

function collectPendingRuntimeTaskIds(taskRows: ProjectTask[]): Set<string> {
  return new Set(
    sanitizeTaskRows(taskRows)
      .filter((row) => {
        const taskId = String(row.task_id || '').trim();
        const taskState = String(row.task_state || '').trim().toUpperCase();
        return Boolean(taskId) && (taskState === 'QUEUED' || taskState === 'RUNNING');
      })
      .map((row) => String(row.task_id || '').trim())
      .filter(Boolean)
  );
}

// Avoid re-serializing the (referentially-stable) current state on every hydration tick: cache its
// JSON signature keyed on the source reference, recomputing only when the source actually changes.
function memoSignature<S, V>(source: S, value: V, cache: { source: S | null; sig: string }): string {
  if (cache.source === source) return cache.sig;
  const sig = JSON.stringify(value);
  cache.source = source;
  cache.sig = sig;
  return sig;
}

export function useProjectTasksDataLoader({
  projectId,
  sessionUserId,
  workspaceView,
  priorityTaskRowIds,
  structureSearchActive,
}: UseProjectTasksDataLoaderOptions): UseProjectTasksDataLoaderResult {
  const [project, setProject] = useState<Project | null>(null);
  const [tasks, setTasks] = useState<ProjectTask[]>([]);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Full-list load state: the task list pages in background chunks; consumers
  // (e.g. the Excel export) must be able to distinguish "still loading" from
  // "everything loaded" and to await completion.
  const [allTasksLoaded, setAllTasksLoaded] = useState(false);
  const allTasksLoadedRef = useRef(false);
  const fullLoadWaitersRef = useRef<Array<{ resolve: () => void; reject: (err: Error) => void }>>([]);
  const backgroundProgressListenersRef = useRef<Set<(loaded: number) => void>>(new Set());
  // True while ANY paginated fetch loop (initial background chunks or the
  // on-demand tail resume) is fetching — ensures waiting, never duplication.
  const backgroundLoadActiveRef = useRef(false);

  const loadSeqRef = useRef(0);
  const loadInFlightRef = useRef(false);
  const projectRef = useRef<Project | null>(null);
  const tasksRef = useRef<ProjectTask[]>([]);
  const runtimeStatusPollCursorRef = useRef(0);
  const leadOptStatusPollCursorRef = useRef(0);
  const currentProjectSigRef = useRef<{ source: Project | null; sig: string }>({ source: null, sig: '' });
  const currentRowsSigRef = useRef<{ source: ProjectTask[] | null; sig: string }>({ source: null, sig: '' });
  const taskListAccessContextRef = useRef<TaskListAccessContext | null>(null);
  const detailHydrationInFlightRef = useRef<Set<string>>(new Set());
  const resultHydrationInFlightRef = useRef<Set<string>>(new Set());
  const resultHydrationDoneRef = useRef<Set<string>>(new Set());
  const resultHydrationAttemptsRef = useRef<Map<string, number>>(new Map());
  const runtimeSnapshotTaskIdsRef = useRef<Set<string>>(new Set());
  const pendingForceRefetchRef = useRef(false);
  const loadDataRef = useRef<((options?: LoadTaskDataOptions) => Promise<void>) | null>(null);

  const taskListRuntimeCacheKey = useMemo(() => {
    const sessionIdentity = String(sessionUserId || '').trim().toLowerCase() || '__anonymous__';
    const normalizedProjectId = String(projectId || '').trim();
    if (!normalizedProjectId) return '';
    return `${sessionIdentity}:${normalizedProjectId}`;
  }, [projectId, sessionUserId]);

  useEffect(() => {
    removeLegacyRuntimeCacheEntries();
  }, []);

  useEffect(() => {
    projectRef.current = project;
  }, [project]);

  useEffect(() => {
    tasksRef.current = sanitizeTaskRows(tasks);
  }, [tasks]);

  useEffect(() => {
    resultHydrationInFlightRef.current.clear();
    resultHydrationDoneRef.current.clear();
    resultHydrationAttemptsRef.current.clear();
  }, [projectId]);

  useEffect(() => {
    detailHydrationInFlightRef.current.clear();
    taskListAccessContextRef.current = null;
    runtimeSnapshotTaskIdsRef.current = new Set();
    pendingForceRefetchRef.current = false;
  }, [projectId]);

  const persistRuntimeCache = useCallback(
    (projectRow: Project | null, taskRows: ProjectTask[]) => {
      if (!taskListRuntimeCacheKey || !projectRow) return;
      const now = Date.now();
      taskListRuntimeCache.delete(taskListRuntimeCacheKey);
      taskListRuntimeCache.set(taskListRuntimeCacheKey, {
        savedAt: now,
        project: projectRow,
        tasks: pickRuntimeCacheTaskRows(taskRows)
      });
      pruneRuntimeCache(now);
    },
    [taskListRuntimeCacheKey]
  );

  const hydrateRuntimeCache = useCallback(() => {
    if (!taskListRuntimeCacheKey) return false;
    if (projectRef.current || tasksRef.current.length > 0) return false;
    const now = Date.now();
    pruneRuntimeCache(now);
    const cached = taskListRuntimeCache.get(taskListRuntimeCacheKey);
    if (!cached) return false;
    const cachedTasks = sanitizeTaskRows(cached.tasks);
    if (cachedTasks.length === 0) {
      taskListRuntimeCache.delete(taskListRuntimeCacheKey);
      return false;
    }
    projectRef.current = cached.project;
    tasksRef.current = cachedTasks;
    setProject(cached.project);
    setTasks(cachedTasks);
    return true;
  }, [taskListRuntimeCacheKey]);

  // Read via ref inside syncRuntimeTasks so its identity (and therefore
  // loadData's, which the mount auto-effect depends on) stays stable while the
  // visible page's priority rows change — otherwise every filter/page change
  // re-triggered a full list reload.
  const priorityTaskRowIdsRef = useRef<string[]>([]);
  priorityTaskRowIdsRef.current = priorityTaskRowIds || [];

  const syncRuntimeTasks = useCallback(
    async (projectRow: Project, taskRows: ProjectTask[]) =>
      syncRuntimeTaskRows(projectRow, taskRows, {
        priorityTaskRowIds: priorityTaskRowIdsRef.current,
        runtimeStatusCursor: runtimeStatusPollCursorRef,
        leadOptStatusCursor: leadOptStatusPollCursorRef
      }),
    []
  );

  const syncInitialRuntimeTasks = useCallback(
    async (projectRow: Project, taskRows: ProjectTask[]) => syncInitialRuntimeTaskRows(projectRow, taskRows),
    []
  );

  const syncCachedRuntimeState = useCallback(
    async () => {
      const cachedProject = projectRef.current;
      const cachedTasks = sanitizeTaskRows(tasksRef.current);
      if (!cachedProject || cachedTasks.length === 0) return;
      const previousPendingTaskIds =
        runtimeSnapshotTaskIdsRef.current.size > 0
          ? new Set(runtimeSnapshotTaskIdsRef.current)
          : collectPendingRuntimeTaskIds(cachedTasks);

      const synced = await syncRuntimeTasks(cachedProject, cachedTasks);
      const nextPendingTaskIds = collectPendingRuntimeTaskIds(synced.taskRows);
      runtimeSnapshotTaskIdsRef.current = nextPendingTaskIds;
      if (
        previousPendingTaskIds.size > 0 &&
        Array.from(previousPendingTaskIds).some((taskId) => !nextPendingTaskIds.has(taskId))
      ) {
        pendingForceRefetchRef.current = true;
      }
      setProject(synced.project);
      // Functional merge: rows fetched by a concurrent pagination loop during
      // the sync's network awaits must survive this write.
      setTasks((prev) => sanitizeTaskRows(mergeTaskRowPages(sortProjectTasks(synced.taskRows), prev)));
      persistRuntimeCache(
        synced.project,
        sanitizeTaskRows(mergeTaskRowPages(sortProjectTasks(synced.taskRows), cachedTasks))
      );
      if (pendingForceRefetchRef.current) {
        pendingForceRefetchRef.current = false;
        window.setTimeout(() => {
          void loadDataRef.current?.({ silent: true, showRefreshing: false, forceRefetch: true });
        }, 0);
      }
    },
    [persistRuntimeCache, syncRuntimeTasks]
  );

  const markFullLoadComplete = useCallback(() => {
    allTasksLoadedRef.current = true;
    setAllTasksLoaded(true);
    const waiters = fullLoadWaitersRef.current;
    fullLoadWaitersRef.current = [];
    waiters.forEach((waiter) => waiter.resolve());
  }, []);

  const rejectFullLoadWaiters = useCallback((error: Error) => {
    const waiters = fullLoadWaitersRef.current;
    fullLoadWaitersRef.current = [];
    waiters.forEach((waiter) => waiter.reject(error));
  }, []);

  const loadData = useCallback(
    async (options?: LoadTaskDataOptions) => {
      if (loadInFlightRef.current) return;
      // A paginated load (initial background chunks or export tail-resume) owns
      // the list right now; a silent refresh would re-download what it is
      // already fetching.
      if (options?.silent && backgroundLoadActiveRef.current) return;
      const loadSeq = ++loadSeqRef.current;
      loadInFlightRef.current = true;
      allTasksLoadedRef.current = false;
      setAllTasksLoaded(false);
      const silent = Boolean(options?.silent);
      const showRefreshing = silent && options?.showRefreshing !== false;
      const preferBackendStatus = options?.preferBackendStatus !== false;
      const forceRefetch = Boolean(options?.forceRefetch);
      if (!forceRefetch) {
        hydrateRuntimeCache();
      }
      if (showRefreshing) {
        setRefreshing(true);
      } else if (!silent) {
        setLoading(true);
      }
      if (!silent) {
        setError(null);
      }

      try {
        const cachedProject = projectRef.current;
        const cachedTasks = sanitizeTaskRows(tasksRef.current);

        const projectRow = await getProjectById(projectId, { lightweight: true });
        if (!projectRow || projectRow.deleted_at) {
          throw new Error('Project not found or already deleted.');
        }
        const accessInfo =
          sessionUserId
            ? await getProjectAccessInfo(projectId, sessionUserId, projectRow.user_id)
            : { scope: 'owner' as const, accessLevel: 'owner' as const, taskIds: [], editableTaskIds: [] };
        if (sessionUserId && !accessInfo.scope) {
          throw new Error('You do not have permission to access this project.');
        }
        const projectAccessScope = accessInfo.scope || 'owner';
        taskListAccessContextRef.current = {
          scope: projectAccessScope,
          accessLevel: accessInfo.accessLevel || 'owner',
          editableTaskIds: accessInfo.editableTaskIds
        };
        const workflowKey = normalizeWorkflowKey(projectRow.task_type);
        const useLightweightTaskRows = workflowKey !== 'lead_optimization';
        const includeComponentsForList = workflowKey === 'prediction' || workflowKey === 'peptide_design';
        const includeConfidenceForList = false;
        const includeConfidenceSummaryForList = useLightweightTaskRows;
        const includePropertiesForList = false;
        const includePropertiesSummaryForList = useLightweightTaskRows;
        const baseTaskListOptions = {
          includeComponents: includeComponentsForList,
          includeConfidence: includeConfidenceForList,
          includeConfidenceSummary: includeConfidenceSummaryForList,
          includeProperties: includePropertiesForList,
          includePropertiesSummary: includePropertiesSummaryForList,
          includeLeadOptSummary: workflowKey === 'lead_optimization',
          taskRowIds: projectAccessScope === 'task_share' ? accessInfo.taskIds : undefined,
          accessScope: projectAccessScope,
          accessLevel: accessInfo.accessLevel || 'owner',
          editableTaskIds: accessInfo.editableTaskIds
        };
        const taskRows = await listProjectTasksForList(projectId, {
          ...baseTaskListOptions,
          limit: TASK_LIST_INITIAL_FETCH_LIMIT,
          offset: 0
        });
        const sortedTaskRows = sortProjectTasks(sanitizeTaskRows(taskRows));
        const accessibleProjectBase =
          projectAccessScope === 'task_share'
            ? sanitizeProjectForTaskShare(
                {
                  ...projectRow,
                  access_scope: projectAccessScope,
                  access_level: accessInfo.accessLevel || 'viewer',
                  accessible_task_ids: accessInfo.taskIds,
                  editable_task_ids: accessInfo.editableTaskIds
                },
                sortedTaskRows
              )
            : {
                ...projectRow,
                access_scope: projectAccessScope,
                access_level: accessInfo.accessLevel || 'owner',
                accessible_task_ids: [],
                editable_task_ids: accessInfo.editableTaskIds
              };
        let nextProject = cachedProject ? mergeProjectRuntimeFields(accessibleProjectBase, cachedProject) : accessibleProjectBase;
        let nextRows = mergeTaskRowPages(sortedTaskRows, cachedTasks);
        runtimeSnapshotTaskIdsRef.current = collectPendingRuntimeTaskIds(nextRows);

        if (loadSeqRef.current !== loadSeq) return;
        setProject(nextProject);
        setTasks(sanitizeTaskRows(nextRows));
        persistRuntimeCache(nextProject, nextRows);

        void (async () => {
          backgroundLoadActiveRef.current = true;
          try {
            // Resume, never re-download: rows already in memory (e.g. from a
            // previous completed load) are skipped, so a restart after a full
            // load costs one empty-chunk probe instead of the whole list again.
            let offset = Math.max(TASK_LIST_INITIAL_FETCH_LIMIT, sanitizeTaskRows(nextRows).length);
            while (loadSeqRef.current === loadSeq) {
              if (TASK_LIST_BACKGROUND_FETCH_DELAY_MS > 0) {
                await new Promise((resolve) => window.setTimeout(resolve, TASK_LIST_BACKGROUND_FETCH_DELAY_MS));
              }
              // Tail chunks ship WITHOUT components: only the first screens' worth needs the
              // ligand/target data eagerly — visible rows hydrate components on demand (see
              // the visible-row hydration effect). For long projects this is the bulk of the
              // background download, and merge preserves any components already in memory.
              const chunkRows = await listProjectTasksForList(projectId, {
                ...baseTaskListOptions,
                includeComponents: false,
                limit: TASK_LIST_BACKGROUND_FETCH_CHUNK_SIZE,
                offset
              });
              if (loadSeqRef.current !== loadSeq) return;
              if (chunkRows.length === 0) {
                markFullLoadComplete();
                return;
              }
              const sortedChunkRows = sortProjectTasks(sanitizeTaskRows(chunkRows));
              const optimisticMerged = sanitizeTaskRows(mergeTaskRowPages(sortedChunkRows, tasksRef.current));
              const projectForChunk = projectRef.current || nextProject;
              runtimeSnapshotTaskIdsRef.current = collectPendingRuntimeTaskIds(optimisticMerged);
              // Functional write: rows merged by a concurrent writer during the
              // chunk fetch must not be clobbered (a clobbered chunk would be
              // lost forever — offset has already advanced past it).
              setTasks((prev) => sanitizeTaskRows(mergeTaskRowPages(sortedChunkRows, prev)));
              persistRuntimeCache(projectForChunk, optimisticMerged);
              backgroundProgressListenersRef.current.forEach((listener) => listener(optimisticMerged.length));
              if (chunkRows.length < TASK_LIST_BACKGROUND_FETCH_CHUNK_SIZE) {
                markFullLoadComplete();
                return;
              }
              offset += TASK_LIST_BACKGROUND_FETCH_CHUNK_SIZE;
            }
          } finally {
            backgroundLoadActiveRef.current = false;
          }
        })().catch((err: unknown) => {
          // Keep the visible rows, but waiters get the real error — a partial
          // list must never masquerade as complete.
          rejectFullLoadWaiters(
            err instanceof Error ? err : new Error('Task list background load failed.')
          );
        });

        if (!preferBackendStatus) {
          return;
        }

        void (async () => {
          try {
            let runtimeSettledProject = nextProject;
            let runtimeSettledRows = nextRows;
            if (!silent) {
              const initialSynced = await syncInitialRuntimeTasks(runtimeSettledProject, runtimeSettledRows);
              if (loadSeqRef.current !== loadSeq) return;
              runtimeSettledProject = initialSynced.project;
              runtimeSettledRows = sanitizeTaskRows(initialSynced.taskRows);
              runtimeSnapshotTaskIdsRef.current = collectPendingRuntimeTaskIds(runtimeSettledRows);
              setProject(runtimeSettledProject);
              setTasks(runtimeSettledRows);
              persistRuntimeCache(runtimeSettledProject, runtimeSettledRows);

              const fullySynced = await syncRuntimeTasks(runtimeSettledProject, runtimeSettledRows);
              if (loadSeqRef.current !== loadSeq) return;
              runtimeSettledProject = fullySynced.project;
              runtimeSettledRows = sanitizeTaskRows(fullySynced.taskRows);
              runtimeSnapshotTaskIdsRef.current = collectPendingRuntimeTaskIds(runtimeSettledRows);
              setProject(runtimeSettledProject);
              setTasks(runtimeSettledRows);
              persistRuntimeCache(runtimeSettledProject, runtimeSettledRows);
            }
          } catch (err) {
            console.error('Backend status sync failed; keeping runtime-synced rows.', err);
            // Keep runtime-synced rows if backend status sync fails.
          }
        })();
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load task history.');
        // A load that fails before its pagination loop spawns must unblock
        // exporters waiting on completion — otherwise the export hangs forever.
        rejectFullLoadWaiters(
          err instanceof Error ? err : new Error('Failed to load task history.')
        );
      } finally {
        if (showRefreshing) {
          setRefreshing(false);
        } else if (!silent) {
          setLoading(false);
        }
        loadInFlightRef.current = false;
      }
    },
    [hydrateRuntimeCache, persistRuntimeCache, projectId, sessionUserId, syncInitialRuntimeTasks, syncRuntimeTasks, markFullLoadComplete]
  );

  useEffect(() => {
    loadDataRef.current = loadData;
    return () => {
      loadDataRef.current = null;
    };
  }, [loadData]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  useEffect(() => {
    if (workspaceView !== 'tasks') return;
    const onFocus = () => {
      void loadData({ silent: true, showRefreshing: false, forceRefetch: true });
    };
    const onVisible = () => {
      if (document.visibilityState === 'visible') {
        void loadData({ silent: true, showRefreshing: false, forceRefetch: true });
      }
    };
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [loadData, workspaceView]);

  useEffect(() => {
    if (workspaceView !== 'tasks') return;
    const projectRow = projectRef.current;
    if (!projectRow) return;
    if (normalizeWorkflowKey(projectRow.task_type) !== 'prediction') return;
    const currentRows = sanitizeTaskRows(tasksRef.current);
    if (currentRows.length === 0) return;

    let cancelled = false;
    void (async () => {
      try {
        const hydrated = await hydrateTaskMetricsFromResultRows(projectRow, currentRows, {
          resultHydrationInFlightRef,
          resultHydrationDoneRef,
          resultHydrationAttemptsRef
        });
        if (cancelled) return;
        const nextRows = sanitizeTaskRows(hydrated.taskRows);
        const nextProject = hydrated.project;
        const projectChanged = JSON.stringify(nextProject) !== memoSignature(projectRef.current, projectRef.current, currentProjectSigRef.current);
        const rowsChanged = JSON.stringify(nextRows) !== memoSignature(tasksRef.current, currentRows, currentRowsSigRef.current);
        if (!projectChanged && !rowsChanged) return;
        setProject(nextProject);
        // Functional merge: concurrent pagination may have added rows while the
        // hydration requests were in flight.
        setTasks((prev) => sanitizeTaskRows(mergeTaskRowPages(sortProjectTasks(nextRows), prev)));
        persistRuntimeCache(nextProject, nextRows);
      } catch (err) {
        console.error('Background result hydration failed; keeping lightweight rows.', err);
        // Keep lightweight rows if background result hydration fails.
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [persistRuntimeCache, tasks, workspaceView]);

  const runtimePollState = useMemo(() => {
    let hasActiveRuntime = false;
    let hasRunning = false;
    let hasQueued = false;
    for (const row of tasks) {
      if (!isProjectTaskRow(row)) continue;
      const taskState = String(row.task_state || '').trim().toUpperCase();
      if (Boolean(row.task_id) && (taskState === 'QUEUED' || taskState === 'RUNNING')) {
        hasActiveRuntime = true;
        if (taskState === 'RUNNING') hasRunning = true;
        if (taskState === 'QUEUED') hasQueued = true;
        continue;
      }
      if (hasLeadOptPredictionRuntime(row)) {
        hasActiveRuntime = true;
        hasRunning = true;
      }
    }
    return {
      hasActiveRuntime,
      hasRunning,
      hasQueued
    };
  }, [tasks]);

  useEffect(() => {
    if (workspaceView !== 'tasks') return;
    if (!runtimePollState.hasActiveRuntime) return;
    let cancelled = false;
    let timer: number | null = null;
    let inFlight = false;
    const computeDelayMs = () => {
      const baseDelay = runtimePollState.hasRunning ? 2500 : runtimePollState.hasQueued ? 4000 : 9000;
      if (typeof document !== 'undefined' && document.visibilityState !== 'visible') {
        return baseDelay * 2;
      }
      return baseDelay;
    };
    const scheduleNext = () => {
      if (cancelled) return;
      timer = window.setTimeout(() => {
        void tick();
      }, computeDelayMs());
    };
    const tick = async () => {
      if (cancelled || inFlight) return;
      inFlight = true;
      try {
        await syncCachedRuntimeState();
      } finally {
        inFlight = false;
        scheduleNext();
      }
    };
    scheduleNext();
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [runtimePollState.hasActiveRuntime, runtimePollState.hasQueued, runtimePollState.hasRunning, syncCachedRuntimeState, workspaceView]);

  useEffect(() => {
    if (workspaceView !== 'tasks') return;
    const projectRow = projectRef.current;
    if (!projectRow) return;
    const workflowKey = normalizeWorkflowKey(projectRow.task_type);
    // Prediction/peptide rows render ligand/target columns from components; the list's tail
    // chunks ship without them, so visible rows hydrate on demand here.
    if (workflowKey !== 'prediction' && workflowKey !== 'peptide_design') return;
    const accessContext = taskListAccessContextRef.current;
    if (!accessContext) return;

    const priorityIds = Array.from(
      new Set(
        (priorityTaskRowIds || [])
          .map((value) => String(value || '').trim())
          .filter(Boolean)
      )
    );
    if (priorityIds.length === 0) return;

    const currentRows = sanitizeTaskRows(tasksRef.current);
    const taskIdsToHydrate = priorityIds.filter((taskRowId) => {
      if (detailHydrationInFlightRef.current.has(taskRowId)) return false;
      const row = currentRows.find((item) => String(item.id || '').trim() === taskRowId);
      if (!row) return false;
      const hasConfidence = Boolean(row.confidence && typeof row.confidence === 'object' && !Array.isArray(row.confidence) && Object.keys(row.confidence as Record<string, unknown>).length > 0);
      const hasProperties = Boolean(row.properties && typeof row.properties === 'object' && !Array.isArray(row.properties) && Object.keys(asRecord(row.properties)).length > 0);
      if (!(hasConfidence && hasProperties)) return true;
      // Tail rows arrive without components (see the background chunk loop). Only submitted
      // rows hydrate them — a submitted row always carries its snapshot components, so the
      // fetch lands non-empty and this trigger does not re-arm on later page changes.
      const missingComponents = !Array.isArray(row.components) && Boolean(String(row.task_id || '').trim());
      return missingComponents;
    });
    if (taskIdsToHydrate.length === 0) return;

    taskIdsToHydrate.forEach((taskRowId) => detailHydrationInFlightRef.current.add(taskRowId));
    let cancelled = false;

    void (async () => {
      try {
        const detailedRows = await listProjectTasksForList(projectId, {
          includeComponents: true,
          includeConfidence: true,
          includeProperties: true,
          taskRowIds: taskIdsToHydrate,
          accessScope: accessContext.scope,
          accessLevel: accessContext.accessLevel,
          editableTaskIds: accessContext.editableTaskIds
        });
        if (cancelled || detailedRows.length === 0) return;
        const detailById = new Map(
          detailedRows
            .map((row) => [String(row.id || '').trim(), row] as const)
            .filter(([id]) => Boolean(id))
        );
        setTasks((prev) =>
          sanitizeTaskRows(
            prev.map((row) => {
              const detail = detailById.get(String(row.id || '').trim());
              if (!detail) return row;
              return mergeTaskRuntimeFields(detail, row);
            })
          )
        );
      } catch (err) {
        console.error('Visible-row detail hydration failed; keeping lightweight rows.', err);
        // Keep lightweight rows if visible-row hydration fails.
      } finally {
        taskIdsToHydrate.forEach((taskRowId) => detailHydrationInFlightRef.current.delete(taskRowId));
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [priorityTaskRowIds, projectId, workspaceView]);

  // Structure search (SMILES/SMARTS) matches ligand data derived from row components. The list's
  // tail chunks ship without components, so unvisited tail rows would silently never match.
  // While a structure search is active, backfill components for loaded rows in small chunks.
  // Each row is attempted at most once per search session — a row that legitimately has none
  // must not requeue forever.
  const componentBackfillAttemptedRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!structureSearchActive) {
      componentBackfillAttemptedRef.current = new Set();
      return;
    }
    if (workspaceView !== 'tasks') return;
    const projectRow = projectRef.current;
    if (!projectRow) return;
    const workflowKey = normalizeWorkflowKey(projectRow.task_type);
    if (workflowKey !== 'prediction' && workflowKey !== 'peptide_design') return;
    const accessContext = taskListAccessContextRef.current;
    if (!accessContext) return;

    let cancelled = false;
    void (async () => {
      try {
        for (;;) {
          if (cancelled) return;
          const pendingIds = sanitizeTaskRows(tasksRef.current)
            .filter(
              (row) =>
                Boolean(String(row.task_id || '').trim()) &&
                !Array.isArray(row.components) &&
                !componentBackfillAttemptedRef.current.has(String(row.id || '').trim()) &&
                !detailHydrationInFlightRef.current.has(String(row.id || '').trim())
            )
            .map((row) => String(row.id || '').trim())
            .filter(Boolean)
            .slice(0, 48);
          if (pendingIds.length === 0) {
            // Stay resident while the search is active: the background tail load may still be
            // merging lightweight rows. The loop exits via `cancelled` on unmount or when the
            // search flag drops (the effect re-runs its cleanup).
            await new Promise((resolve) => window.setTimeout(resolve, 500));
            continue;
          }
          pendingIds.forEach((taskRowId) => {
            componentBackfillAttemptedRef.current.add(taskRowId);
            detailHydrationInFlightRef.current.add(taskRowId);
          });
          try {
            const componentRows = await listProjectTasksForList(projectId, {
              includeComponents: true,
              includeConfidence: false,
              includeProperties: false,
              taskRowIds: pendingIds,
              accessScope: accessContext.scope,
              accessLevel: accessContext.accessLevel,
              editableTaskIds: accessContext.editableTaskIds
            });
            if (cancelled) return;
            if (componentRows.length > 0) {
              const detailById = new Map(
                componentRows
                  .map((row) => [String(row.id || '').trim(), row] as const)
                  .filter(([id]) => Boolean(id))
              );
              setTasks((prev) =>
                sanitizeTaskRows(
                  prev.map((row) => {
                    const detail = detailById.get(String(row.id || '').trim());
                    if (!detail) return row;
                    return mergeTaskRuntimeFields(detail, row);
                  })
                )
              );
            }
          } finally {
            pendingIds.forEach((taskRowId) => detailHydrationInFlightRef.current.delete(taskRowId));
          }
          await new Promise((resolve) => window.setTimeout(resolve, 120));
        }
      } catch (err) {
        console.error('Structure-search component backfill failed; search covers hydrated rows only.', err);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [structureSearchActive, projectId, workspaceView]);

  /**
   * Continue pagination from the rows already in memory. Claims the loader
   * (loadInFlight) so page refreshes cannot start a duplicate load, and bumps
   * loadSeq to abort any stale background loop from a superseded page load.
   * Progress is reported through backgroundProgressListenersRef only.
   */
  const resumeTailLoad = useCallback(
    async () => {
      const projectRow = projectRef.current;
      const accessContext = taskListAccessContextRef.current;
      if (!projectRow || !accessContext || !projectId) {
        throw new Error('Task list is not ready yet.');
      }
      const workflowKey = normalizeWorkflowKey(projectRow.task_type);
      const useLightweightTaskRows = workflowKey !== 'lead_optimization';
      const baseTaskListOptions = {
        includeComponents: workflowKey === 'prediction' || workflowKey === 'peptide_design',
        includeConfidence: false,
        includeConfidenceSummary: useLightweightTaskRows,
        includeProperties: false,
        includePropertiesSummary: useLightweightTaskRows,
        includeLeadOptSummary: workflowKey === 'lead_optimization',
        taskRowIds: accessContext.scope === 'task_share' ? projectRow.accessible_task_ids || [] : undefined,
        accessScope: accessContext.scope,
        accessLevel: accessContext.accessLevel,
        editableTaskIds: accessContext.editableTaskIds
      };
      const loadSeq = ++loadSeqRef.current;
      loadInFlightRef.current = true;
      backgroundLoadActiveRef.current = true;
      try {
        let offset = tasksRef.current.length;
        for (;;) {
          // Same lightweight projection as the auto background tail (components hydrate on
          // demand for rows the user actually looks at).
          const chunkRows = await listProjectTasksForList(projectId, {
            ...baseTaskListOptions,
            includeComponents: false,
            limit: TASK_LIST_BACKGROUND_FETCH_CHUNK_SIZE,
            offset
          });
          if (loadSeqRef.current !== loadSeq) return;
          if (chunkRows.length === 0) {
            markFullLoadComplete();
            return;
          }
          const sortedChunkRows = sortProjectTasks(sanitizeTaskRows(chunkRows));
          const optimisticMerged = sanitizeTaskRows(mergeTaskRowPages(sortedChunkRows, tasksRef.current));
          runtimeSnapshotTaskIdsRef.current = collectPendingRuntimeTaskIds(optimisticMerged);
          // Functional write: concurrent writers must not lose their rows.
          setTasks((prev) => sanitizeTaskRows(mergeTaskRowPages(sortedChunkRows, prev)));
          persistRuntimeCache(projectRef.current, optimisticMerged);
          backgroundProgressListenersRef.current.forEach((listener) => listener(optimisticMerged.length));
          if (chunkRows.length < TASK_LIST_BACKGROUND_FETCH_CHUNK_SIZE) {
            markFullLoadComplete();
            return;
          }
          offset += TASK_LIST_BACKGROUND_FETCH_CHUNK_SIZE;
        }
      } catch (err) {
        rejectFullLoadWaiters(
          err instanceof Error ? err : new Error('Task list tail load failed.')
        );
        throw err;
      } finally {
        backgroundLoadActiveRef.current = false;
        loadInFlightRef.current = false;
      }
    },
    [projectId, markFullLoadComplete, rejectFullLoadWaiters]
  );

  const resumeTailLoadRef = useRef<(() => Promise<void>) | null>(null);
  useEffect(() => {
    resumeTailLoadRef.current = resumeTailLoad;
  }, [resumeTailLoad]);

  /**
   * Await the fully-loaded task list with minimal network cost:
   * - a paginated load already running -> just wait for it (zero extra requests);
   * - nothing running and the list partial -> fetch ONLY the missing tail rows,
   *   each exactly once (no page restart, no duplicate full load).
   * Failure propagates to the caller — a partial list is never reported complete.
   */
  const ensureAllTasksLoaded = useCallback(
    async (onProgress?: (loaded: number) => void) => {
      if (allTasksLoadedRef.current) {
        onProgress?.(tasksRef.current.length);
        return;
      }
      if (onProgress) {
        backgroundProgressListenersRef.current.add(onProgress);
        onProgress(tasksRef.current.length);
      }
      try {
        if (loadInFlightRef.current || backgroundLoadActiveRef.current) {
          await new Promise<void>((resolve, reject) => {
            fullLoadWaitersRef.current.push({ resolve, reject });
          });
          return;
        }
        await resumeTailLoadRef.current?.();
      } finally {
        if (onProgress) backgroundProgressListenersRef.current.delete(onProgress);
      }
    },
    []
  );

  return {
    project,
    tasks,
    loading,
    refreshing,
    error,
    allTasksLoaded,
    setProject,
    setTasks,
    setError,
    loadData,
    ensureAllTasksLoaded,
  };
}
