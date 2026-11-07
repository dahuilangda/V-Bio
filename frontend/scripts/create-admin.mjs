#!/usr/bin/env node
import { randomBytes, scryptSync } from 'node:crypto';

function getArg(key, fallback = undefined) {
  const idx = process.argv.findIndex((arg) => arg === `--${key}`);
  if (idx >= 0 && process.argv[idx + 1]) {
    return process.argv[idx + 1];
  }
  return fallback;
}

function usage() {
  console.log(`
Usage:
  npm run create-admin -- --username admin --password <PASSWORD> [--name "Admin"] [--email admin@example.com]

Optional:
  --rest-url http://127.0.0.1:54321
`);
}

const usernameRaw = getArg('username');
const password = getArg('password');

if (!usernameRaw || !password) {
  usage();
  process.exit(1);
}

const username = usernameRaw.trim().toLowerCase();
const name = (getArg('name') || username).trim();
const email = (getArg('email') || '').trim().toLowerCase();
const restUrl =
  (getArg('rest-url') ||
    process.env.SUPABASE_REST_URL ||
    process.env.VITE_SUPABASE_REST_URL ||
    'http://127.0.0.1:54321').replace(/\/$/, '');

// 与 vbio_management_api.py 的 scrypt 密码格式一致
const SCRYPT_N = 16384;
const SCRYPT_R = 8;
const SCRYPT_P = 1;

function hashPasswordScrypt(pwd) {
  const salt = randomBytes(16);
  const dk = scryptSync(pwd, salt, 32, { N: SCRYPT_N, r: SCRYPT_R, p: SCRYPT_P });
  return `scrypt$${SCRYPT_N}$${SCRYPT_R}$${SCRYPT_P}$${salt.toString('hex')}$${dk.toString('hex')}`;
}

const passwordHash = hashPasswordScrypt(password);

async function fetchJson(url, init = {}) {
  const res = await fetch(url, init);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`HTTP ${res.status}: ${text}`);
  }
  if (res.status === 204) return null;
  return await res.json();
}

const baseHeaders = {
  'Content-Type': 'application/json'
};

async function main() {
  const query = new URLSearchParams({
    select: '*',
    username: `eq.${username}`,
    limit: '1'
  });
  const existing = await fetchJson(`${restUrl}/app_users?${query.toString()}`, {
    method: 'GET'
  });

  if (Array.isArray(existing) && existing.length > 0) {
    const user = existing[0];
    const patchQuery = new URLSearchParams({
      id: `eq.${user.id}`,
      select: '*'
    });

    const payload = {
      username,
      name,
      email: email || null,
      password_hash: passwordHash,
      is_admin: true,
      deleted_at: null
    };

    const updated = await fetchJson(`${restUrl}/app_users?${patchQuery.toString()}`, {
      method: 'PATCH',
      headers: {
        ...baseHeaders,
        Prefer: 'return=representation'
      },
      body: JSON.stringify(payload)
    });

    console.log(`Updated admin user: ${updated?.[0]?.username} (${updated?.[0]?.id})`);
    return;
  }

  const payload = {
    username,
    name,
    email: email || null,
    password_hash: passwordHash,
    is_admin: true,
    last_login_at: null
  };

  const created = await fetchJson(`${restUrl}/app_users?select=*`, {
    method: 'POST',
    headers: {
      ...baseHeaders,
      Prefer: 'return=representation'
    },
    body: JSON.stringify(payload)
  });

  console.log(`Created admin user: ${created?.[0]?.username} (${created?.[0]?.id})`);
}

main().catch((err) => {
  console.error(`Failed to create admin: ${err.message}`);
  process.exit(1);
});
