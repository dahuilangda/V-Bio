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
}

interface UseProjectTasksDataLoaderResult {
  project: Project | null;
  tasks: ProjectTask[];
  loading: boolean;
  refreshing: boolean;
  error: string | null;
  setProject: Dispatch<SetStateAction<Project | null>>;
  setTasks: Dispatch<SetStateAction<ProjectTask[]>>;
  setError: Dispatch<SetStateAction<string | null>>;
  loadData: (options?: LoadTaskDataOptions) => Promise<void>;
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
const TASK_LIST_RUNTIME_CACHE_KEY_VERSION = 'v2';
const TASK_LIST_INITIAL_FETCH_LIMIT = 120;
const TASK_LIST_BACKGROUND_FETCH_CHUNK_SIZE = 240;
const TASK_LIST_BACKGROUND_FETCH_DELAY_MS = 80;
const TASK_LIST_RUNTIME_CACHE_MAX_ROWS = 180;

function taskStatePriority(value: unknown): number {
  return TASK_STATE_PRIORITY[String(value || '').trim().toUpperCase()] ?? 0;
}

function hasObjectContent(value: unknown): boolean {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value as Record<string, unknown>).length > 0);
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
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
      confidence: hasObjectContent(next.confidence) ? next.confidence : prev.confidence,
      affinity: hasObjectContent(next.affinity) ? next.affinity : prev.affinity,
      components: Array.isArray(next.components) && next.components.length > 0 ? next.components : prev.components,
      properties: mergedLeadOptProperties || (hasObjectContent(next.properties) ? next.properties : prev.properties),
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
      confidence: hasObjectContent(next.confidence) ? next.confidence : prev.confidence,
      affinity: hasObjectContent(next.affinity) ? next.affinity : prev.affinity,
      components: Array.isArray(next.components) && next.components.length > 0 ? next.components : prev.components,
      properties: mergedLeadOptProperties || (hasObjectContent(next.properties) ? next.properties : prev.properties),
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
    confidence: hasObjectContent(next.confidence) ? next.confidence : prev.confidence,
    affinity: hasObjectContent(next.affinity) ? next.affinity : prev.affinity,
    components: Array.isArray(next.components) && next.components.length > 0 ? next.components : prev.components,
    properties: mergedLeadOptProperties || (hasObjectContent(next.properties) ? next.properties : prev.properties),
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

