import type { AppUser, Project, ProjectTask, TaskState } from '../types/models';
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
}

export async function listProjects(options: ListProjectsOptions = {}): Promise<Project[]> {
  const query: Record<string, string | undefined> = {
    select: '*',
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

  return request<Project[]>('/projects', undefined, query);
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

interface ProjectTaskStateRow {
  project_id: string;
  task_id: string;
  task_state: TaskState;
}

export async function listProjectTaskStatesByProjects(projectIds: string[]): Promise<ProjectTaskStateRow[]> {
  const normalizedIds = Array.from(new Set(projectIds.map((id) => id.trim()).filter(Boolean)));
  if (normalizedIds.length === 0) return [];
  return request<ProjectTaskStateRow[]>('/project_tasks', undefined, {
    select: 'project_id,task_id,task_state',
    project_id: `in.(${normalizedIds.join(',')})`
  });
}

export async function insertProjectTask(input: Partial<ProjectTask>): Promise<ProjectTask> {
  const rows = await request<ProjectTask[]>(
    '/project_tasks',
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

export async function updateProjectTask(taskRowId: string, patch: Partial<ProjectTask>): Promise<ProjectTask> {
  const rows = await request<ProjectTask[]>(
    '/project_tasks',
    {
      method: 'PATCH',
      headers: {
        Prefer: 'return=representation'
      },
      body: JSON.stringify(patch)
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
