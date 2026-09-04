import { describe, it, expect } from 'vitest';
import {
  resolveEditableDraftTaskRowIdFromContext,
  resolveTerminalTaskRowIdFromContext
} from './projectDraftPersistence';
import { isDraftTaskSnapshot } from './projectTaskSnapshot';
import type { Project, ProjectTask } from '../../types/models';

/**
 * Editing-isolation matrix for draft-save target resolution:
 *  - a DRAFT row is edited IN PLACE (never duplicated, never a new task);
 *  - a COMPLETED/ERRORED task being viewed is renamed IN PLACE by a
 *    metadata-only save (terminal tasks are renamed in place, never duplicated);
 *  - running rows never match either resolver, so a save while viewing one
 *    still falls through to the full-save INSERT.
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

function resolveTerminal(locationSearch: string, project: Project, projectTasks: ProjectTask[], requestNewTask = false) {
  return resolveTerminalTaskRowIdFromContext({
    requestNewTask,
    locationSearch,
    project,
    projectTasks
  });
}

describe('resolveTerminalTaskRowIdFromContext', () => {
  it('resolves the completed task named by the URL', () => {
    const tasks = [row({ id: 'done', task_state: 'SUCCESS' })];
    expect(resolveTerminal('?task_row_id=done', PROJECT_NO_RUNTIME, tasks)).toBe('done');
  });

  it('resolves an errored task named by the URL (FAILURE and REVOKED)', () => {
    const failed = [row({ id: 'failed', task_state: 'FAILURE' })];
    expect(resolveTerminal('?task_row_id=failed', PROJECT_NO_RUNTIME, failed)).toBe('failed');
    const revoked = [row({ id: 'revoked', task_state: 'REVOKED' })];
    expect(resolveTerminal('?task_row_id=revoked', PROJECT_NO_RUNTIME, revoked)).toBe('revoked');
  });

  it('returns null for a RUNNING task, a DRAFT row, or an unknown row id', () => {
    const tasks = [
      row({ id: 'running', task_state: 'RUNNING' }),
      draftRow('d1', '2026-01-01T00:00:00Z')
    ];
    expect(resolveTerminal('?task_row_id=running', PROJECT_NO_RUNTIME, tasks)).toBeNull();
    expect(resolveTerminal('?task_row_id=d1', PROJECT_NO_RUNTIME, tasks)).toBeNull();
    expect(resolveTerminal('?task_row_id=missing', PROJECT_NO_RUNTIME, tasks)).toBeNull();
  });

  it('falls back to the project runtime row when it is terminal', () => {
    const tasks = [row({ id: 'runtime', task_id: 'tid-runtime', task_state: 'FAILURE' })];
    expect(resolveTerminal('', PROJECT_WITH_RUNTIME, tasks)).toBe('runtime');
    const running = [row({ id: 'runtime', task_id: 'tid-runtime', task_state: 'RUNNING' })];
    expect(resolveTerminal('', PROJECT_WITH_RUNTIME, running)).toBeNull();
  });

  it('new-task mode never resolves a terminal row', () => {
    const tasks = [row({ id: 'done', task_state: 'SUCCESS' })];
    expect(resolveTerminal('?task_row_id=done', PROJECT_NO_RUNTIME, tasks, true)).toBeNull();
    expect(resolveTerminal('', PROJECT_WITH_RUNTIME, tasks, true)).toBeNull();
  });
});
