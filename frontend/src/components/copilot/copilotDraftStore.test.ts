import { describe, it, expect, vi } from 'vitest';
import { createCopilotDraftStore } from './copilotDraftStore';

describe('createCopilotDraftStore', () => {
  it('holds the initial value', () => {
    const store = createCopilotDraftStore('hello');
    expect(store.get()).toBe('hello');
  });

  it('defaults to an empty draft', () => {
    expect(createCopilotDraftStore().get()).toBe('');
  });

  it('set() updates the value synchronously', () => {
    const store = createCopilotDraftStore('');
    store.set('abc');
    expect(store.get()).toBe('abc');
  });

  it('set() with an unchanged value does not notify listeners', () => {
    const store = createCopilotDraftStore('same');
    const listener = vi.fn();
    store.subscribe(listener);
    store.set('same');
    expect(listener).not.toHaveBeenCalled();
  });

  it('notifies subscribers on change and supports unsubscribe', () => {
    const store = createCopilotDraftStore('');
    const listener = vi.fn();
    const unsubscribe = store.subscribe(listener);
    store.set('a');
    expect(listener).toHaveBeenCalledTimes(1);
    unsubscribe();
    store.set('b');
    expect(listener).toHaveBeenCalledTimes(1);
    expect(store.get()).toBe('b');
  });

  it('notifies multiple subscribers in registration order', () => {
    const store = createCopilotDraftStore('');
    const calls: string[] = [];
    const offA = store.subscribe(() => calls.push('a'));
    const offB = store.subscribe(() => calls.push('b'));
    store.set('next');
    expect(calls).toEqual(['a', 'b']);
    offA();
    offB();
  });

  it('stops notifying a listener after unsubscribe is called twice', () => {
    const store = createCopilotDraftStore('');
    const listener = vi.fn();
    const unsubscribe = store.subscribe(listener);
    unsubscribe();
    unsubscribe();
    store.set('x');
    expect(listener).not.toHaveBeenCalled();
  });
});
