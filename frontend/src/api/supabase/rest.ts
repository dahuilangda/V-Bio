import type { AppUser } from '../../types/models';
import { ENV } from '../../utils/env';


// REST 客户端核心：连接、超时、查询构造、通用行查询。


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

    // Some PostgREST writes return 200/201 with an empty body; guard the JSON parse.
    const text = await res.text();
    if (!text || !text.trim()) {
      return [] as T;
    }
    try {
      return JSON.parse(text) as T;
    } catch {
      return [] as T;
    }
  }

  const detail = lastError ? ` Last error: ${lastError.message}` : '';
  throw new Error(`Supabase-lite request failed. Tried: ${candidates.join(', ')}.${detail}`);
}

// A representation write must return the row; empty means nothing matched or RLS blocked the read.
function _requireSingleRow<T>(rows: T[]): T {
  if (!Array.isArray(rows) || rows.length === 0) {
    throw new Error('Write returned no rows — the target may not exist or RLS blocked the read.');
  }
  return rows[0];
}

async function listUsersByIds(userIds: string[]): Promise<AppUser[]> {
  const idFilter = buildInFilter(userIds);
  if (!idFilter) return [];
  // No deleted_at filter: the RLS policy enforces deleted_at IS NULL server-side, and
  // anonymous callers have no SELECT grant on that column.
  return request<AppUser[]>('/app_users', undefined, {
    select: 'id,username,name,avatar_url,created_at,last_login_at',
    id: idFilter
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

export { fetchWithTimeout, buildInFilter, buildQueryString, buildSupabaseUrlCandidates, request, asObjectRecord, compactObjectRecord, parseBooleanToken, _requireSingleRow, listUsersByIds, listProjectTaskMetaByIds };