export function useProjectTasksDataLoader({
  projectId,
  sessionUserId,
  workspaceView,
  priorityTaskRowIds,
}: UseProjectTasksDataLoaderOptions): UseProjectTasksDataLoaderResult {
  const [project, setProject] = useState<Project | null>(null);
  const [tasks, setTasks] = useState<ProjectTask[]>([]);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadSeqRef = useRef(0);
  const loadInFlightRef = useRef(false);
  const lastFullFetchTsRef = useRef(0);
  const projectRef = useRef<Project | null>(null);
  const tasksRef = useRef<ProjectTask[]>([]);
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
    return `vbio:project-tasks-runtime:${TASK_LIST_RUNTIME_CACHE_KEY_VERSION}:${sessionIdentity}:${normalizedProjectId}`;
  }, [projectId, sessionUserId]);

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
      if (typeof window === 'undefined' || !taskListRuntimeCacheKey || !projectRow) return;
      try {
        window.localStorage.setItem(
          taskListRuntimeCacheKey,
          JSON.stringify({
            saved_at: Date.now(),
            project: projectRow,
            tasks: pickRuntimeCacheTaskRows(taskRows)
          })
        );
      } catch {
        // Ignore storage quota or serialization failures.
      }
    },
    [taskListRuntimeCacheKey]
  );

  const hydrateRuntimeCache = useCallback(() => {
    if (typeof window === 'undefined' || !taskListRuntimeCacheKey) return false;
    if (projectRef.current || tasksRef.current.length > 0) return false;
    try {
      const raw = window.localStorage.getItem(taskListRuntimeCacheKey);
      if (!raw) return false;
      const parsed = JSON.parse(raw) as {
        saved_at?: number;
        project?: Project | null;
        tasks?: ProjectTask[] | null;
      };
      const savedAt = Number(parsed?.saved_at || 0);
      if (!Number.isFinite(savedAt) || Date.now() - savedAt > TASK_LIST_RUNTIME_CACHE_TTL_MS) {
        window.localStorage.removeItem(taskListRuntimeCacheKey);
        return false;
      }
      const cachedProject = parsed?.project || null;
      const cachedTasks = sanitizeTaskRows(Array.isArray(parsed?.tasks) ? parsed.tasks : []);
      if (!cachedProject || cachedTasks.length === 0) return false;
      projectRef.current = cachedProject;
      tasksRef.current = cachedTasks;
      setProject(cachedProject);
      setTasks(cachedTasks);
      return true;
    } catch {
      return false;
    }
  }, [taskListRuntimeCacheKey]);

  const syncRuntimeTasks = useCallback(
    async (projectRow: Project, taskRows: ProjectTask[]) =>
      syncRuntimeTaskRows(projectRow, taskRows, {
        priorityTaskRowIds
      }),
    [priorityTaskRowIds]
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
      setTasks(sanitizeTaskRows(synced.taskRows));
      persistRuntimeCache(synced.project, synced.taskRows);
      if (pendingForceRefetchRef.current) {
        pendingForceRefetchRef.current = false;
        window.setTimeout(() => {
          void loadDataRef.current?.({ silent: true, showRefreshing: false, forceRefetch: true });
        }, 0);
      }
    },
    [persistRuntimeCache, syncRuntimeTasks]
  );

  const loadData = useCallback(
    async (options?: LoadTaskDataOptions) => {
      if (loadInFlightRef.current) return;
      const loadSeq = ++loadSeqRef.current;
      loadInFlightRef.current = true;
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
        const hasLeadOptRuntimeInCache = cachedTasks.some((row) => hasLeadOptPredictionRuntime(row));
        const canUseCachedSync =
          silent &&
          !forceRefetch &&
          preferBackendStatus &&
          cachedProject &&
          cachedTasks.length > 0 &&
          !hasLeadOptRuntimeInCache;

        if (canUseCachedSync) {
          const synced = await syncRuntimeTasks(cachedProject, cachedTasks);
          if (loadSeqRef.current !== loadSeq) return;
          setProject(synced.project);
          setTasks(sanitizeTaskRows(synced.taskRows));
          persistRuntimeCache(synced.project, synced.taskRows);
          return;
        }

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
        const includeComponentsForList = false;
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

        lastFullFetchTsRef.current = Date.now();
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
          let offset = TASK_LIST_INITIAL_FETCH_LIMIT;
          let mergedRows = sanitizeTaskRows(nextRows);
          while (loadSeqRef.current === loadSeq) {
            if (TASK_LIST_BACKGROUND_FETCH_DELAY_MS > 0) {
              await new Promise((resolve) => window.setTimeout(resolve, TASK_LIST_BACKGROUND_FETCH_DELAY_MS));
            }
            const chunkRows = await listProjectTasksForList(projectId, {
              ...baseTaskListOptions,
              limit: TASK_LIST_BACKGROUND_FETCH_CHUNK_SIZE,
              offset
            });
            if (loadSeqRef.current !== loadSeq || chunkRows.length === 0) return;
            const sortedChunkRows = sortProjectTasks(sanitizeTaskRows(chunkRows));
            mergedRows = mergeTaskRowPages(sortedChunkRows, tasksRef.current);
            const projectForChunk = projectRef.current || nextProject;
            runtimeSnapshotTaskIdsRef.current = collectPendingRuntimeTaskIds(mergedRows);
            setTasks(mergedRows);
            persistRuntimeCache(projectForChunk, mergedRows);
            if (chunkRows.length < TASK_LIST_BACKGROUND_FETCH_CHUNK_SIZE) {
              lastFullFetchTsRef.current = Date.now();
              return;
            }
            offset += TASK_LIST_BACKGROUND_FETCH_CHUNK_SIZE;
          }
        })().catch(() => {
          // Keep the visible first page if background chunk loading fails.
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
          } catch {
            // Keep runtime-synced rows if backend status sync fails.
          }
        })();
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load task history.');
      } finally {
        if (showRefreshing) {
          setRefreshing(false);
        } else if (!silent) {
          setLoading(false);
        }
        loadInFlightRef.current = false;
      }
    },
    [hydrateRuntimeCache, persistRuntimeCache, projectId, sessionUserId, syncInitialRuntimeTasks, syncRuntimeTasks]
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
        const projectChanged = JSON.stringify(nextProject) !== JSON.stringify(projectRef.current);
        const rowsChanged = JSON.stringify(nextRows) !== JSON.stringify(currentRows);
        if (!projectChanged && !rowsChanged) return;
        setProject(nextProject);
        setTasks(nextRows);
        persistRuntimeCache(nextProject, nextRows);
      } catch {
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
    if (normalizeWorkflowKey(projectRow.task_type) !== 'prediction') return;
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
      return !(hasConfidence && hasProperties);
    });
    if (taskIdsToHydrate.length === 0) return;

    taskIdsToHydrate.forEach((taskRowId) => detailHydrationInFlightRef.current.add(taskRowId));
    let cancelled = false;

    void (async () => {
      try {
        const detailedRows = await listProjectTasksForList(projectId, {
          includeComponents: false,
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
      } catch {
        // Keep lightweight rows if visible-row hydration fails.
      } finally {
        taskIdsToHydrate.forEach((taskRowId) => detailHydrationInFlightRef.current.delete(taskRowId));
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [priorityTaskRowIds, projectId, workspaceView]);

  return {
    project,
    tasks,
    loading,
    refreshing,
    error,
    setProject,
    setTasks,
    setError,
    loadData,
  };
}
