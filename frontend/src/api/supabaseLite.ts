import type {
  ApiToken,
  ApiTokenUsage,
  ApiTokenUsageDaily,
  AppUser,
  Project,
  ProjectTaskCounts,
  ProjectAccessScope,
  EffectiveAccessLevel,
  ProjectShareRecord,
  ProjectCopilotMessage,
  ProjectTask,
  ProjectTaskShareRecord,
  ShareAccessLevel,
  TaskState
} from '../types/models';
import { ENV } from '../utils/env';
import { PEPTIDE_TASK_PREVIEW_KEY } from '../utils/peptideTaskPreview';

const configuredBaseUrl = ENV.supabaseRestUrl.replace(/\/$/, '');
const SUPABASE_TIMEOUT_MS = 15000;

async function fetchWithTimeout(url: string, init: RequestInit = {}, timeoutMs = SUPABASE_TIMEOUT_MS): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, {
      ...init,
      signal: controller.signal
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new Error(
        `Supabase-lite request timeout after ${timeoutMs}ms for ${url}. Check that PostgREST is reachable (frontend/supabase-lite, default port 54321).`
      );
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function asObjectRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function compactObjectRecord(record: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(record).filter(([, value]) => {
      if (value === null || value === undefined) return false;
      if (typeof value === 'string') return value.trim().length > 0;
      if (Array.isArray(value)) return value.length > 0;
      if (typeof value === 'object') return Object.keys(asObjectRecord(value)).length > 0;
      return true;
    })
  );
}

function parseBooleanToken(value: unknown): boolean | null {
  if (value === true) return true;
  if (value === false) return false;
  const token = String(value ?? '').trim().toLowerCase();
  if (!token) return null;
  if (token === 'true' || token === '1' || token === 'yes' || token === 'on') return true;
  if (token === 'false' || token === '0' || token === 'no' || token === 'off') return false;
  return null;
}

function buildQueryString(query?: Record<string, string | undefined>) {
  const params = new URLSearchParams();
  if (query) {
    Object.entries(query).forEach(([key, value]) => {
      if (value !== undefined && value !== '') {
        params.set(key, value);
      }
    });
  }
  return params.toString();
}

function buildInFilter(values: string[]): string | undefined {
  const normalized = Array.from(
    new Set(
      values
        .map((value) => String(value || '').trim())
        .filter(Boolean)
    )
  );
  if (normalized.length === 0) return undefined;
  return `in.(${normalized.join(',')})`;
}

function buildSupabaseUrlCandidates(path: string, query?: Record<string, string | undefined>): string[] {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  const queryString = buildQueryString(query);
  const suffix = `${normalizedPath}${queryString ? `?${queryString}` : ''}`;
  return [`${configuredBaseUrl}${suffix}`].filter(Boolean);
}

async function request<T>(
  path: string,
  options?: RequestInit,
  query?: Record<string, string | undefined>
): Promise<T> {
  const candidates = buildSupabaseUrlCandidates(path, query);
  let lastError: Error | null = null;

  for (let i = 0; i < candidates.length; i += 1) {
    const url = candidates[i];
    let res: Response;
    try {
      res = await fetchWithTimeout(url, {
        ...options,
        headers: {
          'Content-Type': 'application/json',
          ...(options?.headers || {})
        }
      });
    } catch (error) {
      lastError = error instanceof Error ? error : new Error('Unknown network error');
      continue;
    }

    if ((res.status === 404 || res.status === 405) && i < candidates.length - 1) {
      lastError = new Error(`Supabase candidate returned ${res.status}: ${url}`);
      continue;
    }

    if (!res.ok) {
      const text = await res.text();
      throw new Error(`PostgREST ${res.status}: ${text}`);
    }

    if (res.status === 204) {
      return [] as T;
    }

    return (await res.json()) as T;
  }

  const detail = lastError ? ` Last error: ${lastError.message}` : '';
  throw new Error(`Supabase-lite request failed. Tried: ${candidates.join(', ')}.${detail}`);
}

async function listUsersByIds(userIds: string[]): Promise<AppUser[]> {
  const idFilter = buildInFilter(userIds);
  if (!idFilter) return [];
  return request<AppUser[]>('/app_users', undefined, {
    select: 'id,username,name,email,is_admin,last_login_at,deleted_at,created_at,updated_at,password_hash',
    id: idFilter,
    deleted_at: 'is.null'
  });
}

async function listProjectTaskMetaByIds(taskIds: string[]): Promise<Array<{
  id: string;
  project_id: string;
  name: string;
  summary: string;
  task_id: string;
}>> {
  const idFilter = buildInFilter(taskIds);
  if (!idFilter) return [];
  return request<Array<{
    id: string;
    project_id: string;
    name: string;
    summary: string;
    task_id: string;
  }>>('/project_tasks_list', undefined, {
    select: 'id,project_id,name,summary,task_id',
    id: idFilter
  });
}

function normalizeProjectAccessScope(value: unknown): ProjectAccessScope {
  const token = String(value || '').trim().toLowerCase();
  if (token === 'project_share') return 'project_share';
  if (token === 'task_share') return 'task_share';
  return 'owner';
}

function normalizeShareAccessLevel(value: unknown): ShareAccessLevel {
  return String(value || '').trim().toLowerCase() === 'editor' ? 'editor' : 'viewer';
}

function normalizeEffectiveAccessLevel(value: unknown): EffectiveAccessLevel {
  const token = String(value || '').trim().toLowerCase();
  if (token === 'owner') return 'owner';
  if (token === 'editor') return 'editor';
  return 'viewer';
}

function toEffectiveAccessLevel(value: unknown): EffectiveAccessLevel {
  return normalizeShareAccessLevel(value) === 'editor' ? 'editor' : 'viewer';
}

function buildEditableTaskIdSet(taskIds: string[]): Set<string> {
  return new Set(taskIds.map((item) => String(item || '').trim()).filter(Boolean));
}

function resolveTaskAccessLevel(
  taskId: string,
  scope: ProjectAccessScope,
  accessLevel: EffectiveAccessLevel,
  editableTaskIdSet?: Set<string>
): EffectiveAccessLevel {
  if (scope === 'owner') return 'owner';
  if (editableTaskIdSet?.has(String(taskId || '').trim())) return 'editor';
  if (scope === 'project_share' && accessLevel === 'editor') return 'editor';
  return 'viewer';
}

function normalizeProjectRow(row: Partial<Project>): Project {
  const normalizedAccessibleTaskIds = Array.isArray(row.accessible_task_ids)
    ? Array.from(new Set(row.accessible_task_ids.map((item) => String(item || '').trim()).filter(Boolean)))
    : [];
  const normalizedEditableTaskIds = Array.isArray(row.editable_task_ids)
    ? Array.from(new Set(row.editable_task_ids.map((item) => String(item || '').trim()).filter(Boolean)))
    : [];
  return {
    ...row,
    protein_sequence: row.protein_sequence || '',
    ligand_smiles: row.ligand_smiles || '',
    confidence:
      row.confidence && typeof row.confidence === 'object' && !Array.isArray(row.confidence)
        ? row.confidence
        : {},
    affinity:
      row.affinity && typeof row.affinity === 'object' && !Array.isArray(row.affinity)
        ? row.affinity
        : {},
    access_scope: normalizeProjectAccessScope(row.access_scope),
    access_level: normalizeEffectiveAccessLevel(row.access_level || (String(row.access_scope || '').trim() === 'owner' ? 'owner' : 'viewer')),
    accessible_task_ids: normalizedAccessibleTaskIds,
    editable_task_ids: normalizedEditableTaskIds
  } as Project;
}

function applyProjectAccess(project: Project, access: { scope: ProjectAccessScope; taskIds: string[]; editableTaskIds?: string[]; accessLevel: EffectiveAccessLevel }): Project {
  const nextAccessibleTaskIds = access.scope === 'task_share' ? access.taskIds : [];
  const nextEditableTaskIds = access.editableTaskIds || [];
  return {
    ...project,
    access_scope: access.scope,
    access_level: access.accessLevel,
    accessible_task_ids: nextAccessibleTaskIds,
    editable_task_ids: nextEditableTaskIds
  };
}

function applyTaskAccess(
  task: ProjectTask,
  scope: ProjectAccessScope,
  accessLevel: EffectiveAccessLevel = scope === 'owner' ? 'owner' : 'viewer',
  editableTaskIdSet?: Set<string>
): ProjectTask {
  return {
    ...task,
    access_scope: scope,
    access_level: resolveTaskAccessLevel(task.id, scope, accessLevel, editableTaskIdSet)
  };
}

export function sanitizeProjectForTaskShare(project: Project, tasks: ProjectTask[]): Project {
  const sanitizedTasks = tasks.filter((task) => String(task.project_id || '').trim() === String(project.id || '').trim());
  const activeTaskId = String(project.task_id || '').trim();
  const preferredTask =
    (activeTaskId
      ? sanitizedTasks.find((task) => String(task.task_id || '').trim() === activeTaskId) || null
      : null) ||
    sanitizedTasks[0] ||
    null;
  if (!preferredTask) {
    return {
      ...project,
      task_id: '',
      task_state: 'DRAFT',
      status_text: 'Shared task unavailable.',
      error_text: '',
      confidence: {},
      affinity: {},
      structure_name: ''
    };
  }
  return {
    ...project,
    task_id: preferredTask.task_id,
    task_state: preferredTask.task_state,
    status_text: preferredTask.status_text,
    error_text: preferredTask.error_text,
    submitted_at: preferredTask.submitted_at,
    completed_at: preferredTask.completed_at,
    duration_seconds: preferredTask.duration_seconds,
    confidence:
      preferredTask.confidence && typeof preferredTask.confidence === 'object' && !Array.isArray(preferredTask.confidence)
        ? preferredTask.confidence
        : {},
    affinity:
      preferredTask.affinity && typeof preferredTask.affinity === 'object' && !Array.isArray(preferredTask.affinity)
        ? preferredTask.affinity
        : {},
    structure_name: preferredTask.structure_name || ''
  };
}

export async function listUsers(): Promise<AppUser[]> {
  return request<AppUser[]>('/app_users', undefined, {
    select: '*',
    deleted_at: 'is.null',
    order: 'created_at.asc'
  });
}

export async function findUserByUsername(username: string): Promise<AppUser | null> {
  const rows = await request<AppUser[]>('/app_users', undefined, {
    select: '*',
    username: `eq.${username.toLowerCase()}`,
    deleted_at: 'is.null',
    limit: '1'
  });
  return rows[0] || null;
}

