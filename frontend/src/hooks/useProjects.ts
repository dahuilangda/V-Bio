import { useCallback, useEffect, useRef, useState } from 'react';
import type { Project, ProjectTaskCounts, Session, TaskStatusResponse } from '../types/models';
import {
  deleteProjectSharesByProjectId,
  deleteProjectTaskSharesByProjectId,
  deleteProjectTasksByProjectId,
  insertProject,
  listProjectShareLinksForUser,
  listProjectTaskCountsByProjects,
  listProjectTaskShareLinksForUser,
  listProjects,
  listProjectsByIds,
  listProjectTaskStatesByTaskIds,
  listProjectTaskStatesByProjects,
  listProjectTaskStatesByTaskRowIds,
  updateProject,
  type ProjectTaskRuntimeRow
} from '../api/supabaseLite';
import { getTaskRuntimeIndex, getTaskStatuses, terminateTask } from '../api/backendApi';
import { removeProjectInputConfig, removeProjectUiState } from '../utils/projectInputs';
import { inferTaskStateFromStatusPayload } from '../utils/taskRuntime';
import { normalizeWorkflowKey } from '../utils/workflows';

const DEFAULT_TASK_TYPE = 'prediction';

const TASK_STATE_PRIORITY: Record<string, number> = {
  DRAFT: 0,
  QUEUED: 1,
  RUNNING: 2,
  SUCCESS: 3,
  FAILURE: 3,
  REVOKED: 3
};
const PROJECT_DELETE_TERMINATE_CONCURRENCY = 6;
const PROJECT_DELETE_STOP_POLL_INTERVAL_MS = 700;
const PROJECT_DELETE_STOP_POLL_TIMEOUT_MS = 12000;
const PROJECT_RUNTIME_COUNTS_CACHE_TTL_MS = 15000;

export interface CreateProjectInput {
  name: string;
  summary?: string;
  taskType?: string;
  backend?: string;
  useMsa?: boolean;
  proteinSequence?: string;
  ligandSmiles?: string;
}

interface LoadProjectsOptions {
  silent?: boolean;
  statusOnly?: boolean;
  preferBackendStatus?: boolean;
}

function buildProjectListSignature(rows: Project[]): string {
  return rows
    .map((row) => {
      const accessibleTaskIds = Array.isArray(row.accessible_task_ids)
        ? row.accessible_task_ids.map((item) => String(item || '').trim()).filter(Boolean).join(',')
        : '';
      return [
        row.id,
        row.updated_at,
        row.task_id,
        row.task_state,
        row.access_scope,
        row.access_level,
        accessibleTaskIds
      ]
        .map((item) => String(item || '').trim())
        .join('|');
    })
    .join('\n');
}

function taskStatePriority(value: unknown): number {
  return TASK_STATE_PRIORITY[String(value || '').trim().toUpperCase()] ?? 0;
}

function mergeProjectRuntimeFields(next: Project, prev: Project | null): Project {
  if (!prev) return next;
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
    status_text: String(next.status_text || '').trim() || prev.status_text,
    error_text: String(next.error_text || '').trim() || prev.error_text,
    completed_at: next.completed_at || prev.completed_at,
    duration_seconds: next.duration_seconds ?? prev.duration_seconds
  };
}

function applyOwnerProjectAccess(project: Project): Project {
  return {
    ...project,
    access_scope: 'owner',
    access_level: 'owner',
    accessible_task_ids: [],
    editable_task_ids: []
  };
}

