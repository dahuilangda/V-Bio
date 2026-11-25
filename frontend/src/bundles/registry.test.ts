import { describe, expect, it, vi } from 'vitest';
import { loadBundle, subscribeBundle, type BundleProgress } from './registry';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function definition<T>(progress?: BundleProgress | null) {
  const task = deferred<T>();
  const load = vi.fn((onProgress: (value: BundleProgress | null) => void) => {
    if (progress) onProgress(progress);
    return task.promise;
  });
  return { task, definition: { load } };
}

describe('loadBundle', () => {
  it('shares one load across concurrent callers', async () => {
    const { task, definition: bundle } = definition<number>();
    const first = loadBundle('shared', bundle);
    const second = loadBundle('shared', bundle);
    task.resolve(7);
    expect(await Promise.all([first, second])).toEqual([7, 7]);
    expect(bundle.load).toHaveBeenCalledTimes(1);
  });

  it('resolves later callers from the settled value without reloading', async () => {
    const { task, definition: bundle } = definition<string>();
    const first = loadBundle('cached', bundle);
    task.resolve('ok');
    await first;
    expect(await loadBundle('cached', { load: vi.fn() })).toBe('ok');
    expect(bundle.load).toHaveBeenCalledTimes(1);
  });

  it('rejects every caller and loads again on the next call after a failure', async () => {
    const { task, definition: bundle } = definition<number>();
    const first = loadBundle('retry', bundle);
    const second = loadBundle('retry', bundle);
    task.reject(new Error('offline'));
    await expect(first).rejects.toThrow('offline');
    await expect(second).rejects.toThrow('offline');
    const next = definition<number>();
    const third = loadBundle('retry', next.definition);
    next.task.resolve(3);
    expect(await third).toBe(3);
  });

  it('publishes progress and settlement to subscribers', async () => {
    const seen: string[] = [];
    const unsubscribe = subscribeBundle<number>('progress', (state) => {
      seen.push(`${state.status}:${state.progress?.loaded ?? '-'}`);
    });
    const { task, definition: bundle } = definition<number>({ loaded: 1, total: 4 });
    const first = loadBundle('progress', bundle);
    unsubscribe();
    task.resolve(1);
    await first;
    expect(seen).toEqual(['idle:-', 'loading:-', 'loading:1']);
  });

  it('stops notifying after unsubscribe', async () => {
    let calls = 0;
    const unsubscribe = subscribeBundle<number>('quiet', () => {
      calls += 1;
    });
    unsubscribe();
    const { task, definition: bundle } = definition<number>();
    const first = loadBundle('quiet', bundle);
    task.resolve(1);
    await first;
    expect(calls).toBe(1);
  });
});
