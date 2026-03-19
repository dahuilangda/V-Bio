import { useCallback, useEffect, useRef, useState } from 'react';
import type { Project, ProjectTaskCounts, Session, TaskStatusResponse } from '../types/models';
import {
  deleteProjectSharesByProjectId,
  deleteProjectTaskSharesByProjectId,
  deleteProjectTasksByProjectId,
  insertProject,
  listAccessibleProjects,
  listProjectTaskStatesByProjects,
  listProjectTaskStatesByTaskRowIds,
  updateProject,
  updateProjectTask,
  type ProjectTaskRuntimeRow
} from '../api/supabaseLite';
import { getTaskStatuses, terminateTask } from '../api/backendApi';
import { removeProjectInputConfig, removeProjectUiState } from '../utils/projectInputs';
import { canEditProject } from '../utils/accessControl';
import { inferTaskStateFromStatusPayload, readTaskRuntimeStatusText } from '../utils/taskRuntime';
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
const TASK_STATUS_POLL_CHUNK_SIZE = 3;
const PROJECT_DELETE_TERMINATE_CONCURRENCY = 6;
const PROJECT_DELETE_STOP_POLL_INTERVAL_MS = 700;
const PROJECT_DELETE_STOP_POLL_TIMEOUT_MS = 12000;

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