export async function searchUsersForSharing(
  queryText: string,
  options?: { excludeUserId?: string | null; limit?: number }
): Promise<AppUser[]> {
  const normalizedQuery = String(queryText || '').trim().toLowerCase();
  if (normalizedQuery.length < 2) return [];
  const rows = await request<AppUser[]>('/app_users', undefined, {
    select: 'id,username,name,email,is_admin,last_login_at,deleted_at,created_at,updated_at,password_hash',
    username: `ilike.*${normalizedQuery}*`,
    deleted_at: 'is.null',
    order: 'username.asc',
    limit: String(Math.max(1, Math.min(12, Number(options?.limit || 8))))
  });
  const excludeUserId = String(options?.excludeUserId || '').trim();
  return rows.filter((row) => String(row.id || '').trim() !== excludeUserId);
}

export async function findUserByEmail(email: string): Promise<AppUser | null> {
  const rows = await request<AppUser[]>('/app_users', undefined, {
    select: '*',
    email: `eq.${email.toLowerCase()}`,
    deleted_at: 'is.null',
    limit: '1'
  });
  return rows[0] || null;
}

export async function findUserByIdentifier(identifier: string): Promise<AppUser | null> {
  const value = identifier.trim().toLowerCase();
  const rows = await request<AppUser[]>('/app_users', undefined, {
    select: '*',
    or: `(username.eq.${value},email.eq.${value})`,
    deleted_at: 'is.null',
    limit: '1'
  });
  return rows[0] || null;
}

export async function insertUser(input: Partial<AppUser>): Promise<AppUser> {
  const rows = await request<AppUser[]>(
    '/app_users',
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
  return rows[0];
}

export async function updateUser(userId: string, patch: Partial<AppUser>): Promise<AppUser> {
  const rows = await request<AppUser[]>(
    '/app_users',
    {
      method: 'PATCH',
      headers: {
        Prefer: 'return=representation'
      },
      body: JSON.stringify(patch)
    },
    {
      id: `eq.${userId}`,
      select: '*'
    }
  );
  return rows[0];
}

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

export async function listAccessibleProjects(
  userId: string,
  options: ListProjectsOptions = {}
): Promise<Project[]> {
  const normalizedUserId = String(userId || '').trim();
  if (!normalizedUserId) return [];

  const [ownedProjects, projectShareLinks, taskShareLinks] = await Promise.all([
    listProjects({
      ...options,
      userId: normalizedUserId
    }),
    listProjectShareLinksForUser(normalizedUserId),
    listProjectTaskShareLinksForUser(normalizedUserId)
  ]);

  const ownedIds = new Set(ownedProjects.map((project) => String(project.id || '').trim()).filter(Boolean));
  const projectShareByProjectId = new Map(
    projectShareLinks
      .map((row) => [String(row.project_id || '').trim(), normalizeShareAccessLevel(row.access_level)] as const)
      .filter(([projectId]) => Boolean(projectId))
  );
  const taskShareIdsByProject = new Map<string, string[]>();
  const editableTaskIdsByProject = new Map<string, string[]>();
  for (const row of taskShareLinks) {
    const projectId = String(row.project_id || '').trim();
    const taskId = String(row.project_task_id || '').trim();
    if (!projectId || !taskId) continue;
    const current = taskShareIdsByProject.get(projectId) || [];
    current.push(taskId);
    taskShareIdsByProject.set(projectId, current);
    if (normalizeShareAccessLevel(row.access_level) === 'editor') {
      const editable = editableTaskIdsByProject.get(projectId) || [];
      editable.push(taskId);
      editableTaskIdsByProject.set(projectId, editable);
    }
  }

  const sharedProjectIds = Array.from(
    new Set([
      ...Array.from(projectShareByProjectId.keys()),
      ...Array.from(taskShareIdsByProject.keys())
    ])
  ).filter((id) => !ownedIds.has(id));

  const sharedProjects = await listProjectsByIds(sharedProjectIds, { lightweight: options.lightweight });
  const sharedById = new Map(sharedProjects.map((project) => [project.id, project] as const));

  const rows: Project[] = [];
  for (const project of ownedProjects) {
    rows.push(applyProjectAccess(project, { scope: 'owner', taskIds: [], editableTaskIds: [], accessLevel: 'owner' }));
  }
  for (const projectId of sharedProjectIds) {
    const project = sharedById.get(projectId);
    if (!project) continue;
    const taskIds = Array.from(new Set(taskShareIdsByProject.get(projectId) || []));
    const editableTaskIds = Array.from(new Set(editableTaskIdsByProject.get(projectId) || []));
    const projectShareLevel = projectShareByProjectId.get(projectId);
    const scope: ProjectAccessScope = projectShareLevel ? 'project_share' : 'task_share';
    rows.push(
      applyProjectAccess(project, {
        scope,
        taskIds,
        editableTaskIds,
        accessLevel: projectShareLevel ? toEffectiveAccessLevel(projectShareLevel) : editableTaskIds.length > 0 ? 'editor' : 'viewer'
      })
    );
  }

  rows.sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime());
  return rows;
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
  return rows[0];
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
  return rows[0];
}

interface ProjectTaskAccessOptions {
  taskRowIds?: string[];
  accessScope?: ProjectAccessScope;
  accessLevel?: EffectiveAccessLevel;
  editableTaskIds?: string[];
}

export async function listProjectTasks(projectId: string, options?: ProjectTaskAccessOptions): Promise<ProjectTask[]> {
  const taskIdFilter = buildInFilter(options?.taskRowIds || []);
  const rows = await request<ProjectTask[]>('/project_tasks', undefined, {
    select: '*',
    project_id: `eq.${projectId}`,
    ...(taskIdFilter ? { id: taskIdFilter } : {}),
    order: 'created_at.desc'
  });
  const scope = options?.accessScope || 'owner';
  const accessLevel = options?.accessLevel || (scope === 'owner' ? 'owner' : 'viewer');
  const editableTaskIdSet = buildEditableTaskIdSet(options?.editableTaskIds || []);
  return rows.map((row) => applyTaskAccess(row, scope, accessLevel, editableTaskIdSet));
}

export async function listProjectTasksCompact(
  projectId: string,
  options?: ProjectTaskAccessOptions
): Promise<ProjectTask[]> {
  const selectFields = [
    'id',
    'project_id',
    'name',
    'summary',
    'task_id',
    'task_state',
    'status_text',
    'error_text',
    'backend',
    'seed',
    'ligand_smiles',
    'properties',
    'structure_name',
    'submitted_at',
    'completed_at',
    'duration_seconds',
    'created_at',
    'updated_at'
  ].join(',');
  const taskIdFilter = buildInFilter(options?.taskRowIds || []);
  const rows = await request<Array<Partial<ProjectTask>>>('/project_tasks_list', undefined, {
    select: selectFields,
    project_id: `eq.${projectId}`,
    ...(taskIdFilter ? { id: taskIdFilter } : {}),
    order: 'created_at.desc'
  });

  const scope = options?.accessScope || 'owner';
  const accessLevel = options?.accessLevel || (scope === 'owner' ? 'owner' : 'viewer');
  const editableTaskIdSet = buildEditableTaskIdSet(options?.editableTaskIds || []);
  return rows.map((row) => applyTaskAccess({
    name: '',
    summary: '',
    protein_sequence: '',
    affinity: {},
    confidence: {},
    components: [],
    constraints: [],
    properties: {
      affinity: false,
      target: null,
      ligand: null,
      binder: null
    },
    ...row
  } as ProjectTask, scope, accessLevel, editableTaskIdSet));
}

