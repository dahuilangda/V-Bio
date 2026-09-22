// Copilot transcript cache in IndexedDB; the server stays the source of truth.
// No fallback backend: a failed IndexedDB operation rejects instead of degrading.
import type { ProjectCopilotMessage } from '../types/models';

/** Hard cap per scope — mirrors the server list limit (200 rows). */
const CACHE_ROW_LIMIT = 200;
const DB_NAME = 'vbio-copilot-cache';
const DB_VERSION = 1;
const STORE_NAME = 'kv';
/** Legacy localStorage key prefix written by the previous cache implementation. */
const LEGACY_STORAGE_PREFIX = 'vbio:project-copilot:v2';

/** Minimal async key-value surface; tests inject their own backend. */
export interface CopilotCacheKv {
  get: (key: string) => Promise<unknown>;
  set: (key: string, value: unknown) => Promise<void>;
  del: (key: string) => Promise<void>;
}

// IndexedDB backend, opened once per session. A failed open memoizes its
// rejection so later calls fail fast with the same error.

let dbPromise: Promise<IDBDatabase> | null = null;

function openDb(): Promise<IDBDatabase> {
  if (!dbPromise) {
    dbPromise = new Promise<IDBDatabase>((resolve, reject) => {
      if (typeof indexedDB === 'undefined') {
        reject(new Error('copilot-cache: IndexedDB is unavailable in this environment'));
        return;
      }
      const open = indexedDB.open(DB_NAME, DB_VERSION);
      open.onupgradeneeded = () => {
        const db = open.result;
        if (!db.objectStoreNames.contains(STORE_NAME)) db.createObjectStore(STORE_NAME);
      };
      open.onsuccess = () => resolve(open.result);
      open.onerror = () => reject(open.error ?? new Error('copilot-cache: failed to open database'));
      open.onblocked = () => reject(new Error('copilot-cache: database open blocked by another tab'));
    });
    // no-op catch: keep the memoized rejection from surfacing as unhandled
    dbPromise.catch(() => {});
  }
  return dbPromise;
}

function requestAsPromise<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function txDone(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
  });
}

function idbKv(db: IDBDatabase): CopilotCacheKv {
  return {
    get: async (key) => {
      const tx = db.transaction(STORE_NAME, 'readonly');
      return requestAsPromise(tx.objectStore(STORE_NAME).get(key));
    },
    set: async (key, value) => {
      const tx = db.transaction(STORE_NAME, 'readwrite');
      tx.objectStore(STORE_NAME).put(value, key);
      await txDone(tx);
    },
    del: async (key) => {
      const tx = db.transaction(STORE_NAME, 'readwrite');
      tx.objectStore(STORE_NAME).delete(key);
      await txDone(tx);
    }
  };
}

let kvOverride: CopilotCacheKv | null = null;

/** Test hook: inject a backend (e.g. in-memory) and get a restore function back. */
export function setCopilotCacheKvForTests(kv: CopilotCacheKv | null): () => void {
  const previous = kvOverride;
  kvOverride = kv;
  return () => {
    kvOverride = previous;
  };
}

async function getKv(): Promise<CopilotCacheKv> {
  if (kvOverride) return kvOverride;
  return idbKv(await openDb());
}

// Cache operations (rows are already normalized by callers in supabaseLite)

export function copilotMessageCacheKey(input: {
  contextType: string;
  projectId?: string | null;
  projectTaskId?: string | null;
  userId?: string | null;
  conversationScope?: string | null;
}): string {
  return [
    LEGACY_STORAGE_PREFIX,
    String(input.userId || 'anonymous').trim().toLowerCase() || 'anonymous',
    String(input.conversationScope || 'scoped').trim() || 'scoped',
    String(input.contextType || 'task_list').trim() || 'task_list',
    String(input.projectId || 'project-null').trim() || 'project-null',
    String(input.projectTaskId || 'task-null').trim() || 'task-null'
  ].join(':');
}

function capRows(rows: ProjectCopilotMessage[]): ProjectCopilotMessage[] {
  return rows.length > CACHE_ROW_LIMIT ? rows.slice(-CACHE_ROW_LIMIT) : rows;
}

export async function readCopilotMessageCache(key: string): Promise<ProjectCopilotMessage[]> {
  const kv = await getKv();
  const rows = await kv.get(key);
  return Array.isArray(rows) ? (rows as ProjectCopilotMessage[]) : [];
}

export async function writeCopilotMessageCache(key: string, rows: ProjectCopilotMessage[]): Promise<void> {
  const kv = await getKv();
  await kv.set(key, capRows(rows));
}

export async function appendCopilotMessageCache(key: string, row: ProjectCopilotMessage): Promise<void> {
  const kv = await getKv();
  const rows = await kv.get(key);
  const current = Array.isArray(rows) ? (rows as ProjectCopilotMessage[]) : [];
  await kv.set(key, capRows([...current, row]));
}

export async function filterCopilotMessageCache(
  key: string,
  keep: (row: ProjectCopilotMessage) => boolean
): Promise<void> {
  const kv = await getKv();
  const rows = await kv.get(key);
  if (!Array.isArray(rows)) return;
  await kv.set(key, capRows((rows as ProjectCopilotMessage[]).filter(keep)));
}

// One-time migration from the legacy localStorage blobs

let migrationPromise: Promise<void> | null = null;

export interface StorageLike {
  readonly length: number;
  key(index: number): string | null;
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** Move every legacy `vbio:project-copilot:v2:*` blob into the cache backend and delete it. */
export async function migrateCopilotLegacyStorageCache(storage?: StorageLike): Promise<void> {
  const store = storage || (typeof window !== 'undefined' ? window.localStorage : undefined);
  if (!store) return;
  // Fail loudly if IndexedDB is unavailable; injected test backends skip this check.
  if (!kvOverride) await openDb();
  const keys: string[] = [];
  for (let index = 0; index < store.length; index += 1) {
    const key = store.key(index);
    if (key && key.startsWith(LEGACY_STORAGE_PREFIX)) keys.push(key);
  }
  const kv = await getKv();
  for (const key of keys) {
    try {
      const raw = store.getItem(key);
      if (raw === null) continue;
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) await kv.set(key, parsed);
      store.removeItem(key);
    } catch {
      // Drop corrupted blobs.
      store.removeItem(key);
    }
  }
}

function migrateOnce(): Promise<void> {
  if (!migrationPromise) migrationPromise = migrateCopilotLegacyStorageCache();
  return migrationPromise;
}

/** Read the cache for a scope, migrating any legacy localStorage blob first. */
export async function readCachedCopilotMessages(key: string): Promise<ProjectCopilotMessage[]> {
  await migrateOnce();
  return readCopilotMessageCache(key);
}
