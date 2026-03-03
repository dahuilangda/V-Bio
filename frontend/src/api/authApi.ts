import type { AppUser, AuthLoginInput, AuthRegisterInput, Session } from '../types/models';
import { hashPassword } from '../utils/crypto';
import {
  findUserByEmail,
  findUserByIdentifier,
  findUserByUsername,
  insertUser,
  updateUser
} from './supabaseLite';

const SESSION_KEY = 'vbio_session';

export function saveSession(session: Session): void {
  localStorage.setItem(SESSION_KEY, JSON.stringify(session));
}

export function loadSession(): Session | null {
  const text = localStorage.getItem(SESSION_KEY);
  if (!text) return null;
  try {
    return JSON.parse(text) as Session;
  } catch {
    return null;
  }
}

export function clearSession(): void {
  localStorage.removeItem(SESSION_KEY);
}

function toSession(user: AppUser): Session {
  return {
    userId: user.id,
    username: user.username,
    name: user.name,
    email: user.email || null,
    avatarUrl: user.avatar_url || null,
    isAdmin: user.is_admin,
    loginAt: new Date().toISOString()
  };
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
  const created = await insertUser({
    username,
    name: input.name.trim(),
    email,
    password_hash,
    is_admin: false,
    last_login_at: new Date().toISOString()
  });

  const session = toSession(created);
  saveSession(session);
  return session;
}

export async function login(input: AuthLoginInput): Promise<Session> {
  const identifier = input.identifier.trim();
  if (!identifier) throw new Error('Username or email is required.');

  const found = await findUserByIdentifier(identifier);
  if (!found) {
    throw new Error('User not found.');
  }

  const expected = await hashPassword(found.username, input.password);
  if (expected !== found.password_hash) {
    throw new Error('Invalid password.');
  }

  const updated = await updateUser(found.id, { last_login_at: new Date().toISOString() });
  const session = toSession(updated);
  saveSession(session);
  return session;
}