export async function listProjectTasksForList(
  projectId: string,
  options?: {
    includeComponents?: boolean;
    includeConfidence?: boolean;
    includeConfidenceSummary?: boolean;
    includeProperties?: boolean;
    includePropertiesSummary?: boolean;
    includeLeadOptSummary?: boolean;
    includeLeadOptCandidates?: boolean;
    taskRowIds?: string[];
    accessScope?: ProjectAccessScope;
    accessLevel?: EffectiveAccessLevel;
    editableTaskIds?: string[];
    limit?: number;
    offset?: number;
  }
): Promise<ProjectTask[]> {
  const includeComponents = options?.includeComponents !== false;
  const includeConfidence = options?.includeConfidence !== false;
  const includeConfidenceSummary = !includeConfidence && options?.includeConfidenceSummary === true;
  const includeProperties = options?.includeProperties !== false;
  const includePropertiesSummary = !includeProperties && options?.includePropertiesSummary === true;
  const includeLeadOptSummary = options?.includeLeadOptSummary === true;
  const includeLeadOptCandidates = options?.includeLeadOptCandidates === true;
  const taskIdFilter = buildInFilter(options?.taskRowIds || []);
  const limit = Number(options?.limit);
  const offset = Number(options?.offset);
  const paginationQuery = {
    ...(Number.isFinite(limit) && limit > 0 ? { limit: String(Math.floor(limit)) } : {}),
    ...(Number.isFinite(offset) && offset > 0 ? { offset: String(Math.floor(offset)) } : {})
  };
  const selectFields = [
    'id',
    'project_id',
    'name',
    'summary',
    'task_id',
    'task_state',
    'status_text',
    'error_text',
    'backend',
    'seed',
    'ligand_smiles',
    ...(includePropertiesSummary
      ? [
          'properties_target:properties->>target',
          'properties_ligand:properties->>ligand',
          'properties_binder:properties->>binder',
          'properties_affinity_mode:properties->__vbio_input_options_v1->>affinityMode',
          'properties_peptide_preview_design_mode:properties->peptide_preview->>design_mode',
          'properties_peptide_preview_binder_length:properties->peptide_preview->>binder_length',
          'properties_peptide_preview_iterations:properties->peptide_preview->>iterations',
          'properties_peptide_preview_population_size:properties->peptide_preview->>population_size',
          'properties_peptide_preview_elite_size:properties->peptide_preview->>elite_size',
          'properties_peptide_preview_mutation_rate:properties->peptide_preview->>mutation_rate',
          'properties_peptide_preview_current_generation:properties->peptide_preview->>current_generation',
          'properties_peptide_preview_total_generations:properties->peptide_preview->>total_generations',
          'properties_peptide_preview_best_score:properties->peptide_preview->>best_score',
          'properties_peptide_preview_candidate_count:properties->peptide_preview->>candidate_count',
          'properties_peptide_preview_completed_tasks:properties->peptide_preview->>completed_tasks',
          'properties_peptide_preview_pending_tasks:properties->peptide_preview->>pending_tasks',
          'properties_peptide_preview_total_tasks:properties->peptide_preview->>total_tasks',
          'properties_peptide_preview_current_status:properties->peptide_preview->>current_status',
          'properties_peptide_preview_status_message:properties->peptide_preview->>status_message',
          'properties_peptide_preview_best_candidate:properties->peptide_preview->best_candidate'
        ]
      : []),
    ...(includeProperties ? ['properties'] : []),
    ...(includeConfidenceSummary
      ? [
          'confidence_ligand_smiles:confidence->>ligand_smiles',
          'confidence_ligand_display_smiles:confidence->>ligand_display_smiles',
          'confidence_requested_target_chain_id:confidence->>requested_target_chain_id',
          'confidence_target_chain_id:confidence->>target_chain_id',
          'confidence_target_chain:confidence->>target_chain',
          'confidence_protein_chain_id:confidence->>protein_chain_id',
          'confidence_requested_ligand_chain_id:confidence->>requested_ligand_chain_id',
          'confidence_ligand_chain_id:confidence->>ligand_chain_id',
          'confidence_model_ligand_chain_id:confidence->>model_ligand_chain_id',
          'confidence_binder_chain_id:confidence->>binder_chain_id',
          'confidence_chain_ids:confidence->chain_ids',
          'confidence_chain_mean_plddt:confidence->chain_mean_plddt',
          'confidence_pair_chains_iptm:confidence->pair_chains_iptm',
          'confidence_chain_pair_iptm:confidence->chain_pair_iptm',
          'confidence_chain_pair_iptm_global:confidence->chain_pair_iptm_global',
          'confidence_ligand_display_atom_plddts_by_chain:confidence->ligand_display_atom_plddts_by_chain',
          'confidence_ligand_atom_plddts_by_chain:confidence->ligand_atom_plddts_by_chain',
          'confidence_ligand_display_atom_plddts:confidence->ligand_display_atom_plddts',
          'confidence_ligand_atom_plddts:confidence->ligand_atom_plddts',
          'confidence_ligand_plddt:confidence->>ligand_plddt',
          'confidence_ligand_mean_plddt:confidence->>ligand_mean_plddt',
          'confidence_complex_iplddt:confidence->>complex_iplddt',
          'confidence_complex_plddt_protein:confidence->>complex_plddt_protein',
          'confidence_complex_plddt:confidence->>complex_plddt',
          'confidence_plddt:confidence->>plddt',
          'confidence_ipsae_dom:confidence->>ipsae_dom',
          'confidence_ligand_ipsae_max:confidence->>ligand_ipsae_max',
          'confidence_iptm:confidence->>iptm',
          'confidence_ligand_iptm:confidence->>ligand_iptm',
          'confidence_protein_iptm:confidence->>protein_iptm',
          'confidence_complex_pde:confidence->>complex_pde',
          'confidence_complex_pae:confidence->>complex_pae',
          'confidence_gpde:confidence->>gpde',
          'confidence_pae:confidence->>pae'
        ]
      : []),
    ...(includeConfidence ? ['confidence'] : []),
    ...(includeLeadOptSummary
      ? [
          'lead_opt_list_stage:properties->lead_opt_list->>stage',
          'lead_opt_list_prediction_stage:properties->lead_opt_list->>prediction_stage',
          'lead_opt_list_query_id:properties->lead_opt_list->>query_id',
          'lead_opt_list_transform_count:properties->lead_opt_list->>transform_count',
          'lead_opt_list_candidate_count:properties->lead_opt_list->>candidate_count',
          'lead_opt_list_bucket_count:properties->lead_opt_list->>bucket_count',
          'lead_opt_list_database_id:properties->lead_opt_list->>mmp_database_id',
          'lead_opt_list_database_label:properties->lead_opt_list->>mmp_database_label',
          'lead_opt_list_database_schema:properties->lead_opt_list->>mmp_database_schema',
          'lead_opt_list_selected_fragment_ids:properties->lead_opt_list->selection->selected_fragment_ids',
          'lead_opt_list_selected_atom_indices:properties->lead_opt_list->selection->selected_fragment_atom_indices',
          'lead_opt_list_selected_fragment_query:properties->lead_opt_list->>selected_fragment_query',
          'lead_opt_list_prediction_total:properties->lead_opt_list->prediction_summary->>total',
          'lead_opt_list_prediction_queued:properties->lead_opt_list->prediction_summary->>queued',
          'lead_opt_list_prediction_running:properties->lead_opt_list->prediction_summary->>running',
          'lead_opt_list_prediction_success:properties->lead_opt_list->prediction_summary->>success',
          'lead_opt_list_prediction_failure:properties->lead_opt_list->prediction_summary->>failure',
          'lead_opt_list_selection:properties->lead_opt_list->selection',
          'lead_opt_list_query_result_query_id:properties->lead_opt_list->query_result->>query_id',
          'lead_opt_list_query_result_query_mode:properties->lead_opt_list->query_result->>query_mode',
          'lead_opt_list_query_result_aggregation_type:properties->lead_opt_list->query_result->>aggregation_type',
          'lead_opt_list_query_result_property_targets:properties->lead_opt_list->query_result->property_targets',
          'lead_opt_list_query_result_rule_env_radius:properties->lead_opt_list->query_result->>rule_env_radius',
          'lead_opt_list_query_result_grouped_by_environment:properties->lead_opt_list->query_result->>grouped_by_environment',
          'lead_opt_list_query_result_cluster_group_by:properties->lead_opt_list->query_result->>cluster_group_by',
          'lead_opt_list_query_result_min_pairs:properties->lead_opt_list->query_result->>min_pairs',
          'lead_opt_list_query_result_count:properties->lead_opt_list->query_result->>count',
          'lead_opt_list_query_result_global_count:properties->lead_opt_list->query_result->>global_count',
          'lead_opt_list_query_result_stats:properties->lead_opt_list->query_result->stats',
          'lead_opt_list_query_result_database_id:properties->lead_opt_list->query_result->>mmp_database_id',
          'lead_opt_list_query_result_database_label:properties->lead_opt_list->query_result->>mmp_database_label',
          'lead_opt_list_query_result_database_schema:properties->lead_opt_list->query_result->>mmp_database_schema',
          ...(includeLeadOptCandidates
            ? ['lead_opt_list_enumerated_candidates:properties->lead_opt_list->enumerated_candidates']
            : []),
          'lead_opt_state_stage:properties->lead_opt_state->>stage',
          'lead_opt_state_prediction_stage:properties->lead_opt_state->>prediction_stage',
          'lead_opt_state_query_id:properties->lead_opt_state->>query_id',
          'lead_opt_state_prediction_total:properties->lead_opt_state->prediction_summary->>total',
          'lead_opt_state_prediction_queued:properties->lead_opt_state->prediction_summary->>queued',
          'lead_opt_state_prediction_running:properties->lead_opt_state->prediction_summary->>running',
          'lead_opt_state_prediction_success:properties->lead_opt_state->prediction_summary->>success',
          'lead_opt_state_prediction_failure:properties->lead_opt_state->prediction_summary->>failure',
          'lead_opt_state_prediction_task_id:properties->lead_opt_state->>prediction_task_id',
          'lead_opt_state_prediction_candidate_smiles:properties->lead_opt_state->>prediction_candidate_smiles',
          'lead_opt_state_prediction_by_smiles:properties->lead_opt_state->prediction_by_smiles',
          'lead_opt_state_reference_prediction_by_backend:properties->lead_opt_state->reference_prediction_by_backend',
        ]
      : []),
    'structure_name',
    'submitted_at',
    'completed_at',
    'duration_seconds',
    'created_at',
    'updated_at'
  ].join(',');
  const rows = await request<Array<Partial<ProjectTask>>>('/project_tasks_list', undefined, {
    select: selectFields,
    project_id: `eq.${projectId}`,
    ...(taskIdFilter ? { id: taskIdFilter } : {}),
    order: 'created_at.desc',
    ...paginationQuery
  });
  const detailRows = includeComponents
    ? await request<Array<Partial<ProjectTask>>>('/project_tasks_list', undefined, {
        select: 'id,components',
        project_id: `eq.${projectId}`,
        ...(taskIdFilter ? { id: taskIdFilter } : {}),
        order: 'created_at.desc',
        ...paginationQuery
      })
    : [];
  const detailById = new Map(
    detailRows
      .map((row) => [String(row.id || '').trim(), row] as const)
      .filter(([id]) => Boolean(id))
  );

  const readText = (value: unknown): string => {
    if (value === null || value === undefined) return '';
    return String(value).trim();
  };

  const readFiniteNumber = (value: unknown): number | null => {
    const parsed = typeof value === 'number' ? value : typeof value === 'string' ? Number(value.trim()) : Number.NaN;
    if (!Number.isFinite(parsed)) return null;
    return parsed;
  };

  const readStringArray = (value: unknown): string[] => {
    if (!Array.isArray(value)) return [];
    return Array.from(
      new Set(
        value
          .map((item) => String(item || '').trim())
          .filter(Boolean)
      )
    );
  };

  const readIntegerArray = (value: unknown): number[] => {
    if (!Array.isArray(value)) return [];
    return Array.from(
      new Set(
        value
          .map((item) => Number(item))
          .filter((item) => Number.isFinite(item) && item >= 0)
          .map((item) => Math.floor(item))
      )
    );
  };

  const buildLeadOptSummaryProperties = (row: Record<string, unknown>): Record<string, unknown> | null => {
    const listStage = readText(row.lead_opt_list_stage);
    const listPredictionStage = readText(row.lead_opt_list_prediction_stage);
    const listQueryId = readText(row.lead_opt_list_query_id);
    const listTransformCount = readFiniteNumber(row.lead_opt_list_transform_count);
    const listCandidateCount = readFiniteNumber(row.lead_opt_list_candidate_count);
    const listBucketCount = readFiniteNumber(row.lead_opt_list_bucket_count);
    const listDatabaseId = readText(row.lead_opt_list_database_id);
    const listDatabaseLabel = readText(row.lead_opt_list_database_label);
    const listDatabaseSchema = readText(row.lead_opt_list_database_schema);
    const listSelectedFragmentIds = readStringArray(row.lead_opt_list_selected_fragment_ids);
    const listSelectedAtomIndices = readIntegerArray(row.lead_opt_list_selected_atom_indices);
    const listSelectedFragmentQuery = readText(row.lead_opt_list_selected_fragment_query);

    const listPredictionTotal = readFiniteNumber(row.lead_opt_list_prediction_total);
    const listPredictionQueued = readFiniteNumber(row.lead_opt_list_prediction_queued);
    const listPredictionRunning = readFiniteNumber(row.lead_opt_list_prediction_running);
    const listPredictionSuccess = readFiniteNumber(row.lead_opt_list_prediction_success);
    const listPredictionFailure = readFiniteNumber(row.lead_opt_list_prediction_failure);
    const listSelection =
      row.lead_opt_list_selection && typeof row.lead_opt_list_selection === 'object' && !Array.isArray(row.lead_opt_list_selection)
        ? (row.lead_opt_list_selection as Record<string, unknown>)
        : {};
    const listQueryResultGroupedByEnvironment = parseBooleanToken(row.lead_opt_list_query_result_grouped_by_environment);
    const listQueryResult = compactObjectRecord({
      query_id: readText(row.lead_opt_list_query_result_query_id) || listQueryId,
      query_mode: readText(row.lead_opt_list_query_result_query_mode),
      aggregation_type: readText(row.lead_opt_list_query_result_aggregation_type),
      property_targets: asObjectRecord(row.lead_opt_list_query_result_property_targets),
      rule_env_radius: readFiniteNumber(row.lead_opt_list_query_result_rule_env_radius),
      grouped_by_environment:
        listQueryResultGroupedByEnvironment === null ? undefined : listQueryResultGroupedByEnvironment,
      cluster_group_by: readText(row.lead_opt_list_query_result_cluster_group_by),
      min_pairs: readFiniteNumber(row.lead_opt_list_query_result_min_pairs),
      count: readFiniteNumber(row.lead_opt_list_query_result_count),
      global_count: readFiniteNumber(row.lead_opt_list_query_result_global_count),
      stats: asObjectRecord(row.lead_opt_list_query_result_stats),
      mmp_database_id:
        readText(row.lead_opt_list_query_result_database_id) || listDatabaseId,
      mmp_database_label:
        readText(row.lead_opt_list_query_result_database_label) || listDatabaseLabel,
      mmp_database_schema:
        readText(row.lead_opt_list_query_result_database_schema) || listDatabaseSchema
    });
    const listEnumeratedCandidates =
      includeLeadOptCandidates && Array.isArray(row.lead_opt_list_enumerated_candidates)
        ? row.lead_opt_list_enumerated_candidates
        : [];

    const stateStage = readText(row.lead_opt_state_stage);
    const statePredictionStage = readText(row.lead_opt_state_prediction_stage);
    const stateQueryId = readText(row.lead_opt_state_query_id);
    const statePredictionTotal = readFiniteNumber(row.lead_opt_state_prediction_total);
    const statePredictionQueued = readFiniteNumber(row.lead_opt_state_prediction_queued);
    const statePredictionRunning = readFiniteNumber(row.lead_opt_state_prediction_running);
    const statePredictionSuccess = readFiniteNumber(row.lead_opt_state_prediction_success);
    const statePredictionFailure = readFiniteNumber(row.lead_opt_state_prediction_failure);
    const statePredictionTaskId = readText(row.lead_opt_state_prediction_task_id);
    const statePredictionCandidateSmiles = readText(row.lead_opt_state_prediction_candidate_smiles);
    const statePredictionBySmiles =
      row.lead_opt_state_prediction_by_smiles &&
      typeof row.lead_opt_state_prediction_by_smiles === 'object' &&
      !Array.isArray(row.lead_opt_state_prediction_by_smiles)
        ? (row.lead_opt_state_prediction_by_smiles as Record<string, unknown>)
        : {};
    const stateReferencePredictionByBackend =
      row.lead_opt_state_reference_prediction_by_backend &&
      typeof row.lead_opt_state_reference_prediction_by_backend === 'object' &&
      !Array.isArray(row.lead_opt_state_reference_prediction_by_backend)
        ? (row.lead_opt_state_reference_prediction_by_backend as Record<string, unknown>)
        : {};

    const hasAnyLeadOptMeta = Boolean(
      listStage ||
        listPredictionStage ||
        listQueryId ||
        listDatabaseId ||
        listDatabaseLabel ||
        listDatabaseSchema ||
        stateStage ||
        statePredictionStage ||
        stateQueryId ||
        listTransformCount !== null ||
        listCandidateCount !== null ||
        listBucketCount !== null ||
        listPredictionTotal !== null ||
        listPredictionQueued !== null ||
        listPredictionRunning !== null ||
        listPredictionSuccess !== null ||
        listPredictionFailure !== null ||
        statePredictionTotal !== null ||
        statePredictionQueued !== null ||
        statePredictionRunning !== null ||
        statePredictionSuccess !== null ||
        statePredictionFailure !== null ||
        statePredictionTaskId ||
        statePredictionCandidateSmiles ||
        Object.keys(listSelection).length > 0 ||
        Object.keys(listQueryResult).length > 0 ||
        listEnumeratedCandidates.length > 0 ||
        Object.keys(statePredictionBySmiles).length > 0 ||
        Object.keys(stateReferencePredictionByBackend).length > 0
    );
    if (!hasAnyLeadOptMeta) return null;

    const normalizedPredictionSummary = {
      total: statePredictionTotal ?? listPredictionTotal ?? 0,
      queued: statePredictionQueued ?? listPredictionQueued ?? 0,
      running: statePredictionRunning ?? listPredictionRunning ?? 0,
      success: statePredictionSuccess ?? listPredictionSuccess ?? 0,
      failure: statePredictionFailure ?? listPredictionFailure ?? 0,
    };

    return {
      lead_opt_list: {
        stage: listStage,
        prediction_stage: listPredictionStage || statePredictionStage,
        query_id: listQueryId || stateQueryId,
        transform_count: listTransformCount,
        candidate_count: listCandidateCount,
        bucket_count: listBucketCount,
        mmp_database_id: listDatabaseId,
        mmp_database_label: listDatabaseLabel,
        mmp_database_schema: listDatabaseSchema,
        selection:
          Object.keys(listSelection).length > 0
            ? listSelection
            : {
                selected_fragment_ids: listSelectedFragmentIds,
                selected_fragment_atom_indices: listSelectedAtomIndices,
                variable_queries: listSelectedFragmentQuery ? [listSelectedFragmentQuery] : []
              },
        selected_fragment_ids: listSelectedFragmentIds,
        selected_fragment_atom_indices: listSelectedAtomIndices,
        selected_fragment_query: listSelectedFragmentQuery,
        prediction_summary: normalizedPredictionSummary,
        query_result: listQueryResult,
        ui_state: {},
        enumerated_candidates: listEnumeratedCandidates
      },
      lead_opt_state: {
        stage: stateStage || listStage,
        prediction_stage: statePredictionStage || listPredictionStage,
        query_id: stateQueryId || listQueryId,
        prediction_task_id: statePredictionTaskId,
        prediction_candidate_smiles: statePredictionCandidateSmiles,
        prediction_summary: normalizedPredictionSummary,
        prediction_by_smiles: statePredictionBySmiles,
        reference_prediction_by_backend: stateReferencePredictionByBackend
      }
    };
  };

  const mergedRows = rows.map((row) => {
    const detail = detailById.get(String(row.id || '').trim()) || {};
    const rowRecord = row as unknown as Record<string, unknown>;
    const leadOptSummaryProperties = includeLeadOptSummary ? buildLeadOptSummaryProperties(rowRecord) : null;
    const summaryProperties = includePropertiesSummary
      ? (() => {
          const peptidePreview = compactObjectRecord({
            design_mode: readText(rowRecord.properties_peptide_preview_design_mode),
            binder_length: readFiniteNumber(rowRecord.properties_peptide_preview_binder_length),
            iterations: readFiniteNumber(rowRecord.properties_peptide_preview_iterations),
            population_size: readFiniteNumber(rowRecord.properties_peptide_preview_population_size),
            elite_size: readFiniteNumber(rowRecord.properties_peptide_preview_elite_size),
            mutation_rate: readFiniteNumber(rowRecord.properties_peptide_preview_mutation_rate),
            current_generation: readFiniteNumber(rowRecord.properties_peptide_preview_current_generation),
            total_generations: readFiniteNumber(rowRecord.properties_peptide_preview_total_generations),
            best_score: readFiniteNumber(rowRecord.properties_peptide_preview_best_score),
            candidate_count: readFiniteNumber(rowRecord.properties_peptide_preview_candidate_count),
            completed_tasks: readFiniteNumber(rowRecord.properties_peptide_preview_completed_tasks),
            pending_tasks: readFiniteNumber(rowRecord.properties_peptide_preview_pending_tasks),
            total_tasks: readFiniteNumber(rowRecord.properties_peptide_preview_total_tasks),
            current_status: readText(rowRecord.properties_peptide_preview_current_status),
            status_message: readText(rowRecord.properties_peptide_preview_status_message),
            best_candidate:
              rowRecord.properties_peptide_preview_best_candidate &&
              typeof rowRecord.properties_peptide_preview_best_candidate === 'object' &&
              !Array.isArray(rowRecord.properties_peptide_preview_best_candidate)
                ? (rowRecord.properties_peptide_preview_best_candidate as Record<string, unknown>)
                : undefined
          });
          return compactObjectRecord({
            target: readText(rowRecord.properties_target),
            ligand: readText(rowRecord.properties_ligand),
            binder: readText(rowRecord.properties_binder),
            __vbio_input_options_v1: compactObjectRecord({
              affinityMode: readText(rowRecord.properties_affinity_mode)
            }),
            ...(Object.keys(peptidePreview).length > 0 ? { [PEPTIDE_TASK_PREVIEW_KEY]: peptidePreview } : {})
          });
        })()
      : {};
    const summaryConfidence = (() => {
      if (!includeConfidenceSummary) return {};
      const next: Record<string, unknown> = {};
      const chainMeanPlddt = asObjectRecord(rowRecord.confidence_chain_mean_plddt);
      const pairChainsIptm = asObjectRecord(rowRecord.confidence_pair_chains_iptm);
      const ligandDisplayAtomPlddtsByChain = asObjectRecord(rowRecord.confidence_ligand_display_atom_plddts_by_chain);
      const ligandAtomPlddtsByChain = asObjectRecord(rowRecord.confidence_ligand_atom_plddts_by_chain);
      const chainIdsFromSummary = Array.from(
        new Set(
          [
            ...(Array.isArray(rowRecord.confidence_chain_ids)
              ? rowRecord.confidence_chain_ids.map((value: unknown) => readText(value)).filter(Boolean)
              : []),
            ...Object.keys(chainMeanPlddt).map((value) => readText(value)).filter(Boolean),
            ...Object.keys(ligandDisplayAtomPlddtsByChain).map((value) => readText(value)).filter(Boolean),
            ...Object.keys(ligandAtomPlddtsByChain).map((value) => readText(value)).filter(Boolean)
          ]
        )
      );
      const explicitLigandChainId =
        readText(rowRecord.confidence_ligand_chain_id) ||
        readText(rowRecord.confidence_model_ligand_chain_id) ||
        readText(rowRecord.confidence_binder_chain_id) ||
        readText(rowRecord.confidence_requested_ligand_chain_id);
      const derivedLigandChainId =
        explicitLigandChainId ||
        (Object.keys(ligandDisplayAtomPlddtsByChain).length === 1
          ? readText(Object.keys(ligandDisplayAtomPlddtsByChain)[0])
          : Object.keys(ligandAtomPlddtsByChain).length === 1
            ? readText(Object.keys(ligandAtomPlddtsByChain)[0])
            : '');
      const explicitTargetChainId =
        readText(rowRecord.confidence_target_chain) ||
        readText(rowRecord.confidence_target_chain_id) ||
        readText(rowRecord.confidence_requested_target_chain_id) ||
        readText(rowRecord.confidence_protein_chain_id);
      const derivedTargetChainId =
        explicitTargetChainId ||
        chainIdsFromSummary.find((chainId) => !derivedLigandChainId || chainId !== derivedLigandChainId) ||
        '';
      const assignText = (key: string, value: unknown) => {
        const text = readText(value);
        if (text) next[key] = text;
      };
      const assignNumber = (key: string, value: unknown) => {
        const numeric = readFiniteNumber(value);
        if (numeric !== null) next[key] = numeric;
      };
      const assignObject = (key: string, value: unknown) => {
        const objectValue = asObjectRecord(value);
        if (Object.keys(objectValue).length > 0) next[key] = objectValue;
      };
      const assignArray = (key: string, value: unknown) => {
        if (Array.isArray(value) && value.length > 0) next[key] = value;
      };

      assignText('ligand_smiles', rowRecord.confidence_ligand_smiles);
      assignText('ligand_display_smiles', rowRecord.confidence_ligand_display_smiles);
      assignText('requested_target_chain_id', rowRecord.confidence_requested_target_chain_id);
      assignText('target_chain_id', rowRecord.confidence_target_chain_id || derivedTargetChainId);
      assignText('target_chain', rowRecord.confidence_target_chain || derivedTargetChainId);
      assignText('protein_chain_id', rowRecord.confidence_protein_chain_id || derivedTargetChainId);
      assignText('requested_ligand_chain_id', rowRecord.confidence_requested_ligand_chain_id || derivedLigandChainId);
      assignText('ligand_chain_id', rowRecord.confidence_ligand_chain_id || derivedLigandChainId);
      assignText('model_ligand_chain_id', rowRecord.confidence_model_ligand_chain_id || derivedLigandChainId);
      assignText('binder_chain_id', rowRecord.confidence_binder_chain_id || derivedLigandChainId);
      assignArray('chain_ids', chainIdsFromSummary);
      assignObject('chain_mean_plddt', chainMeanPlddt);
      assignObject('pair_chains_iptm', pairChainsIptm);
      assignArray('chain_pair_iptm', rowRecord.confidence_chain_pair_iptm);
      assignArray('chain_pair_iptm_global', rowRecord.confidence_chain_pair_iptm_global);
      assignObject('ligand_display_atom_plddts_by_chain', ligandDisplayAtomPlddtsByChain);
      assignObject('ligand_atom_plddts_by_chain', ligandAtomPlddtsByChain);
      assignNumber('ligand_plddt', rowRecord.confidence_ligand_plddt);
      assignNumber('ligand_mean_plddt', rowRecord.confidence_ligand_mean_plddt);
      assignNumber('complex_iplddt', rowRecord.confidence_complex_iplddt);
      assignNumber('complex_plddt_protein', rowRecord.confidence_complex_plddt_protein);
      assignNumber('complex_plddt', rowRecord.confidence_complex_plddt);
      assignNumber('plddt', rowRecord.confidence_plddt);
      assignNumber('ipsae_dom', rowRecord.confidence_ipsae_dom);
      assignNumber('ligand_ipsae_max', rowRecord.confidence_ligand_ipsae_max);
      assignNumber('iptm', rowRecord.confidence_iptm);
      assignNumber('ligand_iptm', rowRecord.confidence_ligand_iptm);
      assignNumber('protein_iptm', rowRecord.confidence_protein_iptm);
      assignNumber('complex_pde', rowRecord.confidence_complex_pde);
      assignNumber('complex_pae', rowRecord.confidence_complex_pae);
      assignNumber('gpde', rowRecord.confidence_gpde);
      assignNumber('pae', rowRecord.confidence_pae);

      const ligandDisplay: Record<string, unknown> = {};
      const displaySmiles = readText(rowRecord.confidence_ligand_display_smiles);
      if (displaySmiles) ligandDisplay.smiles = displaySmiles;
      if (Object.keys(ligandDisplayAtomPlddtsByChain).length > 0) ligandDisplay.atom_plddts_by_chain = ligandDisplayAtomPlddtsByChain;
      if (Array.isArray(rowRecord.confidence_ligand_display_atom_plddts) && rowRecord.confidence_ligand_display_atom_plddts.length > 0) {
        ligandDisplay.atom_plddts = rowRecord.confidence_ligand_display_atom_plddts;
      }
      if (Object.keys(ligandDisplay).length > 0) next.ligand_display = ligandDisplay;

      const ligand: Record<string, unknown> = {};
      const ligandSmiles = readText(rowRecord.confidence_ligand_smiles);
      if (ligandSmiles) ligand.smiles = ligandSmiles;
      if (Object.keys(ligandAtomPlddtsByChain).length > 0) ligand.atom_plddts_by_chain = ligandAtomPlddtsByChain;
      if (Array.isArray(rowRecord.confidence_ligand_atom_plddts) && rowRecord.confidence_ligand_atom_plddts.length > 0) {
        ligand.atom_plddts = rowRecord.confidence_ligand_atom_plddts;
      }
      if (Object.keys(ligand).length > 0) next.ligand = ligand;

      return next;
    })();
    return {
      ...row,
      components: Array.isArray((detail as any).components) ? (detail as any).components : [],
      properties:
        includeProperties
          ? row.properties
          : leadOptSummaryProperties || summaryProperties,
      confidence: includeConfidence ? row.confidence : summaryConfidence,
    };
  });

  const scope = options?.accessScope || 'owner';
  const accessLevel = options?.accessLevel || (scope === 'owner' ? 'owner' : 'viewer');
  const editableTaskIdSet = buildEditableTaskIdSet(options?.editableTaskIds || []);
  return mergedRows.map((row) => {
    const normalizedProperties = includeProperties
      ? (row.properties && typeof row.properties === 'object' && !Array.isArray(row.properties)
          ? (row.properties as ProjectTask['properties'])
          : {
              affinity: false,
              target: null,
              ligand: null,
              binder: null
            }) as ProjectTask['properties']
      : (row.properties && typeof row.properties === 'object' && !Array.isArray(row.properties)
          ? (row.properties as ProjectTask['properties'])
          : {}) as ProjectTask['properties'];
    return applyTaskAccess({
      name: '',
      summary: '',
      protein_sequence: '',
      affinity: {},
      constraints: [],
      ...row,
      confidence:
        row.confidence && typeof row.confidence === 'object' && !Array.isArray(row.confidence)
          ? (row.confidence as ProjectTask['confidence'])
          : ({} as ProjectTask['confidence']),
      properties: normalizedProperties
    } as ProjectTask, scope, accessLevel, editableTaskIdSet);
  }) as ProjectTask[];
}

