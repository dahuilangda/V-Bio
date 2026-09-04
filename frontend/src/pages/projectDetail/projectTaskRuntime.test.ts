import { describe, expect, it, vi, beforeEach } from 'vitest';
import type { Project, ProjectTask } from '../../types/models';
import { refreshTaskStatus } from './projectTaskRuntime';

vi.mock('../../api/backendApi', () => ({
  downloadResultBlob: vi.fn(),
  getTaskStatus: vi.fn(),
  parseResultBundle: vi.fn()
}));

import { getTaskStatus } from '../../api/backendApi';

const getTaskStatusMock = vi.mocked(getTaskStatus);

function makeProject(taskState: string, taskId = 'tid-1'): Project {
  return {
    id: 'proj-1',
    name: 'P',
    task_type: 'peptide_design',
    task_id: taskId,
    task_state: taskState,
    status_text: '',
    error_text: '',
    confidence: {},
    affinity: {}
  } as unknown as Project;
}

function makeTaskRow(taskState: string, overrides: Partial<ProjectTask> = {}): ProjectTask {
  return {
    id: 'row-1',
    project_id: 'proj-1',
    task_id: 'tid-1',
    task_state: taskState,
    status_text: '',
    error_text: '',
    properties: {},
    confidence: {},
    affinity: {},
    ...overrides
  } as unknown as ProjectTask;
}

function makeParams(project: Project, tasks: ProjectTask[]) {
  const setProject = vi.fn((updater: (prev: Project | null) => Project | null) => updater(project));
  const setProjectTasks = vi.fn((updater: (prev: ProjectTask[]) => ProjectTask[]) => updater(tasks));
  return {
    project,
    projectTasks: tasks,
    statusRefreshInFlightRef: { current: new Set<string>() },
    setError: vi.fn(),
    setStatusInfo: vi.fn(),
    setProject,
    setProjectTasks,
    sortProjectTasks: (rows: ProjectTask[]) => rows,
    patch: vi.fn(async () => project),
    patchTask: vi.fn(async (_taskRowId: string, _payload: Partial<ProjectTask>) => makeTaskRow('SUCCESS')),
    pullResultForViewer: vi.fn(async () => undefined),
    options: {}
  };
}

beforeEach(() => {
  getTaskStatusMock.mockReset();
});

