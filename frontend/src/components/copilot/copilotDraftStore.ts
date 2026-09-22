/**
 * Observable store for the Copilot composer draft. The draft lives here (not in
 * modal state) because it changes on every keystroke and would re-render the
 * whole panel; only the composer subscribes, and the modal reads/writes the
 * draft imperatively.
 */
import { useSyncExternalStore } from 'react';

export interface CopilotDraftStore {
  get: () => string;
  set: (value: string) => void;
  subscribe: (listener: (value: string) => void) => () => void;
}

export function createCopilotDraftStore(initial: string = ''): CopilotDraftStore {
  let value = initial;
  const listeners = new Set<(value: string) => void>();
  return {
    get: () => value,
    set: (next: string) => {
      if (next === value) return;
      value = next;
      for (const listener of listeners) listener(value);
    },
    subscribe: (listener: (value: string) => void) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    }
  };
}

/** Subscribe a component to the draft value. */
export function useCopilotDraft(store: CopilotDraftStore): string {
  return useSyncExternalStore(store.subscribe, store.get, store.get);
}