export async function getProjectTaskById(
  taskRowId: string,
  options?: {
    includeComponents?: boolean;
    includeConstraints?: boolean;
    includeProperties?: boolean;
    includeLeadOptSummary?: boolean;
    includeLeadOptCandidates?: boolean;
    includeConfidence?: boolean;
    includeAffinity?: boolean;
    includeProteinSequence?: boolean;
  }
): Promise<ProjectTask | null> {
  const normalizedTaskRowId = String(taskRowId || '').trim();
  if (!normalizedTaskRowId) return null;
  const includeComponents = options?.includeComponents !== false;
  const includeConstraints = options?.includeConstraints !== false;
  const includeProperties = options?.includeProperties !== false;
  const includeLeadOptSummary = options?.includeLeadOptSummary === true;
  const includeLeadOptCandidates = options?.includeLeadOptCandidates === true;
  const includeConfidence = options?.includeConfidence !== false;
  const includeAffinity = options?.includeAffinity !== false;
  const includeProteinSequence = options?.includeProteinSequence !== false;
  const selectFields = [
    'id',
    'project_id',
    'name',
    'summary',
    'task_id',
    'task_state',
    'status_text',
    'error_text',
    ...(includeProteinSequence ? ['protein_sequence'] : []),
    'backend',
    'seed',
    'ligand_smiles',
    ...(includeAffinity ? ['affinity'] : []),
    ...(includeConfidence ? ['confidence'] : []),
    ...(includeComponents ? ['components'] : []),
    ...(includeConstraints ? ['constraints'] : []),
    ...(includeProperties ? ['properties'] : []),
    ...(includeLeadOptSummary
      ? [
          'lead_opt_list_stage:properties->lead_opt_list->>stage',
          'lead_opt_list_prediction_stage:properties->lead_opt_list->>prediction_stage',
          'lead_opt_list_query_id:properties->lead_opt_list->>query_id',
          'lead_opt_list_transform_count:properties->lead_opt_list->>transform_count',
          'lead_opt_list_candidate_count:properties->lead_opt_list->>candidate_count',
          'lead_opt_list_bucket_count:properties->lead_opt_list->>bucket_count',
          'lead_opt_list_database_id:properties->lead_opt_list->>mmp_database_id',
          'lead_opt_list_database_label:properties->lead_opt_list->>mmp_database_label',
          'lead_opt_list_database_schema:properties->lead_opt_list->>mmp_database_schema',
          'lead_opt_list_selected_fragment_ids:properties->lead_opt_list->selection->selected_fragment_ids',
          'lead_opt_list_selected_atom_indices:properties->lead_opt_list->selection->selected_fragment_atom_indices',
          'lead_opt_list_selected_fragment_query:properties->lead_opt_list->>selected_fragment_query',
          'lead_opt_list_prediction_total:properties->lead_opt_list->prediction_summary->>total',
          'lead_opt_list_prediction_queued:properties->lead_opt_list->prediction_summary->>queued',
          'lead_opt_list_prediction_running:properties->lead_opt_list->prediction_summary->>running',
          'lead_opt_list_prediction_success:properties->lead_opt_list->prediction_summary->>success',
          'lead_opt_list_prediction_failure:properties->lead_opt_list->prediction_summary->>failure',
          'lead_opt_list_selection:properties->lead_opt_list->selection',
          'lead_opt_list_query_result_query_id:properties->lead_opt_list->query_result->>query_id',
          'lead_opt_list_query_result_query_mode:properties->lead_opt_list->query_result->>query_mode',
          'lead_opt_list_query_result_aggregation_type:properties->lead_opt_list->query_result->>aggregation_type',
          'lead_opt_list_query_result_property_targets:properties->lead_opt_list->query_result->property_targets',
          'lead_opt_list_query_result_rule_env_radius:properties->lead_opt_list->query_result->>rule_env_radius',
          'lead_opt_list_query_result_grouped_by_environment:properties->lead_opt_list->query_result->>grouped_by_environment',
          'lead_opt_list_query_result_cluster_group_by:properties->lead_opt_list->query_result->>cluster_group_by',
          'lead_opt_list_query_result_min_pairs:properties->lead_opt_list->query_result->>min_pairs',
          'lead_opt_list_query_result_count:properties->lead_opt_list->query_result->>count',
          'lead_opt_list_query_result_global_count:properties->lead_opt_list->query_result->>global_count',
          'lead_opt_list_query_result_stats:properties->lead_opt_list->query_result->stats',
          'lead_opt_list_query_result_database_id:properties->lead_opt_list->query_result->>mmp_database_id',
          'lead_opt_list_query_result_database_label:properties->lead_opt_list->query_result->>mmp_database_label',
          'lead_opt_list_query_result_database_schema:properties->lead_opt_list->query_result->>mmp_database_schema',
          ...(includeLeadOptCandidates
            ? ['lead_opt_list_enumerated_candidates:properties->lead_opt_list->enumerated_candidates']
            : []),
          'lead_opt_state_stage:properties->lead_opt_state->>stage',
          'lead_opt_state_prediction_stage:properties->lead_opt_state->>prediction_stage',
          'lead_opt_state_query_id:properties->lead_opt_state->>query_id',
          'lead_opt_state_prediction_total:properties->lead_opt_state->prediction_summary->>total',
          'lead_opt_state_prediction_queued:properties->lead_opt_state->prediction_summary->>queued',
          'lead_opt_state_prediction_running:properties->lead_opt_state->prediction_summary->>running',
          'lead_opt_state_prediction_success:properties->lead_opt_state->prediction_summary->>success',
          'lead_opt_state_prediction_failure:properties->lead_opt_state->prediction_summary->>failure',
          'lead_opt_state_prediction_by_smiles:properties->lead_opt_state->prediction_by_smiles',
          'lead_opt_state_reference_prediction_by_backend:properties->lead_opt_state->reference_prediction_by_backend',
          'lead_opt_state_prediction_task_id:properties->lead_opt_state->>prediction_task_id',
          'lead_opt_state_prediction_candidate_smiles:properties->lead_opt_state->>prediction_candidate_smiles',
        ]
      : []),
    'structure_name',
    'submitted_at',
    'completed_at',
    'duration_seconds',
    'created_at',
    'updated_at'
  ].join(',');
  const rows = await request<Array<Partial<ProjectTask>>>('/project_tasks', undefined, {
    select: selectFields,
    id: `eq.${normalizedTaskRowId}`,
    limit: '1'
  });
  const row = rows[0];
  if (!row) return null;
  const rowRecord = row as unknown as Record<string, unknown>;
  const readText = (value: unknown): string => {
    if (value === null || value === undefined) return '';
    return String(value).trim();
  };
  const readFiniteNumber = (value: unknown): number | null => {
    const parsed = typeof value === 'number' ? value : typeof value === 'string' ? Number(value.trim()) : Number.NaN;
    if (!Number.isFinite(parsed)) return null;
    return parsed;
  };
  const readStringArray = (value: unknown): string[] => {
    if (!Array.isArray(value)) return [];
    return Array.from(
      new Set(
        value
          .map((item) => String(item || '').trim())
          .filter(Boolean)
      )
    );
  };
  const readIntegerArray = (value: unknown): number[] => {
    if (!Array.isArray(value)) return [];
    return Array.from(
      new Set(
        value
          .map((item) => Number(item))
          .filter((item) => Number.isFinite(item) && item >= 0)
          .map((item) => Math.floor(item))
      )
    );
  };

  const normalizedProperties = (() => {
    if (includeProperties) {
      if (row.properties && typeof row.properties === 'object' && !Array.isArray(row.properties)) {
        return row.properties as ProjectTask['properties'];
      }
      return {
        affinity: false,
        target: null,
        ligand: null,
        binder: null
      } as ProjectTask['properties'];
    }
    if (!includeLeadOptSummary) {
      return {} as ProjectTask['properties'];
    }
    const listStage = readText(rowRecord.lead_opt_list_stage);
    const listPredictionStage = readText(rowRecord.lead_opt_list_prediction_stage);
    const listQueryId = readText(rowRecord.lead_opt_list_query_id);
    const listTransformCount = readFiniteNumber(rowRecord.lead_opt_list_transform_count);
    const listCandidateCount = readFiniteNumber(rowRecord.lead_opt_list_candidate_count);
    const listBucketCount = readFiniteNumber(rowRecord.lead_opt_list_bucket_count);
    const listDatabaseId = readText(rowRecord.lead_opt_list_database_id);
    const listDatabaseLabel = readText(rowRecord.lead_opt_list_database_label);
    const listDatabaseSchema = readText(rowRecord.lead_opt_list_database_schema);
    const listSelectedFragmentIds = readStringArray(rowRecord.lead_opt_list_selected_fragment_ids);
    const listSelectedAtomIndices = readIntegerArray(rowRecord.lead_opt_list_selected_atom_indices);
    const listSelectedFragmentQuery = readText(rowRecord.lead_opt_list_selected_fragment_query);
    const listPredictionTotal = readFiniteNumber(rowRecord.lead_opt_list_prediction_total);
    const listPredictionQueued = readFiniteNumber(rowRecord.lead_opt_list_prediction_queued);
    const listPredictionRunning = readFiniteNumber(rowRecord.lead_opt_list_prediction_running);
    const listPredictionSuccess = readFiniteNumber(rowRecord.lead_opt_list_prediction_success);
    const listPredictionFailure = readFiniteNumber(rowRecord.lead_opt_list_prediction_failure);
    const listQueryResultGroupedByEnvironment = parseBooleanToken(rowRecord.lead_opt_list_query_result_grouped_by_environment);
    const listQueryResult = compactObjectRecord({
      query_id: readText(rowRecord.lead_opt_list_query_result_query_id) || listQueryId,
      query_mode: readText(rowRecord.lead_opt_list_query_result_query_mode),
      aggregation_type: readText(rowRecord.lead_opt_list_query_result_aggregation_type),
      property_targets: asObjectRecord(rowRecord.lead_opt_list_query_result_property_targets),
      rule_env_radius: readFiniteNumber(rowRecord.lead_opt_list_query_result_rule_env_radius),
      grouped_by_environment:
        listQueryResultGroupedByEnvironment === null ? undefined : listQueryResultGroupedByEnvironment,
      cluster_group_by: readText(rowRecord.lead_opt_list_query_result_cluster_group_by),
      min_pairs: readFiniteNumber(rowRecord.lead_opt_list_query_result_min_pairs),
      count: readFiniteNumber(rowRecord.lead_opt_list_query_result_count),
      global_count: readFiniteNumber(rowRecord.lead_opt_list_query_result_global_count),
      stats: asObjectRecord(rowRecord.lead_opt_list_query_result_stats),
      mmp_database_id:
        readText(rowRecord.lead_opt_list_query_result_database_id) || listDatabaseId,
      mmp_database_label:
        readText(rowRecord.lead_opt_list_query_result_database_label) || listDatabaseLabel,
      mmp_database_schema:
        readText(rowRecord.lead_opt_list_query_result_database_schema) || listDatabaseSchema
    });

    const stateStage = readText(rowRecord.lead_opt_state_stage);
    const statePredictionStage = readText(rowRecord.lead_opt_state_prediction_stage);
    const stateQueryId = readText(rowRecord.lead_opt_state_query_id);
    const statePredictionTotal = readFiniteNumber(rowRecord.lead_opt_state_prediction_total);
    const statePredictionQueued = readFiniteNumber(rowRecord.lead_opt_state_prediction_queued);
    const statePredictionRunning = readFiniteNumber(rowRecord.lead_opt_state_prediction_running);
    const statePredictionSuccess = readFiniteNumber(rowRecord.lead_opt_state_prediction_success);
    const statePredictionFailure = readFiniteNumber(rowRecord.lead_opt_state_prediction_failure);
    const statePredictionTaskId = readText(rowRecord.lead_opt_state_prediction_task_id);
    const statePredictionCandidateSmiles = readText(rowRecord.lead_opt_state_prediction_candidate_smiles);

    const hasAnyLeadOptMeta = Boolean(
      listStage ||
        listPredictionStage ||
        listQueryId ||
        listDatabaseId ||
        listDatabaseLabel ||
        listDatabaseSchema ||
        stateStage ||
        statePredictionStage ||
        stateQueryId ||
        statePredictionTaskId ||
        statePredictionCandidateSmiles ||
        listTransformCount !== null ||
        listCandidateCount !== null ||
        listBucketCount !== null ||
        listPredictionTotal !== null ||
        listPredictionQueued !== null ||
        listPredictionRunning !== null ||
        listPredictionSuccess !== null ||
        listPredictionFailure !== null ||
        statePredictionTotal !== null ||
        statePredictionQueued !== null ||
        statePredictionRunning !== null ||
        statePredictionSuccess !== null ||
        statePredictionFailure !== null ||
        Object.keys(listQueryResult).length > 0
    );
    if (!hasAnyLeadOptMeta) return {} as ProjectTask['properties'];

    const normalizedPredictionSummary = {
      total: statePredictionTotal ?? listPredictionTotal ?? 0,
      queued: statePredictionQueued ?? listPredictionQueued ?? 0,
      running: statePredictionRunning ?? listPredictionRunning ?? 0,
      success: statePredictionSuccess ?? listPredictionSuccess ?? 0,
      failure: statePredictionFailure ?? listPredictionFailure ?? 0,
    };

    return {
      lead_opt_list: {
        stage: listStage,
        prediction_stage: listPredictionStage || statePredictionStage,
        query_id: listQueryId || stateQueryId,
        transform_count: listTransformCount,
        candidate_count: listCandidateCount,
        bucket_count: listBucketCount,
        mmp_database_id: listDatabaseId,
        mmp_database_label: listDatabaseLabel,
        mmp_database_schema: listDatabaseSchema,
        selection:
          rowRecord.lead_opt_list_selection && typeof rowRecord.lead_opt_list_selection === 'object' && !Array.isArray(rowRecord.lead_opt_list_selection)
            ? rowRecord.lead_opt_list_selection
            : {
                selected_fragment_ids: listSelectedFragmentIds,
                selected_fragment_atom_indices: listSelectedAtomIndices,
                variable_queries: listSelectedFragmentQuery ? [listSelectedFragmentQuery] : []
              },
        selected_fragment_ids: listSelectedFragmentIds,
        selected_fragment_atom_indices: listSelectedAtomIndices,
        selected_fragment_query: listSelectedFragmentQuery,
        prediction_summary: normalizedPredictionSummary,
        query_result: listQueryResult,
        ui_state: {},
        enumerated_candidates:
          includeLeadOptCandidates && Array.isArray(rowRecord.lead_opt_list_enumerated_candidates)
            ? rowRecord.lead_opt_list_enumerated_candidates
            : []
      },
      lead_opt_state: {
        stage: stateStage || listStage,
        prediction_stage: statePredictionStage || listPredictionStage,
        query_id: stateQueryId || listQueryId,
        prediction_summary: normalizedPredictionSummary,
        prediction_task_id: statePredictionTaskId,
        prediction_candidate_smiles: statePredictionCandidateSmiles,
        prediction_by_smiles:
          rowRecord.lead_opt_state_prediction_by_smiles &&
          typeof rowRecord.lead_opt_state_prediction_by_smiles === 'object' &&
          !Array.isArray(rowRecord.lead_opt_state_prediction_by_smiles)
            ? rowRecord.lead_opt_state_prediction_by_smiles
            : {},
        reference_prediction_by_backend:
          rowRecord.lead_opt_state_reference_prediction_by_backend &&
          typeof rowRecord.lead_opt_state_reference_prediction_by_backend === 'object' &&
          !Array.isArray(rowRecord.lead_opt_state_reference_prediction_by_backend)
            ? rowRecord.lead_opt_state_reference_prediction_by_backend
            : {}
      }
    } as unknown as ProjectTask['properties'];
  })();

  return {
    name: '',
    summary: '',
    protein_sequence: '',
    confidence: {},
    affinity: {},
    components: [],
    constraints: [],
    ...row,
    properties: normalizedProperties
  } as ProjectTask;
}