function buildAccessibleProjects(
  ownedProjects: Project[],
  projectShareLinks: Array<{ project_id: string; access_level: 'viewer' | 'editor' }>,
  taskShareLinks: Array<{ project_id: string; project_task_id: string; access_level: 'viewer' | 'editor' }>,
  sharedProjects: Project[]
): Project[] {
  const ownedIds = new Set(ownedProjects.map((project) => String(project.id || '').trim()).filter(Boolean));
  const projectShareByProjectId = new Map(
    projectShareLinks
      .map((row) => [String(row.project_id || '').trim(), row.access_level] as const)
      .filter(([projectId]) => Boolean(projectId))
  );
  const taskShareIdsByProject = new Map<string, string[]>();
  const editableTaskIdsByProject = new Map<string, string[]>();

  for (const row of taskShareLinks) {
    const projectId = String(row.project_id || '').trim();
    const taskId = String(row.project_task_id || '').trim();
    if (!projectId || !taskId) continue;
    const currentTaskIds = taskShareIdsByProject.get(projectId) || [];
    currentTaskIds.push(taskId);
    taskShareIdsByProject.set(projectId, currentTaskIds);
    if (row.access_level === 'editor') {
      const editableTaskIds = editableTaskIdsByProject.get(projectId) || [];
      editableTaskIds.push(taskId);
      editableTaskIdsByProject.set(projectId, editableTaskIds);
    }
  }

  const sharedById = new Map(sharedProjects.map((project) => [project.id, project] as const));
  const sharedProjectIds = Array.from(
    new Set([
      ...Array.from(projectShareByProjectId.keys()),
      ...Array.from(taskShareIdsByProject.keys())
    ])
  ).filter((id) => !ownedIds.has(id));

  const rows = ownedProjects.map((project) => applyOwnerProjectAccess(project));
  for (const projectId of sharedProjectIds) {
    const project = sharedById.get(projectId);
    if (!project) continue;
    const taskIds = Array.from(new Set(taskShareIdsByProject.get(projectId) || []));
    const editableTaskIds = Array.from(new Set(editableTaskIdsByProject.get(projectId) || []));
    const projectShareLevel = projectShareByProjectId.get(projectId) || null;
    rows.push({
      ...project,
      access_scope: projectShareLevel ? 'project_share' : 'task_share',
      access_level: projectShareLevel ? (projectShareLevel === 'editor' ? 'editor' : 'viewer') : editableTaskIds.length > 0 ? 'editor' : 'viewer',
      accessible_task_ids: taskIds,
      editable_task_ids: editableTaskIds
    });
  }

  rows.sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime());
  return rows;
}

async function fetchRuntimeStatusesByTaskId(taskIds: string[]): Promise<Record<string, TaskStatusResponse>> {
  const normalizedTaskIds = Array.from(
    new Set(
      (Array.isArray(taskIds) ? taskIds : [])
        .map((item) => String(item || '').trim())
        .filter(Boolean)
    )
  );
  if (normalizedTaskIds.length === 0) return {};
  try {
    return await getTaskStatuses(normalizedTaskIds);
  } catch {
    return {};
  }
}

function emptyProjectTaskCounts(): ProjectTaskCounts {
  return {
    total: 0,
    running: 0,
    success: 0,
    failure: 0,
    queued: 0,
    other: 0
  };
}

