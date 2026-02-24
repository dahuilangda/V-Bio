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

        const projectRow = await getProjectById(projectId);
        if (!projectRow || projectRow.deleted_at) {
          throw new Error('Project not found or already deleted.');
        }
        if (sessionUserId && projectRow.user_id !== sessionUserId) {
          throw new Error('You do not have permission to access this project.');
        }
        const workflowKey = normalizeWorkflowKey(projectRow.task_type);
        const includeComponentsForList = workflowKey === 'prediction';
        const taskRows = await listProjectTasksForList(projectId, { includeComponents: includeComponentsForList });

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

  const hasActiveRuntime = useMemo(
    () =>
      tasks.some(
        (row) => {
          if (!isProjectTaskRow(row)) return false;
          if (Boolean(row.task_id) && (row.task_state === 'QUEUED' || row.task_state === 'RUNNING')) return true;
          if (hasLeadOptPredictionRuntime(row)) return true;
          return false;
        }
      ),
    [tasks]
  );

  useEffect(() => {
    if (workspaceView !== 'tasks') return;
    if (!hasActiveRuntime) return;
    const timer = window.setInterval(() => {
      void loadData({ silent: true, showRefreshing: false });
    }, 4000);
    return () => window.clearInterval(timer);
  }, [hasActiveRuntime, loadData, workspaceView]);

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