function stripTemplateContentFromTaskComponents(components: unknown): unknown {
  if (!Array.isArray(components)) return components;
  return components.map((component) => {
    if (!component || typeof component !== 'object' || Array.isArray(component)) return component;
    const next = { ...(component as Record<string, unknown>) };
    const camelUpload = next.templateUpload;
    if (camelUpload && typeof camelUpload === 'object' && !Array.isArray(camelUpload)) {
      const compactUpload = { ...(camelUpload as Record<string, unknown>) };
      delete compactUpload.content;
      next.templateUpload = compactUpload;
    }
    const snakeUpload = next.template_upload;
    if (snakeUpload && typeof snakeUpload === 'object' && !Array.isArray(snakeUpload)) {
      const compactUpload = { ...(snakeUpload as Record<string, unknown>) };
      delete compactUpload.content;
      next.template_upload = compactUpload;
    }
    return next;
  });
}

const AFFINITY_TARGET_UPLOAD_COMPONENT_ID = '__affinity_target_upload__';
const AFFINITY_LIGAND_UPLOAD_COMPONENT_ID = '__affinity_ligand_upload__';

function affinityUploadRoleFromTaskComponent(component: Record<string, unknown>): 'target' | 'ligand' | null {
  const uploadMeta =
    component.affinityUpload && typeof component.affinityUpload === 'object' && !Array.isArray(component.affinityUpload)
      ? (component.affinityUpload as Record<string, unknown>)
      : component.affinity_upload && typeof component.affinity_upload === 'object' && !Array.isArray(component.affinity_upload)
        ? (component.affinity_upload as Record<string, unknown>)
        : null;
  const roleFromMeta = uploadMeta?.role;
  if (roleFromMeta === 'target' || roleFromMeta === 'ligand') return roleFromMeta;
  const componentId = typeof component.id === 'string' ? component.id.trim() : '';
  if (componentId === AFFINITY_TARGET_UPLOAD_COMPONENT_ID) return 'target';
  if (componentId === AFFINITY_LIGAND_UPLOAD_COMPONENT_ID) return 'ligand';
  return null;
}

