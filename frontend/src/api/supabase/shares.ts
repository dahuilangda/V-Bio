import { getProjectTaskById } from './taskDetail';
import { normalizeShareAccessLevel } from './shared';
import { ProjectShareRecord, ProjectTaskShareRecord, ShareAccessLevel } from '../../types/models';
import { listUsersByIds, request } from './rest';



// 项目与任务分享 CRUD。

export async function listProjectShares(projectId: string): Promise<ProjectShareRecord[]> {
  const normalizedProjectId = String(projectId || '').trim();
  if (!normalizedProjectId) return [];
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
    project_id: `eq.${normalizedProjectId}`,
    order: 'created_at.asc'
  });
  const userIds = Array.from(
    new Set(
      rows.flatMap((row) => [String(row.user_id || '').trim(), String(row.granted_by_user_id || '').trim()]).filter(Boolean)
    )
  );
  const users = await listUsersByIds(userIds);
  const userById = new Map(users.map((user) => [user.id, user] as const));
  return rows.map((row) => {
    const target = userById.get(String(row.user_id || '').trim());
    const grantedBy = userById.get(String(row.granted_by_user_id || '').trim());
    return {
      ...row,
      access_level: normalizeShareAccessLevel(row.access_level),
      target_username: target?.username || '',
      target_name: target?.name || '',
      granted_by_username: grantedBy?.username || '',
      granted_by_name: grantedBy?.name || ''
    };
  });
}

export async function upsertProjectShare(input: {
  projectId: string;
  userId: string;
  grantedByUserId?: string | null;
  accessLevel?: ShareAccessLevel;
}): Promise<ProjectShareRecord> {
  await request<ProjectShareRecord[]>(
    '/project_shares',
    {
      method: 'POST',
      headers: {
        Prefer: 'resolution=merge-duplicates,return=representation'
      },
      body: JSON.stringify({
        project_id: input.projectId,
        user_id: input.userId,
        granted_by_user_id: input.grantedByUserId || null,
        ...(normalizeShareAccessLevel(input.accessLevel) === 'editor'
          ? { access_level: 'editor' }
          : {})
      })
    },
    {
      select: '*',
      on_conflict: 'project_id,user_id'
    }
  );
  const records = await listProjectShares(input.projectId);
  const created = records.find((row) => row.project_id === input.projectId && row.user_id === input.userId);
  if (!created) {
    throw new Error('Failed to load persisted project share.');
  }
  return created;
}

export async function updateProjectShareAccessLevel(shareId: string, accessLevel: ShareAccessLevel): Promise<ProjectShareRecord> {
  const rows = await request<ProjectShareRecord[]>(
    '/project_shares',
    {
      method: 'PATCH',
      headers: {
        Prefer: 'return=representation'
      },
      body: JSON.stringify({
        access_level: normalizeShareAccessLevel(accessLevel)
      })
    },
    {
      id: `eq.${shareId}`,
      select: '*'
    }
  );
  const updated = rows[0];
  if (!updated) {
    throw new Error('Failed to update project share access level.');
  }
  return updated;
}

export async function deleteProjectShare(shareId: string): Promise<void> {
  await request<ProjectShareRecord[]>(
    '/project_shares',
    {
      method: 'DELETE',
      headers: {
        Prefer: 'return=minimal'
      }
    },
    {
      id: `eq.${shareId}`
    }
  );
}

export async function deleteProjectSharesByProjectId(projectId: string): Promise<void> {
  const normalizedProjectId = String(projectId || '').trim();
  if (!normalizedProjectId) return;
  await request<ProjectShareRecord[]>(
    '/project_shares',
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

export async function listProjectTaskShares(projectTaskId: string): Promise<ProjectTaskShareRecord[]> {
  const normalizedTaskId = String(projectTaskId || '').trim();
  if (!normalizedTaskId) return [];
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
    project_task_id: `eq.${normalizedTaskId}`,
    order: 'created_at.asc'
  });
  const task = await getProjectTaskById(normalizedTaskId, {
    includeComponents: false,
    includeConstraints: false,
    includeProperties: false,
    includeConfidence: false,
    includeAffinity: false,
    includeProteinSequence: false
  });
  const userIds = Array.from(
    new Set(
      rows.flatMap((row) => [String(row.user_id || '').trim(), String(row.granted_by_user_id || '').trim()]).filter(Boolean)
    )
  );
  const users = await listUsersByIds(userIds);
  const userById = new Map(users.map((user) => [user.id, user] as const));
  return rows.map((row) => {
    const target = userById.get(String(row.user_id || '').trim());
    const grantedBy = userById.get(String(row.granted_by_user_id || '').trim());
    return {
      ...row,
      access_level: normalizeShareAccessLevel(row.access_level),
      task_name: String(task?.name || '').trim(),
      task_summary: String(task?.summary || '').trim(),
      target_username: target?.username || '',
      target_name: target?.name || '',
      granted_by_username: grantedBy?.username || '',
      granted_by_name: grantedBy?.name || ''
    };
  });
}

export async function upsertProjectTaskShare(input: {
  projectId: string;
  projectTaskId: string;
  userId: string;
  grantedByUserId?: string | null;
  accessLevel?: ShareAccessLevel;
}): Promise<ProjectTaskShareRecord> {
  await request<ProjectTaskShareRecord[]>(
    '/project_task_shares',
    {
      method: 'POST',
      headers: {
        Prefer: 'resolution=merge-duplicates,return=representation'
      },
      body: JSON.stringify({
        project_id: input.projectId,
        project_task_id: input.projectTaskId,
        user_id: input.userId,
        granted_by_user_id: input.grantedByUserId || null,
        ...(normalizeShareAccessLevel(input.accessLevel) === 'editor'
          ? { access_level: 'editor' }
          : {})
      })
    },
    {
      select: '*',
      on_conflict: 'project_task_id,user_id'
    }
  );
  const records = await listProjectTaskShares(input.projectTaskId);
  const created = records.find(
    (row) => row.project_task_id === input.projectTaskId && row.user_id === input.userId
  );
  if (!created) {
    throw new Error('Failed to load persisted task share.');
  }
  return created;
}

export async function updateProjectTaskShareAccessLevel(
  shareId: string,
  accessLevel: ShareAccessLevel
): Promise<ProjectTaskShareRecord> {
  const rows = await request<ProjectTaskShareRecord[]>(
    '/project_task_shares',
    {
      method: 'PATCH',
      headers: {
        Prefer: 'return=representation'
      },
      body: JSON.stringify({
        access_level: normalizeShareAccessLevel(accessLevel)
      })
    },
    {
      id: `eq.${shareId}`,
      select: '*'
    }
  );
  const updated = rows[0];
  if (!updated) {
    throw new Error('Failed to update task share access level.');
  }
  return updated;
}

export async function deleteProjectTaskShare(shareId: string): Promise<void> {
  await request<ProjectTaskShareRecord[]>(
    '/project_task_shares',
    {
      method: 'DELETE',
      headers: {
        Prefer: 'return=minimal'
      }
    },
    {
      id: `eq.${shareId}`
    }
  );
}

export async function deleteProjectTaskSharesByProjectId(projectId: string): Promise<void> {
  const normalizedProjectId = String(projectId || '').trim();
  if (!normalizedProjectId) return;
  await request<ProjectTaskShareRecord[]>(
    '/project_task_shares',
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

