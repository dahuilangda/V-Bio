#!/usr/bin/env node
import { createHash } from 'node:crypto';

function parseOptions(args) {
  const options = {};
  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if (!arg.startsWith('--')) continue;
    const stripped = arg.slice(2);
    const eqIndex = stripped.indexOf('=');
    if (eqIndex >= 0) {
      const key = stripped.slice(0, eqIndex);
      const value = stripped.slice(eqIndex + 1);
      options[key] = value;
      continue;
    }

    const next = args[i + 1];
    if (next && !next.startsWith('--')) {
      options[stripped] = next;
      i += 1;
    } else {
      options[stripped] = true;
    }
  }
  return options;
}

function usage() {
  console.log(`
Usage:
  npm run users -- list [--include-deleted] [--rest-url http://127.0.0.1:54321]
  npm run users -- delete --username <name> | --id <uuid> [--hard]
  npm run users -- set-password --username <name> | --id <uuid> --password <new_password>
  npm run users -- set-admin --username <name> | --id <uuid> --value true|false

Notes:
  - All commands can target admin users.
  - Default is soft-delete (set deleted_at). Use --hard for physical delete.
`);
}

function normalizeRestUrl(value) {
  return (value || process.env.SUPABASE_REST_URL || process.env.VITE_SUPABASE_REST_URL || 'http://127.0.0.1:54321').replace(
    /\/$/,
    ''
  );
}

function toBool(value, fallback = false) {
  if (value === undefined || value === null || value === '') return fallback;
  const normalized = String(value).trim().toLowerCase();
  if (['1', 'true', 'yes', 'y', 'on'].includes(normalized)) return true;
  if (['0', 'false', 'no', 'n', 'off'].includes(normalized)) return false;
  return fallback;
}

async function requestJson(url, init = {}) {
  const res = await fetch(url, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init.headers || {})
    }
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`HTTP ${res.status}: ${text}`);
  }

  if (res.status === 204) return null;
  return await res.json();
}

function withQuery(baseUrl, path, query = {}) {
  const params = new URLSearchParams();
  Object.entries(query).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') {
      params.set(key, String(value));
    }
  });
  const queryStr = params.toString();
  return `${baseUrl}${path}${queryStr ? `?${queryStr}` : ''}`;
}

async function findUser(baseUrl, options) {
  const userId = options.id ? String(options.id).trim() : '';
  const username = options.username ? String(options.username).trim().toLowerCase() : '';

  if (!userId && !username) {
    throw new Error('Provide --id or --username.');
  }

  const query = {
    select: '*',
    limit: '1'
  };

  if (userId) query.id = `eq.${userId}`;
  if (username) query.username = `eq.${username}`;

  const url = withQuery(baseUrl, '/app_users', query);
  const rows = await requestJson(url, { method: 'GET' });
  if (!Array.isArray(rows) || rows.length === 0) {
    throw new Error('User not found.');
  }
  return rows[0];
}

function buildPasswordHash(username, password) {
  return createHash('sha256')
    .update(`${String(username).trim().toLowerCase()}::${String(password)}`)
    .digest('hex');
}

async function cmdList(baseUrl, options) {
  const query = {
    select: 'id,username,name,email,is_admin,last_login_at,created_at,deleted_at',
    order: 'created_at.asc'
  };
  if (!toBool(options['include-deleted'], false)) {
    query.deleted_at = 'is.null';
  }

  const rows = await requestJson(withQuery(baseUrl, '/app_users', query), { method: 'GET' });
  if (!Array.isArray(rows) || rows.length === 0) {
    console.log('No users found.');
    return;
  }

  console.table(
    rows.map((row) => ({
      id: row.id,
      username: row.username,
      name: row.name,
      email: row.email || '-',
      role: row.is_admin ? 'admin' : 'member',
      deleted: row.deleted_at ? 'yes' : 'no',
      last_login: row.last_login_at || '-',
      created_at: row.created_at
    }))
  );
}

async function cmdDelete(baseUrl, options) {
  const user = await findUser(baseUrl, options);
  if (toBool(options.hard, false)) {
    const url = withQuery(baseUrl, '/app_users', {
      id: `eq.${user.id}`,
      select: 'id,username'
    });
    const rows = await requestJson(url, {
      method: 'DELETE',
      headers: {
        Prefer: 'return=representation'
      }
    });
    const deleted = Array.isArray(rows) ? rows[0] : null;
    console.log(`Deleted user: ${deleted?.username || user.username} (${deleted?.id || user.id})`);
    return;
  }

  const url = withQuery(baseUrl, '/app_users', {
    id: `eq.${user.id}`,
    select: 'id,username,deleted_at'
  });
  const rows = await requestJson(url, {
    method: 'PATCH',
    headers: {
      Prefer: 'return=representation'
    },
    body: JSON.stringify({
      deleted_at: new Date().toISOString()
    })
  });
  const deleted = Array.isArray(rows) ? rows[0] : null;
  console.log(`Soft-deleted user: ${deleted?.username || user.username} (${deleted?.id || user.id})`);
}

async function cmdSetPassword(baseUrl, options) {
  const password = options.password ? String(options.password) : '';
  if (!password) throw new Error('Provide --password.');

  const user = await findUser(baseUrl, options);
  const passwordHash = buildPasswordHash(user.username, password);
  const url = withQuery(baseUrl, '/app_users', {
    id: `eq.${user.id}`,
    select: 'id,username'
  });

  const rows = await requestJson(url, {
    method: 'PATCH',
    headers: {
      Prefer: 'return=representation'
    },
    body: JSON.stringify({
      password_hash: passwordHash,
      deleted_at: null
    })
  });

  const updated = Array.isArray(rows) ? rows[0] : null;
  console.log(`Password updated: ${updated?.username || user.username} (${updated?.id || user.id})`);
}

async function cmdSetAdmin(baseUrl, options) {
  if (options.value === undefined) {
    throw new Error('Provide --value true|false.');
  }
  const value = toBool(options.value, false);
  const user = await findUser(baseUrl, options);
  const url = withQuery(baseUrl, '/app_users', {
    id: `eq.${user.id}`,
    select: 'id,username,is_admin'
  });

  const rows = await requestJson(url, {
    method: 'PATCH',
    headers: {
      Prefer: 'return=representation'
    },
    body: JSON.stringify({
      is_admin: value,
      deleted_at: null
    })
  });

  const updated = Array.isArray(rows) ? rows[0] : null;
  console.log(`Updated role: ${updated?.username || user.username} -> ${value ? 'admin' : 'member'}`);
}

async function main() {
  const [command, ...rest] = process.argv.slice(2);
  if (!command || ['-h', '--help', 'help'].includes(command)) {
    usage();
    process.exit(0);
  }

  const options = parseOptions(rest);
  const baseUrl = normalizeRestUrl(options['rest-url']);

  if (command === 'list') {
    await cmdList(baseUrl, options);
    return;
  }
  if (command === 'delete') {
    await cmdDelete(baseUrl, options);
    return;
  }
  if (command === 'set-password') {
    await cmdSetPassword(baseUrl, options);
    return;
  }
  if (command === 'set-admin') {
    await cmdSetAdmin(baseUrl, options);
    return;
  }

  throw new Error(`Unknown command: ${command}`);
}

main().catch((err) => {
  console.error(`users CLI error: ${err.message}`);
  process.exit(1);
});