function normalizeAffinityUploadMeta(
  raw: unknown,
  role: 'target' | 'ligand'
): Record<string, unknown> | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const next = { ...(raw as Record<string, unknown>) };
  next.role = role;

  if (typeof next.fileName === 'string') {
    const trimmed = next.fileName.trim();
    if (trimmed) {
      next.fileName = trimmed;
    } else {
      delete next.fileName;
    }
  } else {
    delete next.fileName;
  }

  if (typeof next.content !== 'string') {
    delete next.content;
  }

  return next;
}

function stripAffinityUploadContentFromTaskComponents(components: unknown): unknown {
  if (!Array.isArray(components)) return components;
  return components.map((component) => {
    if (!component || typeof component !== 'object' || Array.isArray(component)) return component;
    const role = affinityUploadRoleFromTaskComponent(component as Record<string, unknown>);
    if (!role) return component;
    const next = { ...(component as Record<string, unknown>) };
    const normalizedCamel = normalizeAffinityUploadMeta(next.affinityUpload, role);
    const normalizedSnake = normalizeAffinityUploadMeta(next.affinity_upload, role);
    if (normalizedCamel) {
      next.affinityUpload = normalizedCamel;
    } else {
      delete next.affinityUpload;
    }
    if (normalizedSnake) {
      next.affinity_upload = normalizedSnake;
    } else {
      delete next.affinity_upload;
    }
    return next;
  });
}

