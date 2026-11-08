import { normalizeShareAccessLevel } from './shared';
import { listProjectsByIds } from './projects';
import { ProjectShareRecord, ProjectTaskShareRecord } from '../../types/models';
import { listProjectTaskMetaByIds, listUsersByIds, request } from './rest';



// 收/发分享列表聚合查询。

export async function listIncomingProjectShares(userId: string): Promise<ProjectShareRecord[]> {
  const normalizedUserId = String(userId || '').trim();
  if (!normalizedUserId) return [];
  const rows = await request<Array<{
    id: string;
    project_id: string;
    user_id: string;
    granted_by_user_id: string | null;
    access_level?: string;
    created_at: string;
    updated_at: string;
  }>>('/project_shares', undefined, {
    select: '*',
    user_id: `eq.${normalizedUserId}`,
    order: 'created_at.desc'
  });
  const projectIds = Array.from(new Set(rows.map((row) => String(row.project_id || '').trim()).filter(Boolean)));
  const userIds = Array.from(
    new Set(
      rows.flatMap((row) => [String(row.user_id || '').trim(), String(row.granted_by_user_id || '').trim()]).filter(Boolean)
    )
  );
  const [projects, users] = await Promise.all([
    listProjectsByIds(projectIds, { lightweight: true }),
    listUsersByIds(userIds)
  ]);
  const projectById = new Map(projects.map((project) => [project.id, project] as const));
  const userById = new Map(users.map((user) => [user.id, user] as const));
  return rows.map((row) => {
    const project = projectById.get(String(row.project_id || '').trim());
    const target = userById.get(String(row.user_id || '').trim());
    const grantedBy = userById.get(String(row.granted_by_user_id || '').trim());
    return {
      ...row,
      access_level: normalizeShareAccessLevel(row.access_level),
      project_name: String(project?.name || '').trim(),
      project_summary: String(project?.summary || '').trim(),
      target_username: target?.username || '',
      target_name: target?.name || '',
      granted_by_username: grantedBy?.username || '',
      granted_by_name: grantedBy?.name || ''
    };
  });
}

export async function listOutgoingProjectShares(userId: string): Promise<ProjectShareRecord[]> {
  const normalizedUserId = String(userId || '').trim();
  if (!normalizedUserId) return [];
  const rows = await request<Array<{
    id: string;
    project_id: string;
    user_id: string;
    granted_by_user_id: string | null;
    access_level?: string;
    created_at: string;
    updated_at: string;
  }>>('/project_shares', undefined, {
    select: '*',
    granted_by_user_id: `eq.${normalizedUserId}`,
    order: 'created_at.desc'
  });
  const projectIds = Array.from(new Set(rows.map((row) => String(row.project_id || '').trim()).filter(Boolean)));
  const userIds = Array.from(
    new Set(
      rows.flatMap((row) => [String(row.user_id || '').trim(), String(row.granted_by_user_id || '').trim()]).filter(Boolean)
    )
  );
  const [projects, users] = await Promise.all([
    listProjectsByIds(projectIds, { lightweight: true }),
    listUsersByIds(userIds)
  ]);
  const projectById = new Map(projects.map((project) => [project.id, project] as const));
  const userById = new Map(users.map((user) => [user.id, user] as const));
  return rows.map((row) => {
    const project = projectById.get(String(row.project_id || '').trim());
    const target = userById.get(String(row.user_id || '').trim());
    const grantedBy = userById.get(String(row.granted_by_user_id || '').trim());
    return {
      ...row,
      access_level: normalizeShareAccessLevel(row.access_level),
      project_name: String(project?.name || '').trim(),
      project_summary: String(project?.summary || '').trim(),
      target_username: target?.username || '',
      target_name: target?.name || '',
      granted_by_username: grantedBy?.username || '',
      granted_by_name: grantedBy?.name || ''
    };
  });
}

