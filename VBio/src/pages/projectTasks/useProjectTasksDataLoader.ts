import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import type { Project, ProjectTask } from '../../types/models';
import { getProjectById, listProjectTasksForList } from '../../api/supabaseLite';
import { normalizeWorkflowKey } from '../../utils/workflows';
import {
  hasLeadOptPredictionRuntime,
  SILENT_CACHE_SYNC_WINDOW_MS,
  isProjectTaskRow,
  sanitizeTaskRows,
  sortProjectTasks,
  type LoadTaskDataOptions,
} from './taskDataUtils';
import { hydrateTaskMetricsFromResultRows, syncRuntimeTaskRows } from './taskRowSync';

interface UseProjectTasksDataLoaderOptions {
  projectId: string;
  sessionUserId: string | null;
  workspaceView: 'tasks' | 'api';
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

const TASK_STATE_PRIORITY: Record<string, number> = {
  DRAFT: 0,
  QUEUED: 1,
  RUNNING: 2,
  SUCCESS: 3,
  FAILURE: 3,
  REVOKED: 3,
};

function taskStatePriority(value: unknown): number {
  return TASK_STATE_PRIORITY[String(value || '').trim().toUpperCase()] ?? 0;
}

function hasObjectContent(value: unknown): boolean {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value as Record<string, unknown>).length > 0);
}