function isRuntimePendingState(value: unknown): boolean {
  const token = String(value || '').trim().toUpperCase();
  return token === 'QUEUED' || token === 'RUNNING';
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

async function runWithConcurrency<T>(
  items: T[],
  limit: number,
  worker: (item: T) => Promise<void>
): Promise<void> {
  const normalizedLimit = Math.max(1, Math.floor(limit));
  let cursor = 0;
  const runners = Array.from({ length: Math.min(normalizedLimit, items.length) }, async () => {
    while (cursor < items.length) {
      const currentIndex = cursor;
      cursor += 1;
      await worker(items[currentIndex]);
    }
  });
  await Promise.all(runners);
}

async function stopProjectRuntimeTasks(taskRows: ProjectTaskRuntimeRow[]): Promise<void> {
  const candidateRows = taskRows.filter((row) => Boolean(String(row.task_id || '').trim()) && isRuntimePendingState(row.task_state));
  if (candidateRows.length === 0) return;

  const candidateTaskIds = Array.from(new Set(candidateRows.map((row) => String(row.task_id || '').trim()).filter(Boolean)));
  const statusByTaskId = await fetchRuntimeStatusesByTaskId(candidateTaskIds);
  const activeTaskIds = candidateTaskIds.filter((taskId) => {
    const row = candidateRows.find((item) => String(item.task_id || '').trim() === taskId) || null;
    const statusPayload = statusByTaskId[taskId];
    if (!statusPayload) return true;
    return isRuntimePendingState(inferTaskStateFromStatusPayload(statusPayload, row?.task_state || 'QUEUED'));
  });
  if (activeTaskIds.length === 0) return;

  await runWithConcurrency(activeTaskIds, PROJECT_DELETE_TERMINATE_CONCURRENCY, async (taskId) => {
    try {
      await terminateTask(taskId);
      return;
    } catch {
      const currentStatus = await fetchRuntimeStatusesByTaskId([taskId]).catch(
        () => ({} as Record<string, TaskStatusResponse>)
      );
      const statusPayload = currentStatus[taskId];
      const currentState = statusPayload ? inferTaskStateFromStatusPayload(statusPayload, 'QUEUED') : 'REVOKED';
      if (isRuntimePendingState(currentState)) {
        throw new Error(`Task ${taskId} is still active.`);
      }
    }
  });

  const deadline = Date.now() + PROJECT_DELETE_STOP_POLL_TIMEOUT_MS;
  let remainingTaskIds = [...activeTaskIds];
  while (remainingTaskIds.length > 0 && Date.now() < deadline) {
    await sleep(PROJECT_DELETE_STOP_POLL_INTERVAL_MS);
    const currentStatus = await fetchRuntimeStatusesByTaskId(remainingTaskIds).catch(
      () => ({} as Record<string, TaskStatusResponse>)
    );
    remainingTaskIds = remainingTaskIds.filter((taskId) => {
      const statusPayload = currentStatus[taskId];
      if (!statusPayload) return false;
      return isRuntimePendingState(inferTaskStateFromStatusPayload(statusPayload, 'QUEUED'));
    });
  }

  if (remainingTaskIds.length > 0) {
    throw new Error(`Failed to stop ${remainingTaskIds.length} active task(s) before deleting the project.`);
  }
}

function countProjectTaskStates(
  projectIds: string[],
  rows: ProjectTaskRuntimeRow[]
): Map<string, ProjectTaskCounts> {
  const map = new Map<string, ProjectTaskCounts>();
  projectIds.forEach((id) => map.set(id, emptyProjectTaskCounts()));
  rows.forEach((row) => {
    const counts = map.get(row.project_id);
    if (!counts) return;
    counts.total += 1;
    if (row.task_state === 'RUNNING') counts.running += 1;
    else if (row.task_state === 'SUCCESS') counts.success += 1;
    else if (row.task_state === 'FAILURE') counts.failure += 1;
    else if (row.task_state === 'QUEUED') counts.queued += 1;
    else counts.other += 1;
  });
  return map;
}

function cloneProjectTaskCounts(counts: ProjectTaskCounts | null | undefined): ProjectTaskCounts {
  return {
    total: counts?.total || 0,
    running: counts?.running || 0,
    success: counts?.success || 0,
    failure: counts?.failure || 0,
    queued: counts?.queued || 0,
    other: counts?.other || 0
  };
}

function applyRuntimeStatusToProjectRow(
  row: Project,
  statusPayload: TaskStatusResponse | null | undefined
): Project {
  if (!statusPayload) return row;
  const taskState = inferTaskStateFromStatusPayload(statusPayload, row.task_state);

  return {
    ...row,
    task_state: taskState,
    status_text: String(row.status_text || '').trim(),
    error_text: String(row.error_text || '').trim(),
    completed_at: row.completed_at,
    duration_seconds: row.duration_seconds
  };
}

function buildTaskIdSet(values: unknown): Set<string> {
  return new Set(
    (Array.isArray(values) ? values : [])
      .map((value) => String(value || '').trim())
      .filter(Boolean)
  );
}

function overlayProjectCountsWithRuntimeSnapshot(params: {
  projects: Project[];
  baseCountsByProject: Map<string, ProjectTaskCounts>;
  runtimeRows: ProjectTaskRuntimeRow[];
  activeTaskIds: Set<string>;
  queuedTaskIds: Set<string>;
}): Map<string, ProjectTaskCounts> {
  const { projects, baseCountsByProject, runtimeRows, activeTaskIds, queuedTaskIds } = params;
  const nextCountsByProject = new Map<string, ProjectTaskCounts>();
  const projectById = new Map(projects.map((project) => [project.id, project] as const));
  const accessibleTaskRowIdSetByProject = new Map(
    projects
      .filter((project) => String(project.access_scope || 'owner') === 'task_share')
      .map((project) => [
        project.id,
        new Set((project.accessible_task_ids || []).map((item) => String(item || '').trim()).filter(Boolean))
      ] as const)
  );

  projects.forEach((project) => {
    const baseCounts = cloneProjectTaskCounts(baseCountsByProject.get(project.id) || emptyProjectTaskCounts());
    baseCounts.running = 0;
    nextCountsByProject.set(project.id, baseCounts);
  });

  runtimeRows.forEach((row) => {
    const project = projectById.get(row.project_id);
    if (!project) return;
    if (String(project.access_scope || 'owner') === 'task_share') {
      const accessibleTaskRowIds = accessibleTaskRowIdSetByProject.get(project.id);
      if (accessibleTaskRowIds && !accessibleTaskRowIds.has(String(row.id || '').trim())) {
        return;
      }
    }
    const taskId = String(row.task_id || '').trim();
    if (!taskId) return;
    const counts = nextCountsByProject.get(project.id) || emptyProjectTaskCounts();
    if (activeTaskIds.has(taskId)) {
      counts.running += 1;
      if (String(row.task_state || '').trim().toUpperCase() === 'QUEUED' && counts.queued > 0) {
        counts.queued -= 1;
      }
      nextCountsByProject.set(project.id, counts);
      return;
    }
    if (queuedTaskIds.has(taskId) && String(row.task_state || '').trim().toUpperCase() !== 'QUEUED') {
      counts.queued += 1;
      nextCountsByProject.set(project.id, counts);
    }
  });

  return nextCountsByProject;
}

export function useProjects(session: Session | null) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const loadSeqRef = useRef(0);
  const loadInFlightRef = useRef(false);
  const projectsRef = useRef<Project[]>([]);
  const projectCountsRef = useRef<Map<string, ProjectTaskCounts>>(new Map());
  const taskRowsRef = useRef<ProjectTaskRuntimeRow[]>([]);
  const projectListSignatureRef = useRef('');

  useEffect(() => {
    projectsRef.current = projects;
  }, [projects]);

  const readProjectCounts = useCallback(
    (projectId: string, fallback?: ProjectTaskCounts | null): ProjectTaskCounts =>
      cloneProjectTaskCounts(projectCountsRef.current.get(projectId) || fallback || emptyProjectTaskCounts()),
    []
  );

  const persistProjectCountsCache = useCallback((nextProjects: Project[]) => {
    if (typeof window === 'undefined' || !session) return;
    const sessionIdentity = String(session.userId || '').trim() || String(session.username || '').trim().toLowerCase();
    if (!sessionIdentity) return;
    try {
      const countsByProject = Object.fromEntries(
        nextProjects.map((row) => [row.id, cloneProjectTaskCounts(row.task_counts || emptyProjectTaskCounts())] as const)
      );
      window.localStorage.setItem(
        `vbio:projects-runtime-counts:v1:${sessionIdentity}`,
        JSON.stringify({
          saved_at: Date.now(),
          counts_by_project: countsByProject
        })
      );
    } catch {
      // Ignore storage failures and continue with in-memory cache only.
    }
  }, [session]);

  const hydrateProjectCountsCache = useCallback(() => {
    if (typeof window === 'undefined' || !session) return;
    if (projectCountsRef.current.size > 0) return;
    const sessionIdentity = String(session.userId || '').trim() || String(session.username || '').trim().toLowerCase();
    if (!sessionIdentity) return;
    const storageKey = `vbio:projects-runtime-counts:v1:${sessionIdentity}`;
    try {
      const raw = window.localStorage.getItem(storageKey);
      if (!raw) return;
      const parsed = JSON.parse(raw) as {
        saved_at?: number;
        counts_by_project?: Record<string, ProjectTaskCounts | null | undefined>;
      };
      const savedAt = Number(parsed?.saved_at || 0);
      if (!Number.isFinite(savedAt) || Date.now() - savedAt > PROJECT_RUNTIME_COUNTS_CACHE_TTL_MS) {
        window.localStorage.removeItem(storageKey);
        return;
      }
      const countsByProject = parsed?.counts_by_project && typeof parsed.counts_by_project === 'object'
        ? parsed.counts_by_project
        : {};
      projectCountsRef.current = new Map(
        Object.entries(countsByProject)
          .map(([projectId, counts]) => [String(projectId || '').trim(), cloneProjectTaskCounts(counts)] as const)
          .filter(([projectId]) => Boolean(projectId))
      );
    } catch {
      // Ignore malformed cache and continue with fresh data.
    }
  }, [session]);

  const publishProjectsState = useCallback((nextProjects: Project[], nextTaskRows: ProjectTaskRuntimeRow[]) => {
    taskRowsRef.current = nextTaskRows;
    projectsRef.current = nextProjects;
    projectCountsRef.current = new Map(nextProjects.map((row) => [row.id, cloneProjectTaskCounts(row.task_counts)] as const));
    persistProjectCountsCache(nextProjects);
    setProjects((prev) => {
      const prevById = new Map(prev.map((item) => [item.id, item] as const));
      return nextProjects.map((row) => ({
        ...mergeProjectRuntimeFields(row, prevById.get(row.id) || null),
        task_counts: row.task_counts
      }));
    });
  }, [persistProjectCountsCache]);

  const fetchProjectRuntimeOverlay = useCallback(async () => {
    try {
      const runtimeIndex = await getTaskRuntimeIndex();
      const activeTaskIdsRaw = Array.from(buildTaskIdSet(runtimeIndex.active_task_ids));
      const activeStatusByTaskId = activeTaskIdsRaw.length > 0 ? await fetchRuntimeStatusesByTaskId(activeTaskIdsRaw) : {};
      const activeTaskIds = new Set(
        activeTaskIdsRaw.filter((taskId) => {
          const statusPayload = activeStatusByTaskId[taskId];
          if (!statusPayload) return true;
          const state = inferTaskStateFromStatusPayload(statusPayload, 'RUNNING');
          return state === 'RUNNING' || state === 'QUEUED';
        })
      );
      const queuedTaskIds = new Set([
        ...buildTaskIdSet(runtimeIndex.reserved_task_ids),
        ...buildTaskIdSet(runtimeIndex.scheduled_task_ids)
      ]);
      const runtimeTaskIds = Array.from(new Set([...Array.from(activeTaskIds), ...Array.from(queuedTaskIds)]));
      const runtimeRows = runtimeTaskIds.length > 0 ? await listProjectTaskStatesByTaskIds(runtimeTaskIds) : [];
      return {
        runtimeRows,
        activeTaskIds,
        queuedTaskIds,
        activeStatusByTaskId
      };
    } catch {
      return {
        runtimeRows: [] as ProjectTaskRuntimeRow[],
        activeTaskIds: new Set<string>(),
        queuedTaskIds: new Set<string>(),
        activeStatusByTaskId: {} as Record<string, TaskStatusResponse>
      };
    }
  }, []);

  const syncCachedProjectRuntimeState = useCallback(
    async (baseProjects?: Project[]): Promise<boolean> => {
      const cachedProjects = (Array.isArray(baseProjects) && baseProjects.length > 0 ? baseProjects : projectsRef.current).filter(Boolean);
      if (cachedProjects.length === 0) return false;
      const cachedCountsByProject = new Map(
        cachedProjects.map((row) => [row.id, readProjectCounts(row.id, row.task_counts)] as const)
      );
      const runtimeOverlay = await fetchProjectRuntimeOverlay();
      const countsByProject = overlayProjectCountsWithRuntimeSnapshot({
        projects: cachedProjects,
        baseCountsByProject: cachedCountsByProject,
        runtimeRows: runtimeOverlay.runtimeRows,
        activeTaskIds: runtimeOverlay.activeTaskIds,
        queuedTaskIds: runtimeOverlay.queuedTaskIds
      });
      const liveProjects = cachedProjects.map((row) => {
        const activeTaskId = String(row.task_id || '').trim();
        const runtimeProject = applyRuntimeStatusToProjectRow(row, runtimeOverlay.activeStatusByTaskId[activeTaskId] || null);
        return {
          ...runtimeProject,
          task_counts: countsByProject.get(row.id) || row.task_counts || emptyProjectTaskCounts()
        };
      });

      publishProjectsState(liveProjects, runtimeOverlay.runtimeRows);
      return true;
    },
    [fetchProjectRuntimeOverlay, publishProjectsState, readProjectCounts]
  );

  const load = useCallback(async (options?: LoadProjectsOptions) => {
    if (!session) {
      setProjects([]);
      projectsRef.current = [];
      projectCountsRef.current = new Map();
      taskRowsRef.current = [];
      projectListSignatureRef.current = '';
      return;
    }
    if (loadInFlightRef.current) return;
    loadInFlightRef.current = true;
    hydrateProjectCountsCache();
    const loadSeq = ++loadSeqRef.current;
    const silent = Boolean(options?.silent);
    const statusOnly = Boolean(options?.statusOnly);
    const preferBackendStatus = options?.preferBackendStatus !== false;
    if (!silent) {
      setLoading(true);
      setError(null);
    }
    try {
      if (statusOnly) {
        const reusedCachedRuntime = await syncCachedProjectRuntimeState();
        if (reusedCachedRuntime) return;
      }

      const previousProjects = projectsRef.current;
      const shouldPublishProjectsEarly = silent || previousProjects.length > 0;
      const shareLinksPromise = Promise.all([
        listProjectShareLinksForUser(session.userId),
        listProjectTaskShareLinksForUser(session.userId)
      ]);
      const ownedProjects = await listProjects({
        userId: session.userId,
        lightweight: true
      });
      if (loadSeqRef.current !== loadSeq) return;
      const ownedBaseProjects = ownedProjects.map((row) => applyOwnerProjectAccess(row));
      const previousProjectsById = new Map(previousProjects.map((row) => [row.id, row] as const));
      const optimisticOwnedProjects = ownedBaseProjects.map((row) => ({
        ...mergeProjectRuntimeFields(row, previousProjectsById.get(row.id) || null),
        task_counts: readProjectCounts(row.id, previousProjectsById.get(row.id)?.task_counts)
      }));
      if (shouldPublishProjectsEarly) {
        setProjects(optimisticOwnedProjects);
        projectsRef.current = optimisticOwnedProjects;
      }

      const [projectShareLinks, taskShareLinks] = await shareLinksPromise;
      if (loadSeqRef.current !== loadSeq) return;
      const ownedIds = new Set(ownedProjects.map((project) => String(project.id || '').trim()).filter(Boolean));
      const sharedProjectIds = Array.from(
        new Set(
          [
            ...projectShareLinks.map((row) => String(row.project_id || '').trim()),
            ...taskShareLinks.map((row) => String(row.project_id || '').trim())
          ].filter((id) => Boolean(id) && !ownedIds.has(id))
        )
      );
      const sharedProjects = sharedProjectIds.length > 0 ? await listProjectsByIds(sharedProjectIds, { lightweight: true }) : [];
      if (loadSeqRef.current !== loadSeq) return;

      const rows = buildAccessibleProjects(ownedProjects, projectShareLinks, taskShareLinks, sharedProjects);
      const nextProjectSignature = buildProjectListSignature(rows);
      const baseProjects = rows.map((row) => ({
        ...mergeProjectRuntimeFields(row, previousProjectsById.get(row.id) || null),
        task_counts: readProjectCounts(row.id, previousProjectsById.get(row.id)?.task_counts)
      }));
      if (shouldPublishProjectsEarly) {
        setProjects(baseProjects);
        projectsRef.current = baseProjects;
      }

      const canReuseCachedTaskRows = silent && nextProjectSignature === projectListSignatureRef.current;
      projectListSignatureRef.current = nextProjectSignature;
      if (canReuseCachedTaskRows) {
        if (preferBackendStatus) {
          await syncCachedProjectRuntimeState(baseProjects);
        }
        return;
      }

      const fullAccessProjectIds = rows
        .filter((row) => String(row.access_scope || 'owner') !== 'task_share')
        .map((row) => row.id);
      const taskSharedProjects = rows.filter((row) => String(row.access_scope || 'owner') === 'task_share');
      const [fullAccessTaskCounts, taskShareTaskRows] = await Promise.all([
        listProjectTaskCountsByProjects(fullAccessProjectIds),
        listProjectTaskStatesByTaskRowIds(taskSharedProjects.flatMap((row) => row.accessible_task_ids || []))
      ]);
      if (loadSeqRef.current !== loadSeq) return;

      const accessibleTaskRowIdSetByProject = new Map(
        taskSharedProjects.map((row) => [
          row.id,
          new Set((row.accessible_task_ids || []).map((item) => String(item || '').trim()).filter(Boolean))
        ] as const)
      );

      const scopedTaskRows = taskShareTaskRows.filter((row) => {
        const scopedIds = accessibleTaskRowIdSetByProject.get(row.project_id);
        return scopedIds ? scopedIds.has(String(row.id || '').trim()) : true;
      });

      const countsByProject = new Map<string, ProjectTaskCounts>();
      rows.forEach((row) => {
        countsByProject.set(row.id, cloneProjectTaskCounts(fullAccessTaskCounts.get(row.id) || emptyProjectTaskCounts()));
      });
      const taskShareCounts = countProjectTaskStates(
        taskSharedProjects.map((row) => row.id),
        scopedTaskRows.filter((row) => String(rows.find((project) => project.id === row.project_id)?.access_scope || 'owner') === 'task_share')
      );
      taskShareCounts.forEach((counts, projectId) => {
        countsByProject.set(projectId, counts);
      });
      const baseProjectsWithCounts = rows.map((row) => ({
        ...row,
        task_counts: countsByProject.get(row.id) || emptyProjectTaskCounts()
      }));

      const deferRuntimeOverlay = !silent && previousProjects.length === 0;
      if (deferRuntimeOverlay) {
        publishProjectsState(baseProjectsWithCounts, scopedTaskRows);
        setLoading(false);
      }

      const runtimeOverlay = preferBackendStatus
        ? await fetchProjectRuntimeOverlay()
        : {
            runtimeRows: [] as ProjectTaskRuntimeRow[],
            activeTaskIds: new Set<string>(),
            queuedTaskIds: new Set<string>(),
            activeStatusByTaskId: {} as Record<string, TaskStatusResponse>
          };
      if (loadSeqRef.current !== loadSeq) return;
      const liveCountsByProject = overlayProjectCountsWithRuntimeSnapshot({
        projects: rows,
        baseCountsByProject: countsByProject,
        runtimeRows: runtimeOverlay.runtimeRows,
        activeTaskIds: runtimeOverlay.activeTaskIds,
        queuedTaskIds: runtimeOverlay.queuedTaskIds
      });
      const liveProjects = rows.map((row) => {
        const activeTaskId = String(row.task_id || '').trim();
        const runtimeProject = preferBackendStatus
          ? applyRuntimeStatusToProjectRow(row, runtimeOverlay.activeStatusByTaskId[activeTaskId] || null)
          : row;
        return {
          ...runtimeProject,
          task_counts: liveCountsByProject.get(row.id) || emptyProjectTaskCounts()
        };
      });

      publishProjectsState(liveProjects, runtimeOverlay.runtimeRows);
    } catch (e) {
      if (!silent) {
        setError(e instanceof Error ? e.message : 'Failed to load projects.');
      }
    } finally {
      loadInFlightRef.current = false;
      if (!silent) {
        setLoading(false);
      }
    }
  }, [session, fetchProjectRuntimeOverlay, publishProjectsState, readProjectCounts, syncCachedProjectRuntimeState]);

  useEffect(() => {
    void load({ preferBackendStatus: true });
  }, [load]);

  const createProject = useCallback(
    async (input: CreateProjectInput) => {
      if (!session) throw new Error('You must sign in first.');
      const workflow = normalizeWorkflowKey(input.taskType);
      const created = await insertProject({
        user_id: session.userId,
        name: input.name.trim(),
        summary: input.summary?.trim() || '',
        backend: input.backend || 'boltz',
        use_msa: input.useMsa ?? true,
        protein_sequence: input.proteinSequence || '',
        ligand_smiles: input.ligandSmiles || '',
        color_mode: 'default',
        task_type: workflow || DEFAULT_TASK_TYPE,
        task_id: '',
        task_state: 'DRAFT',
        status_text: 'Ready for input',
        error_text: '',
        confidence: {},
        affinity: {},
        structure_name: ''
      });
      setProjects((prev) => [
        {
          ...created,
          access_scope: 'owner',
          access_level: 'owner',
          accessible_task_ids: [],
          editable_task_ids: [],
          task_counts: emptyProjectTaskCounts()
        },
        ...prev
      ]);
      projectCountsRef.current.set(created.id, emptyProjectTaskCounts());
      return created;
    },
    [session]
  );

  const patchProject = useCallback(async (id: string, patch: Partial<Project>) => {
    const updated = await updateProject(id, patch);
    setProjects((prev) => prev.map((p) => (p.id === id ? { ...p, ...updated } : p)));
    return updated;
  }, []);

  const softDeleteProject = useCallback(
    async (id: string) => {
      setError(null);
      try {
        const deletedAt = new Date().toISOString();
        const projectTaskRows = (await listProjectTaskStatesByProjects([id])).filter((row) => row.project_id === id);

        await stopProjectRuntimeTasks(projectTaskRows);
        await deleteProjectTaskSharesByProjectId(id);
        await deleteProjectSharesByProjectId(id);
        await deleteProjectTasksByProjectId(id);
        await patchProject(id, {
          deleted_at: deletedAt,
          task_state: 'DRAFT',
          task_id: '',
          status_text: '',
          error_text: '',
          submitted_at: null,
          completed_at: null,
          duration_seconds: null
        });

        removeProjectInputConfig(id);
        removeProjectUiState(id);
        setProjects((prev) => prev.filter((p) => p.id !== id));
        projectCountsRef.current.delete(id);
      } catch (error) {
        setError(error instanceof Error ? error.message : 'Failed to delete project.');
      }
    },
    [patchProject]
  );

  return {
    projects,
    loading,
    error,
    search,
    setSearch,
    load,
    createProject,
    patchProject,
    softDeleteProject
  };
}
