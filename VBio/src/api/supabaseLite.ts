import type { ApiToken, ApiTokenUsage, ApiTokenUsageDaily, AppUser, Project, ProjectTask, TaskState } from '../types/models';
import { ENV } from '../utils/env';

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
        `Supabase-lite request timeout after ${timeoutMs}ms for ${url}. Check that PostgREST is reachable (VBio/supabase-lite, default port 54321).`
      );
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function unique<T>(items: T[]): T[] {
  return Array.from(new Set(items));
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

function buildSupabaseUrlCandidates(path: string, query?: Record<string, string | undefined>): string[] {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  const queryString = buildQueryString(query);
  const suffix = `${normalizedPath}${queryString ? `?${queryString}` : ''}`;
  const candidates = [`${configuredBaseUrl}${suffix}`];

  if (typeof window !== 'undefined') {
    // Vite proxy path
    candidates.push(`${window.location.origin}/supabase${suffix}`);
    // Remote-access fallback: hit PostgREST directly on the current host.
    candidates.push(`${window.location.protocol}//${window.location.hostname}:54321${suffix}`);
  }

  return unique(candidates.filter(Boolean));
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
  return rows.map((row) => ({
    protein_sequence: '',
    ligand_smiles: '',
    confidence: {},
    affinity: {},
    ...row
  })) as Project[];
}

export async function getProjectById(projectId: string): Promise<Project | null> {
  const rows = await request<Project[]>('/projects', undefined, {
    select: '*',
    id: `eq.${projectId}`,
    limit: '1'
  });
  return rows[0] || null;
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

export async function listProjectTasks(projectId: string): Promise<ProjectTask[]> {
  return request<ProjectTask[]>('/project_tasks', undefined, {
    select: '*',
    project_id: `eq.${projectId}`,
    order: 'created_at.desc'
  });
}

export async function listProjectTasksCompact(projectId: string): Promise<ProjectTask[]> {
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
  const rows = await request<Array<Partial<ProjectTask>>>('/project_tasks_list', undefined, {
    select: selectFields,
    project_id: `eq.${projectId}`,
    order: 'created_at.desc'
  });

  return rows.map((row) => ({
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
  })) as ProjectTask[];
}

export async function listProjectTasksForList(projectId: string): Promise<ProjectTask[]> {
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
    'protein_sequence',
    'ligand_smiles',
    'properties',
    'confidence',
    'structure_name',
    'submitted_at',
    'completed_at',
    'duration_seconds',
    'created_at',
    'updated_at'
  ].join(',');
  const [rows, detailRows] = await Promise.all([
    request<Array<Partial<ProjectTask>>>('/project_tasks_list', undefined, {
      select: selectFields,
      project_id: `eq.${projectId}`,
      order: 'created_at.desc'
    }),
    request<Array<Partial<ProjectTask>>>('/project_tasks', undefined, {
      select: 'id,components,affinity',
      project_id: `eq.${projectId}`,
      order: 'created_at.desc'
    })
  ]);
  const detailById = new Map(
    detailRows
      .map((row) => [String(row.id || '').trim(), row] as const)
      .filter(([id]) => Boolean(id))
  );

  const mergedRows = rows.map((row) => {
    const detail = detailById.get(String(row.id || '').trim()) || {};
    const affinity =
      (detail as any).affinity && typeof (detail as any).affinity === 'object' && !Array.isArray((detail as any).affinity)
        ? (detail as any).affinity
        : {};
    return {
      ...row,
      components: Array.isArray((detail as any).components) ? (detail as any).components : [],
      affinity
    };
  });

  return mergedRows.map((row) => ({
    name: '',
    summary: '',
    protein_sequence: '',
    constraints: [],
    properties: {
      affinity: false,
      target: null,
      ligand: null,
      binder: null
    },
    ...row
  })) as ProjectTask[];
}

export async function getProjectTaskById(taskRowId: string): Promise<ProjectTask | null> {
  const normalizedTaskRowId = String(taskRowId || '').trim();
  if (!normalizedTaskRowId) return null;
  const rows = await request<ProjectTask[]>('/project_tasks', undefined, {
    select: '*',
    id: `eq.${normalizedTaskRowId}`,
    limit: '1'
  });
  return rows[0] || null;
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

interface ProjectTaskStateRow {
  project_id: string;
  task_id: string;
  task_state: TaskState;
}

export async function listProjectTaskStatesByProjects(projectIds: string[]): Promise<ProjectTaskStateRow[]> {
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
      request<ProjectTaskStateRow[]>('/project_tasks', undefined, {
        select: 'project_id,task_id,task_state',
        project_id: `in.(${chunk.join(',')})`
      })
    )
  );
  return results.flat();
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

export async function updateProjectTask(taskRowId: string, patch: Partial<ProjectTask>): Promise<ProjectTask> {
  const sanitized = sanitizeProjectTaskWritePayload(patch);
  const rows = await request<ProjectTask[]>(
    '/project_tasks',
    {
      method: 'PATCH',
      headers: {
        Prefer: 'return=representation'
      },
      body: JSON.stringify(sanitized)
    },
    {
      id: `eq.${taskRowId}`,
      select: '*'
    }
  );
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