function mergeTaskRuntimeFields(next: ProjectTask, prev: ProjectTask): ProjectTask {
  const nextTaskId = String(next.task_id || '').trim();
  const prevTaskId = String(prev.task_id || '').trim();
  if (!nextTaskId || !prevTaskId || nextTaskId !== prevTaskId) return next;
  const nextPriority = taskStatePriority(next.task_state);
  const prevPriority = taskStatePriority(prev.task_state);
  if (prevPriority < nextPriority) return next;
  if (prevPriority > nextPriority) {
    return {
      ...next,
      confidence: hasObjectContent(next.confidence) ? next.confidence : prev.confidence,
      affinity: hasObjectContent(next.affinity) ? next.affinity : prev.affinity,
      components: Array.isArray(next.components) && next.components.length > 0 ? next.components : prev.components,
      properties: hasObjectContent(next.properties) ? next.properties : prev.properties,
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
    properties: hasObjectContent(next.properties) ? next.properties : prev.properties,
    completed_at: next.completed_at || prev.completed_at,
    duration_seconds: next.duration_seconds ?? prev.duration_seconds,
    status_text: String(next.status_text || '').trim() || prev.status_text,
    error_text: String(next.error_text || '').trim() || prev.error_text
  };
}

function mergeTaskRowsWithLocal(nextRows: ProjectTask[], prevRows: ProjectTask[]): ProjectTask[] {
  if (prevRows.length === 0 || nextRows.length === 0) return nextRows;
  const prevById = new Map(prevRows.map((row) => [row.id, row] as const));
  return nextRows.map((row) => {
    const prev = prevById.get(row.id);
    if (!prev) return row;
    return mergeTaskRuntimeFields(row, prev);
  });
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

export function useProjectTasksDataLoader({
  projectId,
  sessionUserId,
  workspaceView,
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
  const resultHydrationInFlightRef = useRef<Set<string>>(new Set());
  const resultHydrationDoneRef = useRef<Set<string>>(new Set());
  const resultHydrationAttemptsRef = useRef<Map<string, number>>(new Map());

  useEffect(() => {
    projectRef.current = project;
  }, [project]);

  useEffect(() => {
    tasksRef.current = sanitizeTaskRows(tasks);
  }, [tasks]);

  const syncRuntimeTasks = useCallback(
    async (projectRow: Project, taskRows: ProjectTask[]) => syncRuntimeTaskRows(projectRow, taskRows),
    []
  );

  const hydrateTaskMetricsFromResults = useCallback(
    async (projectRow: Project, taskRows: ProjectTask[]) =>
      hydrateTaskMetricsFromResultRows(projectRow, taskRows, {
        resultHydrationInFlightRef,
        resultHydrationDoneRef,
        resultHydrationAttemptsRef
      }),
    []
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
      if (showRefreshing) {
        setRefreshing(true);
      } else if (!silent) {
        setLoading(true);
      }
      if (!silent) {
        setError(null);
      }

      try {
        const now = Date.now();
        const cachedProject = projectRef.current;
        const cachedTasks = sanitizeTaskRows(tasksRef.current);
        const withinCacheWindow = now - lastFullFetchTsRef.current <= SILENT_CACHE_SYNC_WINDOW_MS;
        const hasLeadOptRuntimeInCache = cachedTasks.some((row) => hasLeadOptPredictionRuntime(row));
        const canUseCachedSync =
          silent &&
          !forceRefetch &&
          preferBackendStatus &&
          withinCacheWindow &&
          cachedProject &&
          cachedTasks.length > 0 &&
          !hasLeadOptRuntimeInCache;

        if (canUseCachedSync) {
          const synced = await syncRuntimeTasks(cachedProject, cachedTasks);
          if (loadSeqRef.current !== loadSeq) return;
          setProject(synced.project);
          setTasks(sanitizeTaskRows(synced.taskRows));
          void (async () => {
            try {
              const hydrated = await hydrateTaskMetricsFromResults(synced.project, synced.taskRows);
              if (loadSeqRef.current !== loadSeq) return;
              setProject(hydrated.project);
              setTasks(sanitizeTaskRows(hydrated.taskRows));
            } catch {
              // Keep cached sync state if result hydration fails.
            }
          })();
          return;
        }

        const projectRow = await getProjectById(projectId, { lightweight: true });
        if (!projectRow || projectRow.deleted_at) {
          throw new Error('Project not found or already deleted.');
        }
        if (sessionUserId && projectRow.user_id !== sessionUserId) {
          throw new Error('You do not have permission to access this project.');
        }
        const workflowKey = normalizeWorkflowKey(projectRow.task_type);
        const includeComponentsForList = workflowKey === 'prediction';
        const includeConfidenceForList =
          workflowKey === 'lead_optimization'
            ? false
            : workflowKey === 'peptide_design'
            ? !silent || cachedTasks.length === 0
            : !(silent && cachedTasks.length > 0);
        const includePropertiesForList = workflowKey === 'lead_optimization' ? false : true;
        const taskRows = await listProjectTasksForList(projectId, {
          includeComponents: includeComponentsForList,
          includeConfidence: includeConfidenceForList,
          includeProperties: includePropertiesForList,
          includeLeadOptSummary: workflowKey === 'lead_optimization'
        });

        lastFullFetchTsRef.current = Date.now();
        const sortedTaskRows = sortProjectTasks(sanitizeTaskRows(taskRows));
        const mergedProject = cachedProject ? mergeProjectRuntimeFields(projectRow, cachedProject) : projectRow;
        const mergedRows = mergeTaskRowsWithLocal(sortedTaskRows, cachedTasks);
        if (loadSeqRef.current !== loadSeq) return;
        setProject(mergedProject);
        setTasks(sanitizeTaskRows(mergedRows));

        if (!preferBackendStatus) {
          return;
        }

        void (async () => {
          try {
            const synced = await syncRuntimeTasks(mergedProject, mergedRows);
            if (loadSeqRef.current !== loadSeq) return;
            setProject(synced.project);
            setTasks(sanitizeTaskRows(synced.taskRows));

            const hydrated = await hydrateTaskMetricsFromResults(synced.project, synced.taskRows);
            if (loadSeqRef.current !== loadSeq) return;
            setProject(hydrated.project);
            setTasks(sanitizeTaskRows(hydrated.taskRows));
          } catch {
            // Keep base rows rendered; background refinement is best-effort.
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
    [projectId, sessionUserId, syncRuntimeTasks, hydrateTaskMetricsFromResults]
  );

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
      const baseDelay = runtimePollState.hasRunning ? 5000 : runtimePollState.hasQueued ? 9000 : 12000;
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
        await loadData({ silent: true, showRefreshing: false });
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
  }, [loadData, runtimePollState.hasActiveRuntime, runtimePollState.hasQueued, runtimePollState.hasRunning, workspaceView]);

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
