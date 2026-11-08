import { ProjectTask, ProjectTaskCounts, TaskState } from '../../types/models';
import { sanitizeProjectTaskWritePayload } from './taskDetail';
import { _requireSingleRow, buildInFilter, request } from './rest';



// 任务行写入与运行时/计数批量查询。

export interface ProjectTaskRuntimeRow {
  id: string;
  project_id: string;
  task_id: string;
  task_state: TaskState;
  status_text: string;
  error_text: string;
  submitted_at: string | null;
  completed_at: string | null;
  duration_seconds: number | null;
}

export interface ProjectLeadOptChildTaskRow {
  project_task_id: string;
  project_id: string;
  prediction_key: string;
  child_task_id: string;
  child_task_state: string;
}

export async function listProjectTaskStatesByProjects(projectIds: string[]): Promise<ProjectTaskRuntimeRow[]> {
  const normalizedIds = Array.from(new Set(projectIds.map((id) => id.trim()).filter(Boolean)));
  if (normalizedIds.length === 0) return [];

  // Keep query strings bounded; very large `in (...)` filters can time out or exceed URL limits.
  const chunkSize = 150;
  const chunks: string[][] = [];
  for (let i = 0; i < normalizedIds.length; i += chunkSize) {
    chunks.push(normalizedIds.slice(i, i + chunkSize));
  }

  const results = await Promise.all(
    chunks.map((chunk) =>
      request<ProjectTaskRuntimeRow[]>('/project_tasks', undefined, {
        select: 'id,project_id,task_id,task_state,status_text,error_text,submitted_at,completed_at,duration_seconds',
        project_id: `in.(${chunk.join(',')})`
      })
    )
  );
  return results.flat();
}

export async function listProjectTaskCountsByProjects(projectIds: string[]): Promise<Map<string, ProjectTaskCounts>> {
  const normalizedIds = Array.from(new Set(projectIds.map((id) => id.trim()).filter(Boolean)));
  if (normalizedIds.length === 0) return new Map();

  const chunkSize = 150;
  const chunks: string[][] = [];
  for (let i = 0; i < normalizedIds.length; i += chunkSize) {
    chunks.push(normalizedIds.slice(i, i + chunkSize));
  }

  const rows = (
    await Promise.all(
      chunks.map((chunk) =>
        request<
          Array<{
            project_id: string;
            total_count?: number | string | null;
            running_count?: number | string | null;
            success_count?: number | string | null;
            failure_count?: number | string | null;
            queued_count?: number | string | null;
            other_count?: number | string | null;
          }>
        >('/project_task_counts', undefined, {
          select: 'project_id,total_count,running_count,success_count,failure_count,queued_count,other_count',
          project_id: `in.(${chunk.join(',')})`
        })
      )
    )
  ).flat();

  const readCount = (value: unknown): number => {
    const parsed = typeof value === 'number' ? value : typeof value === 'string' ? Number(value) : Number.NaN;
    return Number.isFinite(parsed) ? Math.max(0, parsed) : 0;
  };

  return new Map(
    rows
      .map((row) => [
        String(row.project_id || '').trim(),
        {
          total: readCount(row.total_count),
          running: readCount(row.running_count),
          success: readCount(row.success_count),
          failure: readCount(row.failure_count),
          queued: readCount(row.queued_count),
          other: readCount(row.other_count)
        }
      ] as const)
      .filter(([projectId]) => Boolean(projectId))
  );
}

export async function listProjectTaskStatesByTaskRowIds(taskRowIds: string[]): Promise<ProjectTaskRuntimeRow[]> {
  const idFilter = buildInFilter(taskRowIds);
  if (!idFilter) return [];
  return request<ProjectTaskRuntimeRow[]>('/project_tasks', undefined, {
    select: 'id,project_id,task_id,task_state,status_text,error_text,submitted_at,completed_at,duration_seconds',
    id: idFilter
  });
}

export async function listProjectTaskStatesByTaskIds(taskIds: string[]): Promise<ProjectTaskRuntimeRow[]> {
  const taskIdFilter = buildInFilter(taskIds);
  if (!taskIdFilter) return [];
  return request<ProjectTaskRuntimeRow[]>('/project_tasks', undefined, {
    select: 'id,project_id,task_id,task_state,status_text,error_text,submitted_at,completed_at,duration_seconds',
    task_id: taskIdFilter
  });
}

export async function listLeadOptChildTaskRowsByTaskIds(taskIds: string[]): Promise<ProjectLeadOptChildTaskRow[]> {
  const taskIdFilter = buildInFilter(taskIds);
  if (!taskIdFilter) return [];
  return request<ProjectLeadOptChildTaskRow[]>('/project_lead_opt_child_tasks', undefined, {
    select: 'project_task_id,project_id,prediction_key,child_task_id,child_task_state',
    child_task_id: taskIdFilter
  });
}

export async function insertProjectTask(input: Partial<ProjectTask>): Promise<ProjectTask> {
  const sanitized = sanitizeProjectTaskWritePayload(input);
  const rows = await request<ProjectTask[]>(
    '/project_tasks',
    {
      method: 'POST',
      headers: {
        Prefer: 'return=representation'
      },
      body: JSON.stringify(sanitized)
    },
    {
      select: '*'
    }
  );
  return _requireSingleRow(rows);
}

export async function updateProjectTask(
  taskRowId: string,
  patch: Partial<ProjectTask>,
  options?: { minimalReturn?: boolean; select?: string }
): Promise<ProjectTask> {
  const sanitized = sanitizeProjectTaskWritePayload(patch);
  const preferHeader = options?.minimalReturn ? 'return=minimal' : 'return=representation';
  const rows = await request<ProjectTask[]>(
    '/project_tasks',
    {
      method: 'PATCH',
      headers: {
        Prefer: preferHeader
      },
      body: JSON.stringify(sanitized)
    },
    {
      id: `eq.${taskRowId}`,
      ...(options?.minimalReturn ? {} : { select: options?.select || '*' })
    }
  );
  if (options?.minimalReturn) {
    return {
      id: taskRowId,
      ...sanitized,
      updated_at: new Date().toISOString()
    } as ProjectTask;
  }
  return _requireSingleRow(rows);
}

export async function findProjectTaskByTaskId(taskId: string, projectId?: string): Promise<ProjectTask | null> {
  const normalizedTaskId = taskId.trim();
  if (!normalizedTaskId) return null;
  const query: Record<string, string | undefined> = {
    select: '*',
    task_id: `eq.${normalizedTaskId}`,
    order: 'created_at.desc',
    limit: '1'
  };
  if (projectId?.trim()) {
    query.project_id = `eq.${projectId.trim()}`;
  }
  const rows = await request<ProjectTask[]>('/project_tasks', undefined, query);
  return rows[0] || null;
}

export async function deleteProjectTask(taskRowId: string): Promise<void> {
  await request<ProjectTask[]>(
    '/project_tasks',
    {
      method: 'DELETE',
      headers: {
        Prefer: 'return=minimal'
      }
    },
    {
      id: `eq.${taskRowId}`
    }
  );
}

export async function deleteProjectTasksByProjectId(projectId: string): Promise<void> {
  const normalizedProjectId = String(projectId || '').trim();
  if (!normalizedProjectId) return;
  await request<ProjectTask[]>(
    '/project_tasks',
    {
      method: 'DELETE',
      headers: {
        Prefer: 'return=minimal'
      }
    },
    {
      project_id: `eq.${normalizedProjectId}`
    }
  );
}

