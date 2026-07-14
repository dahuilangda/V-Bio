import type { AppUser, AuthLoginInput, AuthRegisterInput, Session } from '../types/models';
import { hashPassword } from '../utils/crypto';
import { ENV } from '../utils/env';
import { requestManagement } from './backendClient';
import {
  findUserByEmail,
  findUserByUsername,
  insertUser
} from './supabaseLite';

const SESSION_KEY = 'vbio_session';

export function parseEnvList(value: string): Set<string> {
  return new Set(
    value
      .split(',')
      .map((item) => item.trim().toLowerCase())
      .filter(Boolean)
  );
}

export function isSuperAdminIdentity(username?: string | null, email?: string | null): boolean {
  const adminUsernames = parseEnvList(ENV.superAdminUsernames);
  const adminEmails = parseEnvList(ENV.superAdminEmails);
  const normalizedUsername = String(username || '').trim().toLowerCase();
  const normalizedEmail = String(email || '').trim().toLowerCase();
  return Boolean(
    (normalizedUsername && adminUsernames.has(normalizedUsername)) ||
      (normalizedEmail && adminEmails.has(normalizedEmail))
  );
}

function toSession(user: AppUser): Session {
  const isSuperAdmin = isSuperAdminIdentity(user.username, user.email);
  return {
    userId: user.id,
    username: user.username,
    name: user.name,
    email: user.email || null,
    avatarUrl: user.avatar_url || null,
    isAdmin: user.is_admin || isSuperAdmin,
    isSuperAdmin,
    loginAt: new Date().toISOString(),
    authProvider: 'local'
  };
}

export function saveSession(session: Session): void {
  localStorage.setItem(SESSION_KEY, JSON.stringify(session));
}

function normalizeSession(session: Session): Session {
  const isSuperAdmin = isSuperAdminIdentity(session.username, session.email);
  const normalized: Session = {
    ...session,
    isAdmin: Boolean(session.isAdmin || isSuperAdmin),
    isSuperAdmin
  };
  return normalized;
}

export function loadSession(): Session | null {
  const text = localStorage.getItem(SESSION_KEY);
  if (!text) return null;
  try {
    const session = normalizeSession(JSON.parse(text) as Session);
    saveSession(session);
    return session;
  } catch {
    return null;
  }
}

export function clearSession(): void {
  localStorage.removeItem(SESSION_KEY);
}


export function managementSessionNeedsRefresh(token: string, minValiditySeconds = 60): boolean {
  try {
    const body = String(token || '').trim().split('.', 1)[0];
    if (!body) return true;
    const normalized = body.replace(/-/g, '+').replace(/_/g, '/');
    const padded = normalized + '='.repeat((4 - (normalized.length % 4)) % 4);
    const payload = JSON.parse(window.atob(padded)) as { exp?: number };
    const expiresAt = Number(payload.exp || 0);
    return !Number.isFinite(expiresAt) || expiresAt <= Math.floor(Date.now() / 1000) + minValiditySeconds;
  } catch {
    return true;
  }
}

export async function renewManagementSession(managementToken: string): Promise<string> {
  const token = String(managementToken || '').trim();
  if (!token) throw new Error('Management session is unavailable.');
  const res = await requestManagement('/vbio-api/auth/management-session/refresh', {
    method: 'POST',
    headers: {
      Accept: 'application/json',
      'X-VBio-Session': token
    }
  });
  const payload = (await res.json().catch(() => ({}))) as { managementToken?: string; error?: string };
  const refreshed = String(payload.managementToken || '').trim();
  if (!res.ok || !refreshed) {
    throw new Error(payload.error || `Management session refresh failed with HTTP ${res.status}.`);
  }
  return refreshed;
}

export function getAuthHeaders(): Record<string, string> {
  return ENV.apiToken ? { 'X-API-Token': ENV.apiToken } : {};
}

function validateRegistration(input: AuthRegisterInput): void {
  if (input.username.trim().length < 3) {
    throw new Error('Username must be at least 3 characters.');
  }
  if (!/^[a-zA-Z0-9_.-]+$/.test(input.username)) {
    throw new Error('Username can contain only letters, numbers, underscore, dot, and hyphen.');
  }
  if (input.password.length < 6) {
    throw new Error('Password must be at least 6 characters.');
  }
  if (!input.name.trim()) {
    throw new Error('Display name is required.');
  }
}

export async function register(input: AuthRegisterInput): Promise<Session> {
  validateRegistration(input);

  const username = input.username.trim().toLowerCase();
  const email = input.email?.trim().toLowerCase() || null;

  const userByName = await findUserByUsername(username);
  if (userByName) {
    throw new Error('Username already exists.');
  }

  if (email) {
    const userByEmail = await findUserByEmail(email);
    if (userByEmail) {
      throw new Error('Email already exists.');
    }
  }

  const password_hash = await hashPassword(username, input.password);
  const isSuperAdmin = isSuperAdminIdentity(username, email);
  const created = await insertUser({
    username,
    name: input.name.trim(),
    email,
    password_hash,
    is_admin: isSuperAdmin,
    last_login_at: new Date().toISOString()
  });

  const session = toSession(created);
  saveSession(session);
  return session;
}

export async function login(input: AuthLoginInput): Promise<Session> {
  const identifier = input.identifier.trim();
  if (!identifier) throw new Error('Username or email is required.');

  const res = await requestManagement('/vbio-api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ identifier, password: input.password })
  });
  const payload = (await res.json().catch(() => ({}))) as { session?: Session; error?: string };
  if (!res.ok || !payload.session) {
    throw new Error(payload.error || `Sign-in failed with HTTP ${res.status}.`);
  }
  const session = normalizeSession(payload.session);
  saveSession(session);
  return session;
}

export async function completeJwtLogin(token: string): Promise<Session> {
  const res = await requestManagement('/vbio-api/auth/jwt', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ token })
  });
  const payload = (await res.json().catch(() => ({}))) as { session?: Session; error?: string };
  if (!res.ok || !payload.session) {
    throw new Error(payload.error || `JWT sign-in failed with HTTP ${res.status}.`);
  }
  const session = {
    ...payload.session,
    isSuperAdmin: isSuperAdminIdentity(payload.session.username, payload.session.email),
    isAdmin: Boolean(payload.session.isAdmin || isSuperAdminIdentity(payload.session.username, payload.session.email)),
    authProvider: 'jwt' as const
  };
  saveSession(session);
  return session;
}
