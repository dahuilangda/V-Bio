import { useCallback, useEffect, useRef, useState } from 'react';
import type { Project, ProjectTask, ProjectTaskCounts, Session, TaskState } from '../types/models';
import {
  insertProject,
  listProjectTaskStatesByProjects,
  listProjects,
  updateProject,
  updateProjectTaskByTaskId
} from '../api/supabaseLite';
import { getTaskStatusBatch } from '../api/backendApi';
import { removeProjectInputConfig, removeProjectUiState } from '../utils/projectInputs';
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

function mapTaskState(raw: string): Project['task_state'] {
  const normalized = raw.toUpperCase();
  if (normalized === 'SUCCESS') return 'SUCCESS';
  if (normalized === 'FAILURE') return 'FAILURE';
  if (normalized === 'REVOKED') return 'REVOKED';
  if (normalized === 'PENDING' || normalized === 'RECEIVED' || normalized === 'RETRY') return 'QUEUED';
  if (normalized === 'STARTED' || normalized === 'RUNNING' || normalized === 'PROGRESS') return 'RUNNING';
  return 'QUEUED';
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

function readStatusText(status: { info?: Record<string, unknown>; state: string }): string {
  if (!status.info) return status.state;
  const s1 = status.info.status;
  const s2 = status.info.message;
  if (typeof s1 === 'string' && s1.trim()) return s1;
  if (typeof s2 === 'string' && s2.trim()) return s2;
  return status.state;
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

function fallbackCountsFromState(state: TaskState, hasTaskId: boolean): ProjectTaskCounts {
  const counts = emptyProjectTaskCounts();
  if (!hasTaskId && state === 'DRAFT') return counts;
  counts.total = 1;
  if (state === 'RUNNING') counts.running = 1;
  else if (state === 'SUCCESS') counts.success = 1;
  else if (state === 'FAILURE') counts.failure = 1;
  else if (state === 'QUEUED') counts.queued = 1;
  else counts.other = 1;
  return counts;
}

function countProjectTaskStates(
  projectIds: string[],
  rows: Array<{ project_id: string; task_id: string; task_state: TaskState }>
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

function stateBucket(state: TaskState): keyof Pick<ProjectTaskCounts, 'running' | 'success' | 'failure' | 'queued' | 'other'> {
  if (state === 'RUNNING') return 'running';
  if (state === 'SUCCESS') return 'success';
  if (state === 'FAILURE') return 'failure';
  if (state === 'QUEUED') return 'queued';
  return 'other';
}

function withFallbackCounts(rows: Project[], existingCounts?: Map<string, ProjectTaskCounts | undefined>): Project[] {
  return rows.map((row) => ({
    ...row,
    task_counts: existingCounts?.get(row.id) || fallbackCountsFromState(row.task_state, Boolean(row.task_id))
  }));
}

export function useProjects(session: Session | null) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const loadSeqRef = useRef(0);

  const load = useCallback(async (options?: LoadProjectsOptions) => {
    const loadSeq = ++loadSeqRef.current;
    const silent = Boolean(options?.silent);
    const statusOnly = Boolean(options?.statusOnly);
    const preferBackendStatus = Boolean(options?.preferBackendStatus);

    if (!session) {
      setProjects([]);
      return;
    }
    if (!silent) {
      setLoading(true);
      setError(null);
    }
    try {
      let rows = await listProjects({
        userId: session.userId,
        lightweight: true
      });

      if (preferBackendStatus) {
        const runtimeRows = rows.filter((row) => Boolean(row.task_id) && (row.task_state === 'QUEUED' || row.task_state === 'RUNNING'));
        if (runtimeRows.length > 0) {
          let statusByTaskId: Record<string, { task_id: string; state: string; info?: Record<string, unknown> }> = {};
          try {
            statusByTaskId = await getTaskStatusBatch(runtimeRows.map((row) => String(row.task_id || '').trim()));
          } catch {
            statusByTaskId = {};
          }
          const patchById = new Map<string, Partial<Project>>();
          const taskPatches: Array<{ projectId: string; taskId: string; patch: Partial<ProjectTask> }> = [];

          runtimeRows.forEach((source) => {
            const sourceTaskId = String(source.task_id || '').trim();
            if (!sourceTaskId) return;
            const statusPayload = statusByTaskId[sourceTaskId];
            if (!statusPayload) return;
            const taskState = mapTaskState(statusPayload.state);
            const statusText = readStatusText(statusPayload);
            const errorText = taskState === 'FAILURE' ? statusText : '';
            const terminal = taskState === 'SUCCESS' || taskState === 'FAILURE' || taskState === 'REVOKED';
            const completedAt = terminal ? source.completed_at || new Date().toISOString() : source.completed_at;
            const durationSeconds =
              terminal && source.submitted_at
                ? Math.max(0, (new Date(completedAt || Date.now()).getTime() - new Date(source.submitted_at).getTime()) / 1000)
                : source.duration_seconds;

            if (
              source.task_state === taskState &&
              (source.status_text || '') === statusText &&
              (source.error_text || '') === errorText &&
              source.completed_at === completedAt &&
              source.duration_seconds === durationSeconds
            ) {
              return;
            }

            const projectPatch: Partial<Project> = {
              task_state: taskState,
              status_text: statusText,
              error_text: errorText,
              completed_at: completedAt,
              duration_seconds: Number.isFinite(durationSeconds as number) ? durationSeconds : null
            };
            const taskPatch: Partial<ProjectTask> = {
              task_state: taskState,
              status_text: statusText,
              error_text: errorText,
              completed_at: completedAt,
              duration_seconds: Number.isFinite(durationSeconds as number) ? durationSeconds : null
            };
            patchById.set(source.id, projectPatch);
            taskPatches.push({
              projectId: source.id,
              taskId: source.task_id,
              patch: taskPatch
            });
          });

          if (patchById.size > 0) {
            rows = rows.map((row) => (patchById.has(row.id) ? { ...row, ...patchById.get(row.id)! } : row));
            for (const [projectId, patch] of patchById.entries()) {
              void updateProject(projectId, patch).catch(() => undefined);
            }
            for (const taskPatch of taskPatches) {
              void updateProjectTaskByTaskId(taskPatch.taskId, taskPatch.patch, taskPatch.projectId).catch(() => undefined);
            }
          }
        }
      }
      if (loadSeqRef.current !== loadSeq) return;

      if (!statusOnly) {
        setProjects((prev) => {
          const prevById = new Map(prev.map((item) => [item.id, item] as const));
          const mergedRows = rows.map((row) => mergeProjectRuntimeFields(row, prevById.get(row.id) || null));
          const countsByProjectId = new Map(prev.map((item) => [item.id, item.task_counts]));
          return withFallbackCounts(mergedRows, countsByProjectId);
        });
      }

      const rowById = new Map(rows.map((row) => [row.id, row]));
      if (statusOnly) {
        if (loadSeqRef.current !== loadSeq) return;
        setProjects((prev) =>
          prev.map((project) => {
            const next = rowById.get(project.id);
            if (!next) return project;
            const mergedNext = mergeProjectRuntimeFields(next, project);

            if (
              project.task_state === mergedNext.task_state &&
              project.task_id === mergedNext.task_id &&
              project.status_text === mergedNext.status_text &&
              project.error_text === mergedNext.error_text &&
              project.submitted_at === mergedNext.submitted_at &&
              project.completed_at === mergedNext.completed_at &&
              project.duration_seconds === mergedNext.duration_seconds
            ) {
              return project;
            }

            return {
              ...project,
              task_state: mergedNext.task_state,
              task_id: mergedNext.task_id,
              status_text: mergedNext.status_text,
              error_text: mergedNext.error_text,
              submitted_at: mergedNext.submitted_at,
              completed_at: mergedNext.completed_at,
              duration_seconds: mergedNext.duration_seconds
            };
          })
        );
        return;
      }

      const projectIds = rows.map((row) => row.id).filter(Boolean);
      if (projectIds.length === 0) return;

      void (async () => {
        try {
          const taskStates = await listProjectTaskStatesByProjects(projectIds);
          const countsByProject = countProjectTaskStates(projectIds, taskStates);
          const taskStateByProjectAndTaskId = new Map(taskStates.map((item) => [`${item.project_id}:${item.task_id}`, item.task_state]));
          rows.forEach((projectRow) => {
            const activeTaskId = String(projectRow.task_id || '').trim();
            if (!activeTaskId) return;
            const counts = countsByProject.get(projectRow.id);
            if (!counts) return;
            const rowState = taskStateByProjectAndTaskId.get(`${projectRow.id}:${activeTaskId}`);
            if (!rowState || rowState === projectRow.task_state) return;
            const sourceBucket = stateBucket(rowState);
            const targetBucket = stateBucket(projectRow.task_state);
            if (sourceBucket === targetBucket) return;
            counts[sourceBucket] = Math.max(0, counts[sourceBucket] - 1);
            counts[targetBucket] += 1;
          });
          const rowsWithCounts = rows.map((row) => {
            const counted = countsByProject.get(row.id);
            if (!counted || counted.total <= 0) {
              return {
                ...row,
                task_counts: fallbackCountsFromState(row.task_state, Boolean(row.task_id))
              };
            }
            return {
              ...row,
              task_counts: counted
            };
          });
          if (loadSeqRef.current !== loadSeq) return;
          setProjects(rowsWithCounts);
        } catch {
          // Keep fallback counts if heavy count query fails.
        }
      })();
    } catch (e) {
      if (!silent) {
        setError(e instanceof Error ? e.message : 'Failed to load projects.');
      }
    } finally {
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
      await patchProject(id, {
        deleted_at: new Date().toISOString(),
        task_state: 'DRAFT',
        task_id: ''
      });
      removeProjectInputConfig(id);
      removeProjectUiState(id);
      setProjects((prev) => prev.filter((p) => p.id !== id));
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
