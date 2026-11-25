export interface BundleProgress {
  loaded: number;
  total: number;
}

export interface BundleState<T> {
  status: 'idle' | 'loading' | 'ready' | 'error';
  progress: BundleProgress | null;
  value: T | null;
  error: Error | null;
}

export interface BundleDefinition<T> {
  load: (onProgress: (progress: BundleProgress | null) => void) => Promise<T>;
}

type Listener<T> = (state: BundleState<T>) => void;

interface Entry<T> extends BundleState<T> {
  waiters: Set<{
    resolve: (value: T) => void;
    reject: (error: Error) => void;
  }>;
  subscribers: Set<Listener<T>>;
}

const entries = new Map<string, Entry<unknown>>();

function entry<T>(key: string): Entry<T> {
  let current = entries.get(key);
  if (!current) {
    current = {
      status: 'idle',
      progress: null,
      value: null,
      error: null,
      waiters: new Set(),
      subscribers: new Set()
    };
    entries.set(key, current);
  }
  return current as Entry<T>;
}

function snapshot<T>(target: Entry<T>): BundleState<T> {
  const { status, progress, value, error } = target;
  return { status, progress, value, error };
}

function settle<T>(key: string, state: BundleState<T>) {
  const target = entry<T>(key);
  Object.assign(target, state);
  const current = snapshot(target);
  for (const listener of target.subscribers) listener(current);
  const { waiters } = target;
  target.waiters = new Set();
  for (const waiter of waiters) {
    if (current.status === 'ready') waiter.resolve(current.value as T);
    else waiter.reject(current.error as Error);
  }
}

function settled<T>(key: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    entry<T>(key).waiters.add({ resolve, reject });
  });
}

export function readBundle<T>(key: string): BundleState<T> {
  const current = entries.get(key);
  return current ? (snapshot(current) as BundleState<T>) : { status: 'idle', progress: null, value: null, error: null };
}

export function loadBundle<T>(key: string, definition: BundleDefinition<T>): Promise<T> {
  const { status } = readBundle<T>(key);
  if (status === 'ready') return Promise.resolve(readBundle<T>(key).value as T);
  if (status === 'loading') return settled<T>(key);
  settle(key, { status: 'loading', progress: null, value: null, error: null });
  definition
    .load((progress) => {
      if (readBundle<T>(key).status === 'loading') {
        Object.assign(entry<T>(key), { progress });
        const current = snapshot(entry<T>(key));
        for (const listener of entry<T>(key).subscribers) listener(current);
      }
    })
    .then(
      (value) => settle(key, { status: 'ready', progress: null, value, error: null }),
      (error: unknown) =>
        settle(key, {
          status: 'error',
          progress: null,
          value: null,
          error: error instanceof Error ? error : new Error(String(error))
        })
    );
  return settled<T>(key);
}

export function subscribeBundle<T>(key: string, listener: Listener<T>): () => void {
  const target = entry<T>(key);
  target.subscribers.add(listener);
  listener(snapshot(target));
  return () => {
    target.subscribers.delete(listener);
  };
}