export async function listIncomingTaskShares(userId: string): Promise<ProjectTaskShareRecord[]> {
  const normalizedUserId = String(userId || '').trim();
  if (!normalizedUserId) return [];
  const rows = await request<Array<{
    id: string;
    project_id: string;
    project_task_id: string;
    user_id: string;
    granted_by_user_id: string | null;
    access_level?: string;
    created_at: string;
    updated_at: string;
  }>>('/project_task_shares', undefined, {
    select: '*',
    user_id: `eq.${normalizedUserId}`,
    order: 'created_at.desc'
  });
  const projectIds = Array.from(new Set(rows.map((row) => String(row.project_id || '').trim()).filter(Boolean)));
  const taskIds = Array.from(new Set(rows.map((row) => String(row.project_task_id || '').trim()).filter(Boolean)));
  const userIds = Array.from(
    new Set(
      rows.flatMap((row) => [String(row.user_id || '').trim(), String(row.granted_by_user_id || '').trim()]).filter(Boolean)
    )
  );
  const [projects, tasks, users] = await Promise.all([
    listProjectsByIds(projectIds, { lightweight: true }),
    listProjectTaskMetaByIds(taskIds),
    listUsersByIds(userIds)
  ]);
  const projectById = new Map(projects.map((project) => [project.id, project] as const));
  const taskById = new Map(tasks.map((task) => [String(task.id || '').trim(), task] as const));
  const userById = new Map(users.map((user) => [user.id, user] as const));
  return rows.map((row) => {
    const project = projectById.get(String(row.project_id || '').trim());
    const task = taskById.get(String(row.project_task_id || '').trim());
    const target = userById.get(String(row.user_id || '').trim());
    const grantedBy = userById.get(String(row.granted_by_user_id || '').trim());
    return {
      ...row,
      access_level: normalizeShareAccessLevel(row.access_level),
      project_name: String(project?.name || '').trim(),
      project_summary: String(project?.summary || '').trim(),
      task_name: String(task?.name || '').trim(),
      task_summary: String(task?.summary || '').trim(),
      target_username: target?.username || '',
      target_name: target?.name || '',
      granted_by_username: grantedBy?.username || '',
      granted_by_name: grantedBy?.name || ''
    };
  });
}

export async function listOutgoingTaskShares(userId: string): Promise<ProjectTaskShareRecord[]> {
  const normalizedUserId = String(userId || '').trim();
  if (!normalizedUserId) return [];
  const rows = await request<Array<{
    id: string;
    project_id: string;
    project_task_id: string;
    user_id: string;
    granted_by_user_id: string | null;
    access_level?: string;
    created_at: string;
    updated_at: string;
  }>>('/project_task_shares', undefined, {
    select: '*',
    granted_by_user_id: `eq.${normalizedUserId}`,
    order: 'created_at.desc'
  });
  const projectIds = Array.from(new Set(rows.map((row) => String(row.project_id || '').trim()).filter(Boolean)));
  const taskIds = Array.from(new Set(rows.map((row) => String(row.project_task_id || '').trim()).filter(Boolean)));
  const userIds = Array.from(
    new Set(
      rows.flatMap((row) => [String(row.user_id || '').trim(), String(row.granted_by_user_id || '').trim()]).filter(Boolean)
    )
  );
  const [projects, tasks, users] = await Promise.all([
    listProjectsByIds(projectIds, { lightweight: true }),
    listProjectTaskMetaByIds(taskIds),
    listUsersByIds(userIds)
  ]);
  const projectById = new Map(projects.map((project) => [project.id, project] as const));
  const taskById = new Map(tasks.map((task) => [String(task.id || '').trim(), task] as const));
  const userById = new Map(users.map((user) => [user.id, user] as const));
  return rows.map((row) => {
    const project = projectById.get(String(row.project_id || '').trim());
    const task = taskById.get(String(row.project_task_id || '').trim());
    const target = userById.get(String(row.user_id || '').trim());
    const grantedBy = userById.get(String(row.granted_by_user_id || '').trim());
    return {
      ...row,
      access_level: normalizeShareAccessLevel(row.access_level),
      project_name: String(project?.name || '').trim(),
      project_summary: String(project?.summary || '').trim(),
      task_name: String(task?.name || '').trim(),
      task_summary: String(task?.summary || '').trim(),
      target_username: target?.username || '',
      target_name: target?.name || '',
      granted_by_username: grantedBy?.username || '',
      granted_by_name: grantedBy?.name || ''
    };
  });
}

// api_tokens CRUD lives in api/authServerApi.ts; only usage-log reads remain here.