function sanitizeProjectTaskWritePayload(payload: Partial<ProjectTask>): Partial<ProjectTask> {
  if (!Object.prototype.hasOwnProperty.call(payload, 'components')) return payload;
  const compactComponents = stripTemplateContentFromTaskComponents((payload as { components?: unknown }).components);
  return {
    ...payload,
    components: stripAffinityUploadContentFromTaskComponents(compactComponents) as ProjectTask['components']
  };
}

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

export async function listRuntimeCandidateProjectTaskStatesByProjects(projectIds: string[]): Promise<ProjectTaskRuntimeRow[]> {
  const normalizedIds = Array.from(new Set(projectIds.map((id) => id.trim()).filter(Boolean)));
  if (normalizedIds.length === 0) return [];

  const chunkSize = 150;
  const chunks: string[][] = [];
  for (let i = 0; i < normalizedIds.length; i += chunkSize) {
    chunks.push(normalizedIds.slice(i, i + chunkSize));
  }

  const results = await Promise.all(
    chunks.map((chunk) =>
      request<ProjectTaskRuntimeRow[]>('/project_tasks', undefined, {
        select: 'id,project_id,task_id,task_state,status_text,error_text,submitted_at,completed_at,duration_seconds',
        project_id: `in.(${chunk.join(',')})`,
        task_id: 'neq.',
        completed_at: 'is.null'
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
  return rows[0];
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
  return rows[0];
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

export async function updateProjectTaskByTaskId(
  taskId: string,
  patch: Partial<ProjectTask>,
  projectId?: string
): Promise<ProjectTask | null> {
  const current = await findProjectTaskByTaskId(taskId, projectId);
  if (!current) return null;
  return updateProjectTask(current.id, patch);
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

function normalizeChatMessageRole(value: unknown): ProjectCopilotMessage['role'] {
  const token = String(value || '').trim().toLowerCase();
  if (token === 'assistant') return 'assistant';
  if (token === 'system') return 'system';
  return 'user';
}

function isMissingRelationError(error: unknown, relationName: string): boolean {
  const message = error instanceof Error ? error.message : String(error || '');
  return message.includes('42P01') || message.includes(`relation "public.${relationName}" does not exist`);
}

function createLocalId(prefix: string): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `local:${crypto.randomUUID()}`;
  }
  return `local:${prefix}:${Date.now()}:${Math.random().toString(36).slice(2)}`;
}

function readLocalRows<T>(key: string): T[] {
  if (typeof window === 'undefined') return [];
  try {
    const parsed = JSON.parse(window.localStorage.getItem(key) || '[]');
    return Array.isArray(parsed) ? (parsed as T[]) : [];
  } catch {
    return [];
  }
}

function writeLocalRows<T>(key: string, rows: T[]) {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(key, JSON.stringify(rows.slice(-200)));
}

function copilotLocalStorageKey(input: {
  contextType: ProjectCopilotMessage['context_type'];
  projectId?: string | null;
  projectTaskId?: string | null;
}): string {
  return [
    'vbio:project-copilot:v1',
    normalizeCopilotContextType(input.contextType),
    String(input.projectId || 'project-null'),
    String(input.projectTaskId || 'task-null')
  ].join(':');
}

function normalizeCopilotContextType(value: unknown): ProjectCopilotMessage['context_type'] {
  const token = String(value || '').trim().toLowerCase();
  if (token === 'project_list') return 'project_list';
  if (token === 'task_detail') return 'task_detail';
  return 'task_list';
}

function normalizeProjectCopilotMessage(row: Partial<ProjectCopilotMessage>): ProjectCopilotMessage {
  return {
    id: String(row.id || ''),
    context_type: normalizeCopilotContextType(row.context_type),
    project_id: row.project_id ? String(row.project_id) : null,
    project_task_id: row.project_task_id ? String(row.project_task_id) : null,
    user_id: row.user_id ? String(row.user_id) : null,
    role: normalizeChatMessageRole(row.role),
    content: String(row.content || ''),
    metadata: asObjectRecord(row.metadata),
    created_at: String(row.created_at || ''),
    updated_at: String(row.updated_at || ''),
    username: String(row.username || '').trim(),
    user_name: String(row.user_name || '').trim()
  };
}

export async function listProjectCopilotMessages(input: {
  contextType: ProjectCopilotMessage['context_type'];
  projectId?: string | null;
  projectTaskId?: string | null;
  userId?: string | null;
}): Promise<ProjectCopilotMessage[]> {
  const contextType = normalizeCopilotContextType(input.contextType);
  const query: Record<string, string> = {
    select: '*',
    context_type: `eq.${contextType}`,
    order: 'created_at.asc',
    limit: '200'
  };
  const projectId = String(input.projectId || '').trim();
  const projectTaskId = String(input.projectTaskId || '').trim();
  query.project_id = projectId ? `eq.${projectId}` : 'is.null';
  query.project_task_id = projectTaskId ? `eq.${projectTaskId}` : 'is.null';
  let rows: Array<Partial<ProjectCopilotMessage>>;
  try {
    rows = await request<Array<Partial<ProjectCopilotMessage>>>('/project_copilot_messages', undefined, query);
  } catch (error) {
    if (!isMissingRelationError(error, 'project_copilot_messages')) throw error;
    const normalizedUserId = String(input.userId || '').trim();
    return readLocalRows<Partial<ProjectCopilotMessage>>(copilotLocalStorageKey({ contextType, projectId, projectTaskId }))
      .map(normalizeProjectCopilotMessage)
      .filter((row) => {
        if (!normalizedUserId) return false;
        return row.user_id === normalizedUserId || String(row.metadata?.owner_user_id || '') === normalizedUserId;
      });
  }
  const normalizedUserId = String(input.userId || '').trim();
  rows = rows.filter((row) => {
    if (!normalizedUserId) return false;
    return String(row.user_id || '') === normalizedUserId || String(asObjectRecord(row.metadata).owner_user_id || '') === normalizedUserId;
  });
  const userIds = Array.from(new Set(rows.map((row) => String(row.user_id || '').trim()).filter(Boolean)));
  const users = await listUsersByIds(userIds);
  const userById = new Map(users.map((user) => [user.id, user] as const));
  return rows.map((row) => {
    const user = row.user_id ? userById.get(String(row.user_id)) : null;
    return normalizeProjectCopilotMessage({
      ...row,
      username: user?.username || '',
      user_name: user?.name || ''
    });
  });
}

export async function insertProjectCopilotMessage(input: {
  contextType: ProjectCopilotMessage['context_type'];
  projectId?: string | null;
  projectTaskId?: string | null;
  userId?: string | null;
  role?: ProjectCopilotMessage['role'];
  content: string;
  metadata?: Record<string, unknown>;
}): Promise<ProjectCopilotMessage> {
  const content = String(input.content || '').trim();
  if (!content) throw new Error('Message content is required.');
  const contextType = normalizeCopilotContextType(input.contextType);
  const ownerUserId = String(input.userId || input.metadata?.owner_user_id || '').trim();
  const metadata = {
    ...(input.metadata || {}),
    ...(ownerUserId ? { owner_user_id: ownerUserId } : {})
  };
  try {
    const rows = await request<ProjectCopilotMessage[]>(
      '/project_copilot_messages',
      {
        method: 'POST',
        headers: { Prefer: 'return=representation' },
        body: JSON.stringify({
          context_type: contextType,
          project_id: input.projectId || null,
          project_task_id: input.projectTaskId || null,
          user_id: input.userId || null,
          role: normalizeChatMessageRole(input.role),
          content,
          metadata
        })
      },
      { select: '*' }
    );
    return normalizeProjectCopilotMessage(rows[0] || {});
  } catch (error) {
    if (!isMissingRelationError(error, 'project_copilot_messages')) throw error;
    const now = new Date().toISOString();
    const row = normalizeProjectCopilotMessage({
      id: createLocalId('copilot'),
      context_type: contextType,
      project_id: input.projectId || null,
      project_task_id: input.projectTaskId || null,
      user_id: input.userId || null,
      role: normalizeChatMessageRole(input.role),
      content,
      metadata,
      created_at: now,
      updated_at: now
    });
    const key = copilotLocalStorageKey({ contextType, projectId: input.projectId || null, projectTaskId: input.projectTaskId || null });
    writeLocalRows(key, [...readLocalRows<ProjectCopilotMessage>(key), row]);
    return row;
  }
}

export async function deleteProjectCopilotMessagesBySession(input: {
  contextType: ProjectCopilotMessage['context_type'];
  projectId?: string | null;
  projectTaskId?: string | null;
  sessionId: string;
  userId?: string | null;
  messageIds?: string[];
}): Promise<void> {
  const sessionId = String(input.sessionId || '').trim();
  if (!sessionId) return;
  const contextType = normalizeCopilotContextType(input.contextType);
  const projectId = String(input.projectId || '').trim();
  const projectTaskId = String(input.projectTaskId || '').trim();
  const userId = String(input.userId || '').trim();
  const messageIds = Array.from(
    new Set((input.messageIds || []).map((value) => String(value || '').trim()).filter((value) => value && !value.startsWith('local:')))
  );
  if (messageIds.length > 0) {
    try {
      await request<unknown>('/project_copilot_messages', { method: 'DELETE' }, { id: buildInFilter(messageIds) });
    } catch (error) {
      if (!isMissingRelationError(error, 'project_copilot_messages')) throw error;
    }
  }
  const query: Record<string, string> = {
    context_type: `eq.${contextType}`,
    project_id: projectId ? `eq.${projectId}` : 'is.null',
    project_task_id: projectTaskId ? `eq.${projectTaskId}` : 'is.null',
    'metadata->>session_id': `eq.${sessionId}`,
    'metadata->>owner_user_id': `eq.${userId}`
  };
  try {
    await request<unknown>('/project_copilot_messages', { method: 'DELETE' }, query);
    if (userId) {
      await request<unknown>('/project_copilot_messages', { method: 'DELETE' }, {
        context_type: `eq.${contextType}`,
        project_id: projectId ? `eq.${projectId}` : 'is.null',
        project_task_id: projectTaskId ? `eq.${projectTaskId}` : 'is.null',
        'metadata->>session_id': `eq.${sessionId}`,
        user_id: `eq.${userId}`
      });
    }
  } catch (error) {
    if (!isMissingRelationError(error, 'project_copilot_messages')) throw error;
    const key = copilotLocalStorageKey({ contextType, projectId, projectTaskId });
    const rows = readLocalRows<ProjectCopilotMessage>(key).filter(
      (row) => String(row.metadata?.session_id || 'default') !== sessionId || String(row.metadata?.owner_user_id || '') !== userId
    );
    writeLocalRows(key, rows);
  }
}

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

export async function listApiTokens(userId: string): Promise<ApiToken[]> {
  const normalizedUserId = String(userId || '').trim();
  if (!normalizedUserId) return [];
  return request<ApiToken[]>('/api_tokens', undefined, {
    select: '*',
    user_id: `eq.${normalizedUserId}`,
    order: 'created_at.desc'
  });
}

export async function findApiTokenByHash(tokenHash: string): Promise<ApiToken | null> {
  const normalizedHash = String(tokenHash || '').trim();
  if (!normalizedHash) return null;
  const rows = await request<ApiToken[]>('/api_tokens', undefined, {
    select: '*',
    token_hash: `eq.${normalizedHash}`,
    limit: '1'
  });
  return rows[0] || null;
}

export async function insertApiToken(input: Partial<ApiToken>): Promise<ApiToken> {
  const rows = await request<ApiToken[]>(
    '/api_tokens',
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
  return rows[0];
}

export async function updateApiToken(tokenId: string, patch: Partial<ApiToken>): Promise<ApiToken> {
  const rows = await request<ApiToken[]>(
    '/api_tokens',
    {
      method: 'PATCH',
      headers: {
        Prefer: 'return=representation'
      },
      body: JSON.stringify(patch)
    },
    {
      id: `eq.${tokenId}`,
      select: '*'
    }
  );
  return rows[0];
}

export async function revokeApiToken(tokenId: string): Promise<ApiToken> {
  return updateApiToken(tokenId, {
    is_active: false,
    revoked_at: new Date().toISOString()
  });
}

export async function deleteApiToken(tokenId: string): Promise<void> {
  await request<ApiToken[]>(
    '/api_tokens',
    {
      method: 'DELETE',
      headers: {
        Prefer: 'return=minimal'
      }
    },
    {
      id: `eq.${tokenId}`
    }
  );
}

export async function listApiTokenUsage(tokenId: string, sinceIso?: string): Promise<ApiTokenUsage[]> {
  const normalizedTokenId = String(tokenId || '').trim();
  if (!normalizedTokenId) return [];
  const query: Record<string, string | undefined> = {
    select: '*',
    token_id: `eq.${normalizedTokenId}`,
    order: 'created_at.desc'
  };
  if (sinceIso?.trim()) {
    query.created_at = `gte.${sinceIso.trim()}`;
  }
  return request<ApiTokenUsage[]>('/api_token_usage', undefined, query);
}

function parseTotalFromContentRange(value: string | null): number {
  if (!value) return 0;
  const slashIdx = value.lastIndexOf('/');
  if (slashIdx < 0) return 0;
  const raw = value.slice(slashIdx + 1).trim();
  const total = Number(raw);
  return Number.isFinite(total) && total >= 0 ? Math.floor(total) : 0;
}

export async function listApiTokenUsagePage(
  tokenId: string,
  options: {
    sinceIso?: string;
    limit: number;
    offset: number;
  }
): Promise<{ rows: ApiTokenUsage[]; total: number }> {
  const normalizedTokenId = String(tokenId || '').trim();
  if (!normalizedTokenId) {
    return { rows: [], total: 0 };
  }
  const limit = Math.max(1, Math.floor(options.limit || 1));
  const offset = Math.max(0, Math.floor(options.offset || 0));
  const query: Record<string, string | undefined> = {
    select: '*',
    token_id: `eq.${normalizedTokenId}`,
    order: 'created_at.desc',
    limit: String(limit),
    offset: String(offset)
  };
  if (options.sinceIso?.trim()) {
    query.created_at = `gte.${options.sinceIso.trim()}`;
  }

  const candidates = buildSupabaseUrlCandidates('/api_token_usage', query);
  let lastError: Error | null = null;
  for (let i = 0; i < candidates.length; i += 1) {
    const url = candidates[i];
    let res: Response;
    try {
      res = await fetchWithTimeout(url, {
        method: 'GET',
        headers: {
          'Content-Type': 'application/json',
          Prefer: 'count=exact'
        }
      });
    } catch (error) {
      lastError = error instanceof Error ? error : new Error('Unknown network error');
      continue;
    }

    if ((res.status === 404 || res.status === 405) && i < candidates.length - 1) {
      lastError = new Error(`Supabase candidate returned ${res.status}: ${url}`);
      continue;
    }

    if (!res.ok) {
      const text = await res.text();
      throw new Error(`PostgREST ${res.status}: ${text}`);
    }

    const rows = (await res.json()) as ApiTokenUsage[];
    const total = parseTotalFromContentRange(res.headers.get('content-range'));
    return { rows, total };
  }

  const detail = lastError ? ` Last error: ${lastError.message}` : '';
  throw new Error(`Supabase-lite request failed. Tried: ${candidates.join(', ')}.${detail}`);
}

export async function listApiTokenUsageDaily(tokenId: string, sinceIso?: string): Promise<ApiTokenUsageDaily[]> {
  const normalizedTokenId = String(tokenId || '').trim();
  if (!normalizedTokenId) return [];
  const query: Record<string, string | undefined> = {
    select: '*',
    token_id: `eq.${normalizedTokenId}`,
    order: 'usage_day.asc'
  };
  if (sinceIso?.trim()) {
    query.usage_day = `gte.${sinceIso.trim().slice(0, 10)}`;
  }
  return request<ApiTokenUsageDaily[]>('/api_token_usage_daily', undefined, query);
}

export async function listApiTokenUsageDailyByTokenIds(tokenIds: string[], sinceIso?: string): Promise<ApiTokenUsageDaily[]> {
  const ids = Array.from(new Set(tokenIds.map((id) => String(id || '').trim()).filter(Boolean)));
  if (ids.length === 0) return [];
  const chunkSize = 150;
  const rows = await Promise.all(
    Array.from({ length: Math.ceil(ids.length / chunkSize) }, (_, i) => ids.slice(i * chunkSize, (i + 1) * chunkSize)).map(
      (chunk) => {
        const query: Record<string, string | undefined> = {
          select: '*',
          token_id: `in.(${chunk.join(',')})`,
          order: 'usage_day.asc'
        };
        if (sinceIso?.trim()) {
          query.usage_day = `gte.${sinceIso.trim().slice(0, 10)}`;
        }
        return request<ApiTokenUsageDaily[]>('/api_token_usage_daily', undefined, query);
      }
    )
  );
  return rows.flat();
}
