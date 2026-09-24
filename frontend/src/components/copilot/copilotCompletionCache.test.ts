import { describe, it, expect } from 'vitest';
import { completionCacheKey, createCompletionCache } from './copilotCompletionCache';

describe('completionCacheKey', () => {
  it('separates context, user and draft', () => {
    expect(completionCacheKey({ contextType: 'a', userId: 'u', draft: 'x' }))
      .not.toBe(completionCacheKey({ contextType: 'ab', userId: 'u', draft: 'x' }));
    expect(completionCacheKey({ contextType: 'a', userId: 'u', draft: 'x' }))
      .not.toBe(completionCacheKey({ contextType: 'a', userId: 'uu', draft: 'x' }));
    expect(completionCacheKey({ contextType: 'a', userId: 'u', draft: 'x' }))
      .not.toBe(completionCacheKey({ contextType: 'a', userId: 'u', draft: 'xy' }));
  });
});

describe('createCompletionCache', () => {
  it('round-trips a ranked result', () => {
    const cache = createCompletionCache(1000, 5);
    const key = completionCacheKey({ contextType: 'task_detail', userId: 'u1', draft: '最好' });
    cache.put(key, ['的导出']);
    expect(cache.get(key)).toEqual(['的导出']);
  });

  it('misses on an unknown key', () => {
    const cache = createCompletionCache(1000, 5);
    expect(cache.get('nope')).toBeNull();
  });

  it('expires entries after the TTL', async () => {
    const cache = createCompletionCache(20, 5);
    const key = 'k';
    cache.put(key, ['a']);
    expect(cache.get(key)).toEqual(['a']);
    await new Promise((resolve) => setTimeout(resolve, 40));
    expect(cache.get(key)).toBeNull();
  });

  it('never stores empty result lists (a miss must not mask a later retry)', () => {
    const cache = createCompletionCache(1000, 5);
    const key = 'k';
    cache.put(key, []);
    expect(cache.get(key)).toBeNull();
  });

  it('evicts the least-recently-used entry beyond the cap', () => {
    const cache = createCompletionCache(60_000, 2);
    cache.put('a', ['1']);
    cache.put('b', ['2']);
    cache.get('a'); // refresh a
    cache.put('c', ['3']); // evicts b
    expect(cache.get('a')).toEqual(['1']);
    expect(cache.get('b')).toBeNull();
    expect(cache.get('c')).toEqual(['3']);
  });

  it('clear wipes everything', () => {
    const cache = createCompletionCache(60_000, 5);
    cache.put('a', ['1']);
    cache.clear();
    expect(cache.get('a')).toBeNull();
  });
});
