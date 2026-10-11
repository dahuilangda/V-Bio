import { describe, expect, it } from 'vitest';
import {
  isRuntimeTaskState,
  mergeTaskRuntimeFields,
  normalizeAffinityMode,
  overlayRowsWithRuntimeStatus
} from './projectRuntimeRowHelpers';

/**
 * Behavior locks for the runtime-row merge/overlay helpers: a fresh status
 * payload must never regress a terminal row to a non-terminal state (except
 * the deliberate lead-opt scoring re-entry), and runtime status overlays only
 * touch rows whose task_id resolves.
 */

describe('mergeTaskRuntimeFields', () => {
  it('keeps the terminal row when a stale payload arrives', () => {
    const merged = mergeTaskRuntimeFields(
      {
        task_id: 't1',
        task_state: 'PENDING',
        status_text: 'Task is waiting in the queue.',
        error_text: '',
        completed_at: null,
        duration_seconds: null
      },
      {
        task_id: 't1',
        task_state: 'SUCCESS',
        status_text: 'Task completed successfully.',
        error_text: '',
        completed_at: '2026-10-08T00:27:00Z',
        duration_seconds: 120
      }
    );
    expect(merged.task_state).toBe('SUCCESS');
    expect(merged.status_text).toBe('Task completed successfully.');
    expect(merged.completed_at).toBe('2026-10-08T00:27:00Z');
    expect(merged.duration_seconds).toBe(120);
  });

  it('lets a newer terminal payload replace a running row', () => {
    const merged = mergeTaskRuntimeFields(
      {
        task_id: 't1',
        task_state: 'SUCCESS',
        status_text: 'Task completed successfully.',
        error_text: '',
        completed_at: '2026-10-08T00:27:00Z',
        duration_seconds: 30
      },
      {
        task_id: 't1',
        task_state: 'RUNNING',
        status_text: 'Running prediction',
        error_text: '',
        completed_at: null,
        duration_seconds: null
      }
    );
    expect(merged.task_state).toBe('SUCCESS');
    expect(merged.completed_at).toBe('2026-10-08T00:27:00Z');
  });

  it('different task ids fall back to plain field merge', () => {
    const merged = mergeTaskRuntimeFields(
      { task_id: 'a', task_state: 'RUNNING', status_text: 'new', error_text: '', completed_at: null, duration_seconds: null },
      { task_id: 'b', task_state: 'SUCCESS', status_text: 'old', error_text: '', completed_at: 'x', duration_seconds: 1 }
    );
    expect(merged.task_state).toBe('RUNNING');
    expect(merged.status_text).toBe('new');
  });
});

describe('overlayRowsWithRuntimeStatus', () => {
  it('applies runtime state to the matching task_id only', () => {
    const rows = [
      { task_id: 't1', task_state: 'PENDING', status_text: 'queued', error_text: '' },
      { task_id: '', task_state: 'PENDING', status_text: 'queued', error_text: '' }
    ];
    const out = overlayRowsWithRuntimeStatus(rows, {
      t1: { task_id: 't1', state: 'SUCCESS', info: { status: 'Task completed successfully.' } }
    });
    expect(out[0].task_state).toBe('SUCCESS');
    expect(out[1].task_state).toBe('PENDING');
  });

  it('returns the same row object when nothing changed', () => {
    const row = { task_id: 't1', task_state: 'RUNNING', status_text: 'Running', error_text: '' };
    const out = overlayRowsWithRuntimeStatus([row], {
      t1: { task_id: 't1', state: 'RUNNING', info: { status: 'Running' } }
    });
    expect(out[0]).toBe(row);
  });
});

describe('small normalizers', () => {
  it('isRuntimeTaskState accepts only runtime states', () => {
    expect(isRuntimeTaskState('RUNNING')).toBe(true);
    expect(isRuntimeTaskState('QUEUED')).toBe(true);
    expect(isRuntimeTaskState('SUCCESS')).toBe(false);
    expect(isRuntimeTaskState('garbage')).toBe(false);
  });

  it('normalizeAffinityMode falls back to dock', () => {
    expect(normalizeAffinityMode('pose')).toBe('pose');
    expect(normalizeAffinityMode('nonsense')).toBe('dock');
    expect(normalizeAffinityMode(null)).toBe('dock');
  });
});
