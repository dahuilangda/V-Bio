import { normalizeProjectRow, normalizeShareAccessLevel, toEffectiveAccessLevel } from './shared';
import { EffectiveAccessLevel, Project, ProjectAccessScope, ShareAccessLevel } from '../../types/models';
import { _requireSingleRow, buildInFilter, request } from './rest';



// 项目 CRUD 与访问信息查询。

interface ListProjectsOptions {
  userId?: string;
  includeDeleted?: boolean;
  search?: string;
  lightweight?: boolean;
}

export async function listProjects(options: ListProjectsOptions = {}): Promise<Project[]> {
  const projectSelect = options.lightweight === false
    ? '*'
    : [
        'id',
        'user_id',
        'name',
        'summary',
        'backend',
        'use_msa',
        'color_mode',
        'task_type',
        'task_id',
        'task_state',
        'status_text',
        'error_text',
        'submitted_at',
        'completed_at',
        'duration_seconds',
        'structure_name',
        'created_at',
        'updated_at',
        'deleted_at'
      ].join(',');
  const query: Record<string, string | undefined> = {
    select: projectSelect,
    order: 'updated_at.desc'
  };

  if (!options.includeDeleted) {
    query.deleted_at = 'is.null';
  }
  if (options.userId) {
    query.user_id = `eq.${options.userId}`;
  }
  if (options.search?.trim()) {
    query.name = `ilike.*${options.search.trim()}*`;
  }

  const rows = await request<Array<Partial<Project>>>('/projects', undefined, query);
  return rows.map((row) => normalizeProjectRow(row));
}

interface GetProjectByIdOptions {
  lightweight?: boolean;
}

export async function getProjectById(projectId: string, options: GetProjectByIdOptions = {}): Promise<Project | null> {
  const selectFields = options.lightweight
    ? [
        'id',
        'user_id',
        'name',
        'summary',
        'backend',
        'use_msa',
        'color_mode',
        'task_type',
        'task_id',
        'task_state',
        'status_text',
        'error_text',
        'submitted_at',
        'completed_at',
        'duration_seconds',
        'structure_name',
        'created_at',
        'updated_at',
        'deleted_at'
      ].join(',')
    : '*';
  const rows = await request<Project[]>('/projects', undefined, {
    select: selectFields,
    id: `eq.${projectId}`,
    limit: '1'
  });
  const row = rows[0];
  if (!row) return null;
  return normalizeProjectRow(row as Partial<Project>);
}

export async function listProjectsByIds(projectIds: string[], options: GetProjectByIdOptions = {}): Promise<Project[]> {
  const idFilter = buildInFilter(projectIds);
  if (!idFilter) return [];
  const selectFields = options.lightweight
    ? [
        'id',
        'user_id',
        'name',
        'summary',
        'backend',
        'use_msa',
        'color_mode',
        'task_type',
        'task_id',
        'task_state',
        'status_text',
        'error_text',
        'submitted_at',
        'completed_at',
        'duration_seconds',
        'structure_name',
        'created_at',
        'updated_at',
        'deleted_at'
      ].join(',')
    : '*';
  const rows = await request<Array<Partial<Project>>>('/projects', undefined, {
    select: selectFields,
    id: idFilter,
    deleted_at: 'is.null',
    order: 'updated_at.desc'
  });
  return rows.map((row) => normalizeProjectRow(row));
}

export async function listProjectShareLinksForUser(userId: string): Promise<Array<{ project_id: string; access_level: ShareAccessLevel }>> {
  const normalizedUserId = String(userId || '').trim();
  if (!normalizedUserId) return [];
  const rows = await request<Array<{ project_id: string; access_level?: string }>>('/project_shares', undefined, {
    select: '*',
    user_id: `eq.${normalizedUserId}`
  });
  return rows.map((row) => ({
    project_id: row.project_id,
    access_level: normalizeShareAccessLevel(row.access_level)
  }));
}

export async function listProjectTaskShareLinksForUser(
  userId: string
): Promise<Array<{ project_id: string; project_task_id: string; access_level: ShareAccessLevel }>> {
  const normalizedUserId = String(userId || '').trim();
  if (!normalizedUserId) return [];
  const rows = await request<Array<{ project_id: string; project_task_id: string; access_level?: string }>>('/project_task_shares', undefined, {
    select: '*',
    user_id: `eq.${normalizedUserId}`
  });
  return rows.map((row) => ({
    project_id: row.project_id,
    project_task_id: row.project_task_id,
    access_level: normalizeShareAccessLevel(row.access_level)
  }));
}

export async function getProjectAccessInfo(
  projectId: string,
  userId: string,
  ownerUserId?: string | null
): Promise<{ scope: ProjectAccessScope | null; accessLevel: EffectiveAccessLevel | null; taskIds: string[]; editableTaskIds: string[] }> {
  const normalizedProjectId = String(projectId || '').trim();
  const normalizedUserId = String(userId || '').trim();
  if (!normalizedProjectId || !normalizedUserId) {
    return { scope: null, accessLevel: null, taskIds: [], editableTaskIds: [] };
  }
  if (String(ownerUserId || '').trim() === normalizedUserId) {
    return { scope: 'owner', accessLevel: 'owner', taskIds: [], editableTaskIds: [] };
  }
  const [projectShareRows, taskShareRows] = await Promise.all([
    request<Array<{ project_id: string; access_level?: string }>>('/project_shares', undefined, {
      select: '*',
      project_id: `eq.${normalizedProjectId}`,
      user_id: `eq.${normalizedUserId}`,
      limit: '1'
    }),
    request<Array<{ project_task_id: string; access_level?: string }>>('/project_task_shares', undefined, {
      select: '*',
      project_id: `eq.${normalizedProjectId}`,
      user_id: `eq.${normalizedUserId}`,
      order: 'created_at.asc'
    })
  ]);
  const editableTaskIds = Array.from(
    new Set(
      taskShareRows
        .filter((row) => normalizeShareAccessLevel(row.access_level) === 'editor')
        .map((row) => String(row.project_task_id || '').trim())
        .filter(Boolean)
    )
  );
  if (projectShareRows.length > 0) {
    return {
      scope: 'project_share',
      accessLevel: toEffectiveAccessLevel(projectShareRows[0]?.access_level),
      taskIds: [],
      editableTaskIds
    };
  }
  const taskIds = Array.from(
    new Set(taskShareRows.map((row) => String(row.project_task_id || '').trim()).filter(Boolean))
  );
  if (taskIds.length > 0) {
    return {
      scope: 'task_share',
      accessLevel: editableTaskIds.length > 0 ? 'editor' : 'viewer',
      taskIds,
      editableTaskIds
    };
  }
  return { scope: null, accessLevel: null, taskIds: [], editableTaskIds: [] };
}

export async function insertProject(input: Partial<Project>): Promise<Project> {
  const rows = await request<Project[]>(
    '/projects',
    {
      method: 'POST',
      headers: {
        Prefer: 'return=representation'
      },
      body: JSON.stringify(input)
    },
    {
      select: '*'
    }
  );
  return _requireSingleRow(rows);
}

export async function updateProject(projectId: string, patch: Partial<Project>): Promise<Project> {
  const rows = await request<Project[]>(
    '/projects',
    {
      method: 'PATCH',
      headers: {
        Prefer: 'return=representation'
      },
      body: JSON.stringify(patch)
    },
    {
      id: `eq.${projectId}`,
      select: '*'
    }
  );
  return _requireSingleRow(rows);
}
