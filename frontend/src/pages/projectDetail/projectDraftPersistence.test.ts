import { describe, it, expect } from 'vitest';
import { resolveEditableDraftTaskRowIdFromContext } from './projectDraftPersistence';
import { isDraftTaskSnapshot } from './projectTaskSnapshot';
import type { Project, ProjectTask } from '../../types/models';

/**
 * Editing-isolation matrix for draft-save target resolution:
 *  - a DRAFT row is edited IN PLACE (never duplicated, never a new task);
 *  - finished/runtime rows are never written by a draft save;
 *  - viewing a finished task with an unrelated draft around still creates a
 *    NEW draft instead of silently updating that unrelated draft.
 */

function row(partial: Partial<ProjectTask> & { id: string }): ProjectTask {
  return {
    task_state: 'SUCCESS',
    task_id: `tid-${partial.id}`,
    created_at: '2026-01-01T00:00:00Z',
    ...partial
  } as ProjectTask;
}

function draftRow(id: string, createdAt: string): ProjectTask {
  return row({ id, task_state: 'DRAFT', task_id: '', created_at: createdAt });
}

const PROJECT_NO_RUNTIME = { id: 'p1', task_id: '' } as unknown as Project;
const PROJECT_WITH_RUNTIME = { id: 'p1', task_id: 'tid-runtime' } as unknown as Project;

function resolve(locationSearch: string, project: Project, projectTasks: ProjectTask[], requestNewTask = false) {
  return resolveEditableDraftTaskRowIdFromContext({
    requestNewTask,
    locationSearch,
    project,
    projectTasks,
    isDraftTaskSnapshot
  });
}

describe('resolveEditableDraftTaskRowIdFromContext', () => {
  it('reuses the DRAFT row named by the URL', () => {
    const tasks = [draftRow('d1', '2026-01-01T00:00:00Z')];
    expect(resolve('?task_row_id=d1', PROJECT_NO_RUNTIME, tasks)).toBe('d1');
  });

  it('returns null when the URL names a finished task', () => {
    const tasks = [row({ id: 'done' }), draftRow('d1', '2026-01-01T00:00:00Z')];
    expect(resolve('?task_row_id=done', PROJECT_NO_RUNTIME, tasks)).toBeNull();
  });

  it('falls back to the latest DRAFT row when the project has no runtime task', () => {
    // projectTasks arrive sorted newest-first (submitted_at || created_at desc),
    // matching what the load flow feeds into latestDraftTask
    const tasks = [
      draftRow('d-new', '2026-01-02T00:00:00Z'),
      draftRow('d-old', '2026-01-01T00:00:00Z')
    ];
    expect(resolve('', PROJECT_NO_RUNTIME, tasks)).toBe('d-new');
  });

  it('returns null when there is no DRAFT row at all', () => {
    const tasks = [row({ id: 'done' })];
    expect(resolve('', PROJECT_NO_RUNTIME, tasks)).toBeNull();
  });

  it('never redirects to an unrelated draft while a runtime/finished task is active', () => {
    const tasks = [row({ id: 'runtime', task_id: 'tid-runtime' }), draftRow('d1', '2026-01-01T00:00:00Z')];
    expect(resolve('', PROJECT_WITH_RUNTIME, tasks)).toBeNull();
    expect(resolve('?task_row_id=runtime', PROJECT_WITH_RUNTIME, tasks)).toBeNull();
  });

  it('new-task mode always creates a fresh draft', () => {
    const tasks = [draftRow('d1', '2026-01-01T00:00:00Z')];
    expect(resolve('?task_row_id=d1', PROJECT_NO_RUNTIME, tasks, true)).toBeNull();
    expect(resolve('', PROJECT_NO_RUNTIME, tasks, true)).toBeNull();
  });
});
