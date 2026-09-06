import { describe, expect, it } from 'vitest';
import type { ProjectTask } from '../../types/models';
import { TaskRowDeletionLedger } from './taskRowDeletionLedger';
// Imported from the loader (not re-implemented here) so the test pins the exact
// merge path every writer goes through — mergeTaskRowPages is add-only, which
// is precisely what resurrected deleted rows before the ledger existed.
import { mergeTaskRowPages } from './useProjectTasksDataLoader';

function makeRow(id: string, overrides: Partial<ProjectTask> = {}): ProjectTask {
  return {
    id,
    project_id: 'proj-1',
    task_id: `runtime-${id}`,
    task_state: 'SUCCESS',
    status_text: 'done',
    error_text: '',
    name: `Task ${id}`,
    confidence: {},
    affinity: {},
    properties: {},
    submitted_at: '2026-09-06T00:00:00Z',
    completed_at: '2026-09-06T00:01:00Z',
    ...overrides
  } as ProjectTask;
}

describe('TaskRowDeletionLedger', () => {
  it('returns the input array untouched while empty (no allocation on hot paths)', () => {
    const ledger = new TaskRowDeletionLedger();
    const rows = [makeRow('a'), makeRow('b')];
    expect(ledger.apply(rows)).toBe(rows);
  });

  it('drops a row deleted after the caller captured its snapshot', () => {
    const ledger = new TaskRowDeletionLedger();
    const rows = [makeRow('a'), makeRow('b'), makeRow('c')];
    ledger.markDeleted('b');
    expect(ledger.apply(rows).map((row) => row.id)).toEqual(['a', 'c']);
    expect(ledger.isDeleted('b')).toBe(true);
    expect(ledger.isDeleted('a')).toBe(false);
  });

  it('ignores empty ids and tolerates whitespace', () => {
    const ledger = new TaskRowDeletionLedger();
    ledger.markDeleted('');
    ledger.markDeleted('   ');
    expect(ledger.isDeleted('')).toBe(false);
    ledger.markDeleted(' b ');
    expect(ledger.isDeleted('b')).toBe(true);
  });

  it('reset forgets deletions (loader switching to another project)', () => {
    const ledger = new TaskRowDeletionLedger();
    ledger.markDeleted('a');
    ledger.reset();
    expect(ledger.apply([makeRow('a')])).toHaveLength(1);
  });
});

describe('deleted task rows cannot be resurrected by stale writers', () => {
  // The user-visible bug: delete a task from the list, it disappears, then a
  // writer that had captured its rows before the deletion (runtime-status poll,
  // list refresh, pagination chunk) resolves and merges the row back in; a page
  // reload then shows it gone. Every scenario below replays one such writer
  // against the exact merge + filter combination the loader applies.
  const deletedRow = makeRow('row-deleted');
  const survivor = makeRow('row-survivor');

  it('runtime-status poll: snapshot taken pre-delete, resolved post-delete', () => {
    const ledger = new TaskRowDeletionLedger();
    // Poll tick captured the pre-deletion snapshot (row still present).
    const pollSnapshot = [deletedRow, survivor];
    // Deletion commits server-side and locally while the poll awaits.
    ledger.markDeleted(deletedRow.id);
    const stateAfterDelete = ledger.apply(pollSnapshot);
    // Poll resolves: loader merges its (stale) synced rows over current state.
    const merged = mergeTaskRowPages(ledger.apply(pollSnapshot), stateAfterDelete);
    expect(merged.map((row) => row.id)).toEqual([survivor.id]);
  });

  it('list refresh: stale fetch response and stale cached rows both carry the row', () => {
    const ledger = new TaskRowDeletionLedger();
    const cachedTasks = [deletedRow, survivor];
    const staleFetchRows = [deletedRow, survivor];
    ledger.markDeleted(deletedRow.id);
    const nextRows = ledger.apply(mergeTaskRowPages(staleFetchRows, cachedTasks));
    expect(nextRows.map((row) => row.id)).toEqual([survivor.id]);
  });

  it('pagination chunk: chunk requested pre-delete carries the row', () => {
    const ledger = new TaskRowDeletionLedger();
    const stateWithoutDeleted = [survivor];
    const staleChunkRows = [deletedRow];
    ledger.markDeleted(deletedRow.id);
    const merged = mergeTaskRowPages(ledger.apply(staleChunkRows), stateWithoutDeleted);
    expect(merged.map((row) => row.id)).toEqual([survivor.id]);
  });

  it('fresh rows written after reset (another project) are unaffected', () => {
    const ledger = new TaskRowDeletionLedger();
    ledger.markDeleted(deletedRow.id);
    ledger.reset();
    const merged = mergeTaskRowPages([deletedRow, survivor], []);
    expect(merged.map((row) => row.id).sort()).toEqual([deletedRow.id, survivor.id].sort());
  });
});
