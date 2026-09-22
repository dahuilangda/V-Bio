/**
 * TTL cache for inline-completion results, keyed by the exact draft text
 * (plus context identity). Short TTL keeps suggestions tied to the
 * current conversation context.
 */
const DEFAULT_TTL_MS = 30_000;
const DEFAULT_CAP = 50;

export interface CopilotCompletionCache {
  get: (key: string) => string[] | null;
  put: (key: string, ranked: string[]) => void;
  clear: () => void;
}

export function completionCacheKey(input: {
  contextType: string;
  userId: string;
  draft: string;
}): string {
  return `${input.contextType}\u0000${input.userId}\u0000${input.draft}`;
}

export function createCompletionCache(
  ttlMs: number = DEFAULT_TTL_MS,
  cap: number = DEFAULT_CAP
): CopilotCompletionCache {
  const entries = new Map<string, { at: number; ranked: string[] }>();
  return {
    get: (key: string) => {
      const hit = entries.get(key);
      if (!hit) return null;
      if (Date.now() - hit.at > ttlMs) {
        entries.delete(key);
        return null;
      }
      // Refresh recency for the LRU bound.
      entries.delete(key);
      entries.set(key, hit);
      return hit.ranked;
    },
    put: (key: string, ranked: string[]) => {
      if (ranked.length === 0) return;
      entries.delete(key);
      entries.set(key, { at: Date.now(), ranked });
      if (entries.size > cap) {
        const oldest = entries.keys().next().value;
        if (oldest !== undefined) entries.delete(oldest);
      }
    },
    clear: () => {
      entries.clear();
    }
  };
}
