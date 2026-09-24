import { describe, it, expect, beforeEach } from 'vitest';
import {
  copilotMessageCacheKey,
  filterCopilotMessageCache,
  appendCopilotMessageCache,
  migrateCopilotLegacyStorageCache,
  readCopilotMessageCache,
  readCachedCopilotMessages,
  setCopilotCacheKvForTests,
  writeCopilotMessageCache
} from './copilotLocalCache';
import type { CopilotCacheKv, StorageLike } from './copilotLocalCache';
import type { ProjectCopilotMessage } from '../types/models';

function message(id: string, sessionId = 's1'): ProjectCopilotMessage {
  return {
    id,
    context_type: 'task_detail',
    project_id: null,
    project_task_id: null,
    user_id: 'u1',
    role: 'user',
    content: `content ${id}`,
    metadata: { session_id: sessionId, owner_user_id: 'u1' },
    created_at: `2026-01-01T00:00:0${id.length % 10}Z`,
    updated_at: `2026-01-01T00:00:0${id.length % 10}Z`,
    username: 'goai2026',
    user_name: 'GOAI2026'
  } as ProjectCopilotMessage;
}

function memoryKv(): CopilotCacheKv & { dump: () => Map<string, unknown> } {
  const map = new Map<string, unknown>();
  return {
    get: async (key) => map.get(key),
    set: async (key, value) => {
      map.set(key, value);
    },
    del: async (key) => {
      map.delete(key);
    },
    dump: () => map
  };
}

function fakeStorage(entries: Map<string, string>): StorageLike {
  return {
    get length() {
      return entries.size;
    },
    key: (index: number) => Array.from(entries.keys())[index] ?? null,
    getItem: (key: string) => entries.get(key) ?? null,
    removeItem: (key: string) => {
      entries.delete(key);
    },
    setItem: (key: string, value: string) => {
      entries.set(key, value);
    }
  };
}

let kv: ReturnType<typeof memoryKv>;

beforeEach(() => {
  kv = memoryKv();
  setCopilotCacheKvForTests(kv);
});

describe('copilotMessageCacheKey', () => {
  it('derives a stable per-scope key', () => {
    const a = copilotMessageCacheKey({ contextType: 'task_detail', userId: 'u1' });
    const b = copilotMessageCacheKey({ contextType: 'task_detail', userId: 'u1' });
    const c = copilotMessageCacheKey({ contextType: 'task_detail', userId: 'u2' });
    expect(a).toBe(b);
    expect(a).not.toBe(c);
  });

  it('keeps the legacy key layout so migrated blobs land under the same key', () => {
    expect(copilotMessageCacheKey({ contextType: 'task_list', userId: 'u1', conversationScope: 'global' }))
      .toBe('vbio:project-copilot:v2:u1:global:task_list:project-null:task-null');
  });
});

describe('message cache operations', () => {
  it('round-trips rows', async () => {
    const key = copilotMessageCacheKey({ contextType: 'task_detail', userId: 'u1' });
    await writeCopilotMessageCache(key, [message('m1'), message('m2')]);
    expect(await readCopilotMessageCache(key)).toHaveLength(2);
  });

  it('caps stored rows at 200 keeping the newest', async () => {
    const key = copilotMessageCacheKey({ contextType: 'task_detail', userId: 'u1' });
    const rows = Array.from({ length: 250 }, (_, index) => message(`m${index}`));
    await writeCopilotMessageCache(key, rows);
    const stored = await readCopilotMessageCache(key);
    expect(stored).toHaveLength(200);
    expect(stored[0].id).toBe('m50');
    expect(stored[199].id).toBe('m249');
  });

  it('appends a row without touching other scopes', async () => {
    const keyA = copilotMessageCacheKey({ contextType: 'task_detail', userId: 'u1' });
    const keyB = copilotMessageCacheKey({ contextType: 'task_list', userId: 'u1' });
    await writeCopilotMessageCache(keyA, [message('m1')]);
    await writeCopilotMessageCache(keyB, [message('m2')]);
    await appendCopilotMessageCache(keyA, message('m3'));
    expect((await readCopilotMessageCache(keyA)).map((row) => row.id)).toEqual(['m1', 'm3']);
    expect((await readCopilotMessageCache(keyB)).map((row) => row.id)).toEqual(['m2']);
  });

  it('filters rows in place', async () => {
    const key = copilotMessageCacheKey({ contextType: 'task_detail', userId: 'u1' });
    await writeCopilotMessageCache(key, [message('m1'), message('m2'), message('m3')]);
    await filterCopilotMessageCache(key, (row) => row.id !== 'm2');
    expect((await readCopilotMessageCache(key)).map((row) => row.id)).toEqual(['m1', 'm3']);
  });

  it('reads an empty array from an unknown key', async () => {
    expect(await readCopilotMessageCache('nope')).toEqual([]);
  });

  it('survives a non-array blob without throwing', async () => {
    const key = copilotMessageCacheKey({ contextType: 'task_detail', userId: 'u1' });
    await kv.set(key, 'not-an-array');
    expect(await readCopilotMessageCache(key)).toEqual([]);
  });

  it('propagates backend failures instead of absorbing them (no fallback convention)', async () => {
    const broken: CopilotCacheKv = {
      get: async () => {
        throw new Error('idb broken');
      },
      set: async () => {
        throw new Error('idb broken');
      },
      del: async () => {
        throw new Error('idb broken');
      }
    };
    setCopilotCacheKvForTests(broken);
    await expect(readCopilotMessageCache('k')).rejects.toThrow('idb broken');
    await expect(writeCopilotMessageCache('k', [message('m1')])).rejects.toThrow('idb broken');
    await expect(appendCopilotMessageCache('k', message('m1'))).rejects.toThrow('idb broken');
    await expect(filterCopilotMessageCache('k', () => true)).rejects.toThrow('idb broken');
  });
});

describe('legacy localStorage migration', () => {
  it('moves legacy blobs into the cache and removes them from storage', async () => {
    const legacyKey = 'vbio:project-copilot:v2:u1:global:task_list:project-null:task-null';
    const entries = new Map<string, string>([
      [legacyKey, JSON.stringify([message('m1'), message('m2')])],
      ['vbio:some-other-key', 'keep me']
    ]);
    const storage = fakeStorage(entries);

    await migrateCopilotLegacyStorageCache(storage);

    expect(entries.has(legacyKey)).toBe(false);
    expect(entries.get('vbio:some-other-key')).toBe('keep me');
    expect((await readCopilotMessageCache(legacyKey)).map((row) => row.id)).toEqual(['m1', 'm2']);
  });

  it('drops a corrupted legacy blob instead of throwing', async () => {
    const legacyKey = 'vbio:project-copilot:v2:u1:scoped:task_detail:p1:t1';
    const entries = new Map<string, string>([[legacyKey, '{not json']]);
    const storage = fakeStorage(entries);

    await migrateCopilotLegacyStorageCache(storage);

    expect(entries.has(legacyKey)).toBe(false);
  });

  it('is a no-op when storage has no legacy keys', async () => {
    const entries = new Map<string, string>([['vbio:unrelated', '1']]);
    const storage = fakeStorage(entries);

    await migrateCopilotLegacyStorageCache(storage);

    expect(entries.get('vbio:unrelated')).toBe('1');
    expect(kv.dump().size).toBe(0);
  });

  it('readCachedCopilotMessages reads through the kv backend', async () => {
    const key = copilotMessageCacheKey({ contextType: 'task_detail', userId: 'u1' });
    await kv.set(key, [message('m1')]);
    expect(await readCachedCopilotMessages(key)).toHaveLength(1);
  });
});
