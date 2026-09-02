/**
 * Server-backed auth operations (F2): profile, user search, admin user management, and API
 * tokens — all through the management API with the caller's session. These replaced direct
 * anonymous writes to app_users/api_tokens, which the database no longer permits.
 */
import { requestManagement } from './backendClient';
import type { AppUser } from '../types/models';

function readSessionToken(): string | null {
  try {
    const raw = localStorage.getItem('vbio_session');
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { managementToken?: string };
    return parsed.managementToken || null;
  } catch {
    return null;
  }
}

async function authedFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const token = readSessionToken();
  if (!token) throw new Error('Sign-in required.');
  return requestManagement(path, {
    ...init,
    headers: {
      ...(init.headers as Record<string, string> | undefined),
      'X-VBio-Session': token,
      ...(init.body ? { 'Content-Type': 'application/json' } : {})
    }
  });
}

async function readError(res: Response, fallback: string): Promise<string> {
  const payload = (await res.json().catch(() => ({}))) as { error?: string };
  return payload.error || `${fallback} (HTTP ${res.status})`;
}

export interface ServerUser {
  id: string;
  username: string;
  name: string | null;
  email: string | null;
  avatar_url: string | null;
  is_admin?: boolean;
  deleted_at?: string | null;
  created_at?: string | null;
  last_login_at?: string | null;
}

export async function fetchMe(): Promise<ServerUser> {
  const res = await authedFetch('/vbio-api/auth/me');
  if (!res.ok) throw new Error(await readError(res, 'Failed to load profile'));
  const payload = (await res.json()) as { user: ServerUser };
  return payload.user;
}

export async function updateProfile(patch: {
  name?: string;
  avatar_url?: string;
  password?: string;
  current_password?: string;
}): Promise<ServerUser> {
  const res = await authedFetch('/vbio-api/auth/profile', {
    method: 'PATCH',
    body: JSON.stringify(patch)
  });
  if (!res.ok) throw new Error(await readError(res, 'Failed to update profile'));
  const payload = (await res.json()) as { user: ServerUser };
  return payload.user;
}

export async function searchUsers(query: string): Promise<ServerUser[]> {
  const res = await authedFetch(`/vbio-api/auth/users/search?q=${encodeURIComponent(query)}`);
  if (!res.ok) throw new Error(await readError(res, 'User search failed'));
  const payload = (await res.json()) as { users: ServerUser[] };
  return payload.users || [];
}

export async function adminListUsers(): Promise<ServerUser[]> {
  const res = await authedFetch('/vbio-api/admin/users');
  if (!res.ok) throw new Error(await readError(res, 'Failed to load users'));
  const payload = (await res.json()) as { users: ServerUser[] };
  return payload.users || [];
}

export async function adminCreateUser(input: {
  username: string;
  password: string;
  name?: string;
  email?: string;
  is_admin?: boolean;
}): Promise<ServerUser> {
  const res = await authedFetch('/vbio-api/admin/users', {
    method: 'POST',
    body: JSON.stringify(input)
  });
  if (!res.ok) throw new Error(await readError(res, 'Failed to create user'));
  const payload = (await res.json()) as { user: ServerUser };
  return payload.user;
}

export async function adminUpdateUser(userId: string, patch: Partial<ServerUser> & { password?: string }): Promise<ServerUser> {
  const res = await authedFetch(`/vbio-api/admin/users/${encodeURIComponent(userId)}`, {
    method: 'PATCH',
    body: JSON.stringify(patch)
  });
  if (!res.ok) throw new Error(await readError(res, 'Failed to update user'));
  const payload = (await res.json()) as { user: ServerUser };
  return payload.user;
}

export interface ApiTokenRow {
  id: string;
  user_id: string | null;
  name: string;
  project_id: string;
  allow_submit: boolean;
  allow_delete: boolean;
  allow_cancel: boolean;
  is_active: boolean;
  revoked_at: string | null;
  expires_at: string | null;
  created_at?: string | null;
  last_used_at?: string | null;
}

export async function listApiTokens(): Promise<ApiTokenRow[]> {
  const res = await authedFetch('/vbio-api/tokens');
  if (!res.ok) throw new Error(await readError(res, 'Failed to load API tokens'));
  const payload = (await res.json()) as { tokens: ApiTokenRow[] };
  return payload.tokens || [];
}

export async function createApiToken(input: {
  name: string;
  project_id: string;
  allow_submit?: boolean;
  allow_delete?: boolean;
  allow_cancel?: boolean;
}): Promise<{ token: ApiTokenRow; token_plain: string }> {
  const res = await authedFetch('/vbio-api/tokens', { method: 'POST', body: JSON.stringify(input) });
  if (!res.ok) throw new Error(await readError(res, 'Failed to create token'));
  return (await res.json()) as { token: ApiTokenRow; token_plain: string };
}

export async function updateApiToken(tokenId: string, patch: Partial<ApiTokenRow>): Promise<ApiTokenRow> {
  const res = await authedFetch(`/vbio-api/tokens/${encodeURIComponent(tokenId)}`, {
    method: 'PATCH',
    body: JSON.stringify(patch)
  });
  if (!res.ok) throw new Error(await readError(res, 'Failed to update token'));
  const payload = (await res.json()) as { token: ApiTokenRow };
  return payload.token;
}

export async function deleteApiToken(tokenId: string): Promise<void> {
  const res = await authedFetch(`/vbio-api/tokens/${encodeURIComponent(tokenId)}`, { method: 'DELETE' });
  if (!res.ok) throw new Error(await readError(res, 'Failed to delete token'));
}

export function toApiToken(row: ApiTokenRow): import('../types/models').ApiToken {
  return {
    id: row.id,
    user_id: row.user_id || '',
    name: row.name,
    token_hash: '',
    token_plain: '',
    token_prefix: '',
    token_last4: '',
    project_id: row.project_id || '',
    allow_submit: row.allow_submit,
    allow_delete: row.allow_delete,
    allow_cancel: row.allow_cancel,
    scopes: ['runtime', 'project', 'task'],
    is_active: row.is_active,
    last_used_at: row.last_used_at || null,
    expires_at: row.expires_at,
    revoked_at: row.revoked_at,
    created_at: row.created_at || '',
    updated_at: ''
  };
}

export function toAppUser(user: ServerUser): AppUser {
  return user as unknown as AppUser;
}