function canPersistProjectWrites(project: Project | null | undefined): boolean {
  return canEditProject(project);
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

  const byTaskId: Record<string, TaskStatusResponse> = {};
  for (let i = 0; i < normalizedTaskIds.length; i += Math.max(TASK_STATUS_POLL_CHUNK_SIZE, 64)) {
    const chunk = normalizedTaskIds.slice(i, i + Math.max(TASK_STATUS_POLL_CHUNK_SIZE, 64));
    try {
      Object.assign(byTaskId, await getTaskStatuses(chunk));
    } catch {
      // Keep partial successes from other chunks.
    }
  }

  return byTaskId;
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

function applyRuntimeStatusToTaskRow(
  row: ProjectTaskRuntimeRow,
  statusPayload: TaskStatusResponse | null | undefined
): ProjectTaskRuntimeRow {
  if (!statusPayload) return row;
  const taskState = inferTaskStateFromStatusPayload(statusPayload, row.task_state);
  const statusText = readTaskRuntimeStatusText(statusPayload);
  const errorText = taskState === 'FAILURE' ? statusText : '';
  const terminal = taskState === 'SUCCESS' || taskState === 'FAILURE' || taskState === 'REVOKED';
  const completedAt = terminal ? row.completed_at || new Date().toISOString() : null;
  const durationSeconds =
    terminal && row.submitted_at
      ? Math.max(0, (new Date(completedAt || Date.now()).getTime() - new Date(row.submitted_at).getTime()) / 1000)
      : null;

  return {
    ...row,
    task_state: taskState,
    status_text: statusText,
    error_text: errorText,
    completed_at: completedAt,
    duration_seconds: Number.isFinite(durationSeconds as number) ? durationSeconds : null
  };
}

function applyRuntimeStatusToProjectRow(
  row: Project,
  statusPayload: TaskStatusResponse | null | undefined
): Project {
  if (!statusPayload) return row;
  const taskState = inferTaskStateFromStatusPayload(statusPayload, row.task_state);
  const statusText = readTaskRuntimeStatusText(statusPayload);
  const errorText = taskState === 'FAILURE' ? statusText : '';
  const terminal = taskState === 'SUCCESS' || taskState === 'FAILURE' || taskState === 'REVOKED';
  const completedAt = terminal ? row.completed_at || new Date().toISOString() : null;
  const durationSeconds =
    terminal && row.submitted_at
      ? Math.max(0, (new Date(completedAt || Date.now()).getTime() - new Date(row.submitted_at).getTime()) / 1000)
      : null;

  return {
    ...row,
    task_state: taskState,
    status_text: statusText,
    error_text: errorText,
    completed_at: completedAt,
    duration_seconds: Number.isFinite(durationSeconds as number) ? durationSeconds : null
  };
}

function sameProjectRuntime(a: Project, b: Project): boolean {
  return (
    a.task_id === b.task_id &&
    a.task_state === b.task_state &&
    a.status_text === b.status_text &&
    a.error_text === b.error_text &&
    a.submitted_at === b.submitted_at &&
    a.completed_at === b.completed_at &&
    a.duration_seconds === b.duration_seconds
  );
}

function sameTaskRuntimeRow(a: ProjectTaskRuntimeRow, b: ProjectTaskRuntimeRow): boolean {
  return (
    a.task_state === b.task_state &&
    a.status_text === b.status_text &&
    a.error_text === b.error_text &&
    a.submitted_at === b.submitted_at &&
    a.completed_at === b.completed_at &&
    a.duration_seconds === b.duration_seconds
  );
}

export function useProjects(session: Session | null) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const loadSeqRef = useRef(0);
  const loadInFlightRef = useRef(false);

  const load = useCallback(async (options?: LoadProjectsOptions) => {
    if (!session) {
      setProjects([]);
      return;
    }
    if (loadInFlightRef.current) return;
    loadInFlightRef.current = true;
    const loadSeq = ++loadSeqRef.current;
    const silent = Boolean(options?.silent);
    const preferBackendStatus = Boolean(options?.preferBackendStatus);
    if (!silent) {
      setLoading(true);
      setError(null);
    }
    try {
      const rows = await listAccessibleProjects(session.userId, {
        lightweight: true
      });
      if (loadSeqRef.current !== loadSeq) return;

      const fullAccessProjectIds = rows
        .filter((row) => String(row.access_scope || 'owner') !== 'task_share')
        .map((row) => row.id);
      const taskSharedProjects = rows.filter((row) => String(row.access_scope || 'owner') === 'task_share');
      const [fullAccessTaskRows, taskShareTaskRows] = await Promise.all([
        listProjectTaskStatesByProjects(fullAccessProjectIds),
        listProjectTaskStatesByTaskRowIds(taskSharedProjects.flatMap((row) => row.accessible_task_ids || []))
      ]);
      if (loadSeqRef.current !== loadSeq) return;

      const accessibleTaskRowIdSetByProject = new Map(
        taskSharedProjects.map((row) => [
          row.id,
          new Set((row.accessible_task_ids || []).map((item) => String(item || '').trim()).filter(Boolean))
        ] as const)
      );

      const scopedTaskRows = [
        ...fullAccessTaskRows,
        ...taskShareTaskRows.filter((row) => {
          const scopedIds = accessibleTaskRowIdSetByProject.get(row.project_id);
          return scopedIds ? scopedIds.has(String(row.id || '').trim()) : true;
        })
      ];

      const runtimeTaskIds = preferBackendStatus
        ? scopedTaskRows
            .filter((row) => {
              const taskId = String(row.task_id || '').trim();
              const taskState = String(row.task_state || '').trim().toUpperCase();
              return Boolean(taskId) && (taskState === 'QUEUED' || taskState === 'RUNNING');
            })
            .map((row) => String(row.task_id || '').trim())
        : [];
      const statusByTaskId = preferBackendStatus ? await fetchRuntimeStatusesByTaskId(runtimeTaskIds) : {};
      if (loadSeqRef.current !== loadSeq) return;

      const liveTaskRows = scopedTaskRows.map((row) =>
        applyRuntimeStatusToTaskRow(row, statusByTaskId[String(row.task_id || '').trim()] || null)
      );
      const countsByProject = countProjectTaskStates(
        rows.map((row) => row.id).filter(Boolean),
        liveTaskRows
      );
      const liveProjects = rows.map((row) => {
        const activeTaskId = String(row.task_id || '').trim();
        const runtimeProject = preferBackendStatus
          ? applyRuntimeStatusToProjectRow(row, statusByTaskId[activeTaskId] || null)
          : row;
        return {
          ...runtimeProject,
          task_counts: countsByProject.get(row.id) || emptyProjectTaskCounts()
        };
      });

      if (preferBackendStatus) {
        const originalProjectById = new Map(rows.map((row) => [row.id, row] as const));
        const originalTaskRowById = new Map(scopedTaskRows.map((row) => [row.id, row] as const));

        for (const liveTaskRow of liveTaskRows) {
          const sourceTaskRow = originalTaskRowById.get(liveTaskRow.id);
          if (!sourceTaskRow || sameTaskRuntimeRow(sourceTaskRow, liveTaskRow)) continue;
          const parentProject = originalProjectById.get(liveTaskRow.project_id) || null;
          if (!canPersistProjectWrites(parentProject)) continue;
          const taskPatch = {
            task_state: liveTaskRow.task_state,
            status_text: liveTaskRow.status_text,
            error_text: liveTaskRow.error_text,
            completed_at: liveTaskRow.completed_at,
            duration_seconds: liveTaskRow.duration_seconds
          };
          void updateProjectTask(liveTaskRow.id, taskPatch, { minimalReturn: true }).catch(() => undefined);
        }

        for (const liveProject of liveProjects) {
          const sourceProject = originalProjectById.get(liveProject.id);
          if (!sourceProject || sameProjectRuntime(sourceProject, liveProject)) continue;
          if (!canPersistProjectWrites(sourceProject)) continue;
          const projectPatch = {
            task_state: liveProject.task_state,
            status_text: liveProject.status_text,
            error_text: liveProject.error_text,
            completed_at: liveProject.completed_at,
            duration_seconds: liveProject.duration_seconds
          };
          void updateProject(liveProject.id, projectPatch).catch(() => undefined);
        }
      }

      if (loadSeqRef.current !== loadSeq) return;
      setProjects((prev) => {
        const prevById = new Map(prev.map((item) => [item.id, item] as const));
        return liveProjects.map((row) => ({
          ...mergeProjectRuntimeFields(row, prevById.get(row.id) || null),
          task_counts: row.task_counts
        }));
      });
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
  }, [session]);

  useEffect(() => {
    void load();
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