describe('refreshTaskStatus DB write policy', () => {
  it('does not PATCH the DB for progress-only changes while RUNNING', async () => {
    getTaskStatusMock.mockResolvedValue({
      task_id: 'tid-1',
      state: 'RUNNING',
      info: { progress: { status_message: 'Generation 3', elapsed_seconds: 91 } }
    } as never);
    const params = makeParams(makeProject('RUNNING'), [makeTaskRow('RUNNING')]);

    await refreshTaskStatus(params as never);

    expect(params.patch).not.toHaveBeenCalled();
    expect(params.patchTask).not.toHaveBeenCalled();
    expect(params.pullResultForViewer).not.toHaveBeenCalled();
    // Live state still advances in memory.
    expect(params.setProjectTasks).toHaveBeenCalled();
    expect(params.setProject).toHaveBeenCalled();
  });

  it('does not touch state at all when nothing changed between polls', async () => {
    getTaskStatusMock.mockResolvedValue({
      task_id: 'tid-1',
      state: 'RUNNING',
      info: { status: 'Generation 3' }
    } as never);
    const row = makeTaskRow('RUNNING', { status_text: 'Generation 3' });
    const project = makeProject('RUNNING');
    project.status_text = 'Generation 3';
    const params = makeParams(project, [row]);

    const changed = await refreshTaskStatus(params as never);

    // Unchanged tick: no state writes, so downstream identities stay stable.
    expect(changed).toBe(false);
    expect(params.setProjectTasks).not.toHaveBeenCalled();
    expect(params.setProject).not.toHaveBeenCalled();
  });

  it('writes the DB once on a RUNNING→SUCCESS transition and pulls the result', async () => {
    getTaskStatusMock.mockResolvedValue({
      task_id: 'tid-1',
      state: 'SUCCESS',
      info: { progress: { status_message: 'done' } }
    } as never);
    const params = makeParams(makeProject('RUNNING'), [makeTaskRow('RUNNING')]);

    await refreshTaskStatus(params as never);

    expect(params.patch).toHaveBeenCalledTimes(1);
    expect(params.patchTask).toHaveBeenCalledTimes(1);
    const taskPatch = params.patchTask.mock.calls[0][1] as Partial<ProjectTask>;
    expect(taskPatch.task_state).toBe('SUCCESS');
    expect(taskPatch.completed_at).toBeTruthy();
    expect(taskPatch.confidence).toBeUndefined();
    expect(params.pullResultForViewer).toHaveBeenCalledTimes(1);
  });

  it('strips bulk structure text from runtime peptide candidate rows', async () => {
    getTaskStatusMock.mockResolvedValue({
      task_id: 'tid-1',
      state: 'RUNNING',
      info: {
        peptide_design: {
          best_sequences: [
            {
              peptide_sequence: 'ACDE',
              generation: 1,
              rank: 1,
              structure_text: 'data _atom_site.xyz\n' + 'x'.repeat(5000)
            }
          ]
        }
      }
    } as never);
    const params = makeParams(makeProject('RUNNING'), [makeTaskRow('RUNNING')]);

    await refreshTaskStatus(params as never);

    const updater = params.setProjectTasks.mock.calls[0][0] as (prev: ProjectTask[]) => ProjectTask[];
    const nextRows = updater([makeTaskRow('RUNNING')]);
    const confidence = nextRows[0].confidence as Record<string, unknown>;
    const peptideDesign = confidence.peptide_design as Record<string, unknown>;
    const rows = peptideDesign.best_sequences as Array<Record<string, unknown>>;
    expect(rows).toHaveLength(1);
    expect(rows[0].structure_text).toBeUndefined();
    expect(rows[0].peptide_sequence).toBe('ACDE');
  });

  describe('change signal for the adaptive poll cadence', () => {
    it('reports no change when a RUNNING poll returns the same status', async () => {
      getTaskStatusMock.mockResolvedValue({
        task_id: 'tid-1',
        state: 'RUNNING',
        info: { status: 'Generation 3' }
      } as never);
      const row = makeTaskRow('RUNNING', { status_text: 'Generation 3' });
      const project = makeProject('RUNNING');
      project.status_text = 'Generation 3';
      const params = makeParams(project, [row]);

      const changed = await refreshTaskStatus(params as never);

      expect(changed).toBe(false);
    });

    it('reports a change when progress text moves while RUNNING', async () => {
      getTaskStatusMock.mockResolvedValue({
        task_id: 'tid-1',
        state: 'RUNNING',
        info: { status: 'Generation 4' }
      } as never);
      const row = makeTaskRow('RUNNING', { status_text: 'Generation 3' });
      const project = makeProject('RUNNING');
      project.status_text = 'Generation 3';
      const params = makeParams(project, [row]);

      const changed = await refreshTaskStatus(params as never);

      expect(changed).toBe(true);
    });

    it('reports a change on a RUNNING→SUCCESS transition', async () => {
      getTaskStatusMock.mockResolvedValue({
        task_id: 'tid-1',
        state: 'SUCCESS',
        info: { status: 'done' }
      } as never);
      const params = makeParams(makeProject('RUNNING'), [makeTaskRow('RUNNING')]);

      const changed = await refreshTaskStatus(params as never);

      expect(changed).toBe(true);
    });

    it('reports no change when the status fetch fails', async () => {
      getTaskStatusMock.mockRejectedValue(new Error('backend down'));
      const params = makeParams(makeProject('RUNNING'), [makeTaskRow('RUNNING')]);

      const changed = await refreshTaskStatus(params as never);

      expect(changed).toBe(false);
    });
  });
});
