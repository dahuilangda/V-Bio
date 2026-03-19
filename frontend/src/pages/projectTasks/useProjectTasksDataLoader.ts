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

  useEffect(() => {
    projectRef.current = project;
  }, [project]);

  useEffect(() => {
    tasksRef.current = sanitizeTaskRows(tasks);
  }, [tasks]);

  useEffect(() => {
    detailHydrationInFlightRef.current.clear();
    taskListAccessContextRef.current = null;
  }, [projectId]);

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

  const hydrateTaskMetricsFromResults = useCallback(
    async (projectRow: Project, taskRows: ProjectTask[]) =>
      hydrateTaskMetricsFromResultRows(projectRow, taskRows, {
        resultHydrationInFlightRef,
        resultHydrationDoneRef,
        resultHydrationAttemptsRef
      }),
    []
  );

  const syncCachedRuntimeState = useCallback(
    async (options?: { hydrateResults?: boolean }) => {
      const cachedProject = projectRef.current;
      const cachedTasks = sanitizeTaskRows(tasksRef.current);
      if (!cachedProject || cachedTasks.length === 0) return;

      const synced = await syncRuntimeTasks(cachedProject, cachedTasks);
      setProject(synced.project);
      setTasks(sanitizeTaskRows(synced.taskRows));

      if (options?.hydrateResults === false) return;

      void (async () => {
        try {
          const hydrated = await hydrateTaskMetricsFromResults(synced.project, synced.taskRows);
          setProject(hydrated.project);
          setTasks(sanitizeTaskRows(hydrated.taskRows));
        } catch {
          // Keep runtime-synced state if result hydration fails.
        }
      })();
    },
    [hydrateTaskMetricsFromResults, syncRuntimeTasks]
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
        const useLightweightPredictionRows = workflowKey === 'prediction';
        const includeComponentsForList = false;
        const includeConfidenceForList =
          workflowKey === 'lead_optimization'
            ? false
            : workflowKey === 'peptide_design'
            ? !silent || cachedTasks.length === 0
            : useLightweightPredictionRows
              ? false
              : !(silent && cachedTasks.length > 0);
        const includeConfidenceSummaryForList = useLightweightPredictionRows && !includeConfidenceForList;
        const includePropertiesForList =
          workflowKey === 'lead_optimization'
            ? false
            : useLightweightPredictionRows
              ? false
              : true;
        const includePropertiesSummaryForList = useLightweightPredictionRows && !includePropertiesForList;
        const taskRows = await listProjectTasksForList(projectId, {
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
        let nextRows = mergeTaskRowsWithLocal(sortedTaskRows, cachedTasks);

        if (loadSeqRef.current !== loadSeq) return;
        setProject(nextProject);
        setTasks(sanitizeTaskRows(nextRows));

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
              setProject(runtimeSettledProject);
              setTasks(runtimeSettledRows);

              const fullySynced = await syncRuntimeTasks(runtimeSettledProject, runtimeSettledRows);
              if (loadSeqRef.current !== loadSeq) return;
              runtimeSettledProject = fullySynced.project;
              runtimeSettledRows = sanitizeTaskRows(fullySynced.taskRows);
              setProject(runtimeSettledProject);
              setTasks(runtimeSettledRows);
            }

            const hydrated = await hydrateTaskMetricsFromResults(runtimeSettledProject, runtimeSettledRows);
            if (loadSeqRef.current !== loadSeq) return;
            setProject(hydrated.project);
            setTasks(sanitizeTaskRows(hydrated.taskRows));
          } catch {
            // Keep runtime-synced rows if result hydration fails.
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
    [projectId, sessionUserId, syncInitialRuntimeTasks, syncRuntimeTasks, hydrateTaskMetricsFromResults]
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
        await syncCachedRuntimeState({ hydrateResults: true });
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
