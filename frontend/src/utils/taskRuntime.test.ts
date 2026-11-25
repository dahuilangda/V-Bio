import { describe, expect, it } from 'vitest';
import { readTaskRuntimeCompletedAt } from './taskRuntime';

const DONE_AT = '2026-11-24T23:15:31.658158+00:00';

describe('readTaskRuntimeCompletedAt', () => {
  it('returns the time recorded by the runtime', () => {
    expect(readTaskRuntimeCompletedAt({ state: 'FAILURE', info: { exc_message: 'Engine failed', completed_at: DONE_AT } })).toBe(DONE_AT);
  });

  it('returns null when the runtime recorded no time', () => {
    expect(readTaskRuntimeCompletedAt({ state: 'FAILURE', info: { exc_message: 'Engine failed' } })).toBeNull();
    expect(readTaskRuntimeCompletedAt({ state: 'FAILURE' })).toBeNull();
    expect(readTaskRuntimeCompletedAt({ state: 'FAILURE', info: { completed_at: '  ' } })).toBeNull();
  });
});
