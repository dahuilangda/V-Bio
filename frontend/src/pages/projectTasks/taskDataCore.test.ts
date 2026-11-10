import { describe, expect, it } from 'vitest';
import type { InputComponent, ProjectTask } from '../../types/models';
import { resolveTaskSelectionContext } from './taskDataCore';

function taskWith(components: InputComponent[], properties: Record<string, unknown>): ProjectTask {
  return {
    id: 'row-1',
    task_id: 'cel-1',
    project_id: 'p-1',
    name: 'prediction task',
    task_state: 'SUCCESS',
    backend: 'protenix',
    components,
    properties,
    submitted_at: '2026-11-09T00:00:00Z',
    created_at: '2026-11-09T00:00:00Z'
  } as unknown as ProjectTask;
}

function protein(id: string, sequence: string): InputComponent {
  return { id, type: 'protein', sequence, numCopies: 1 } as InputComponent;
}

function ligand(id: string, smiles: string): InputComponent {
  return { id, type: 'ligand', sequence: smiles, inputMethod: 'jsme', numCopies: 1 } as InputComponent;
}

describe('resolveTaskSelectionContext binding ligand default', () => {
  it('falls back to the first non-target component when binder/ligand properties were never saved', () => {
    // 对齐 Binding 面板默认：target = 第一个非配体组件，ligand = 剩余组件之首
    const task = taskWith(
      [protein('c1', 'FLYKVNEYV'), protein('c2', 'CREEECHWLANRRFSC')],
      { target: null, ligand: null, binder: null, affinity: false }
    );
    const selection = resolveTaskSelectionContext(task, undefined, 'prediction');
    expect(selection.ligandSequence).toBe('CREEECHWLANRRFSC');
    expect(selection.ligandSequenceType).toBe('protein');
    expect(selection.ligandSmiles).toBe('');
  });

  it('prefers a small molecule over polymer components under the same default', () => {
    const task = taskWith(
      [protein('c1', 'FLYKVNEYV'), protein('c2', 'CREEECHWLANRRFSC'), ligand('c3', 'c1ccccc1')],
      {}
    );
    const selection = resolveTaskSelectionContext(task, undefined, 'prediction');
    expect(selection.ligandSmiles).toBe('c1ccccc1');
    expect(selection.ligandIsSmiles).toBe(true);
    expect(selection.ligandSequence).toBe('');
  });

  it('keeps an explicit binder selection ahead of the default', () => {
    const task = taskWith([protein('c1', 'FLYKVNEYV'), protein('c2', 'CREEECHWLANRRFSC')], {
      binder: 'B'
    });
    const selection = resolveTaskSelectionContext(task, undefined, 'prediction');
    expect(selection.ligandSequence).toBe('CREEECHWLANRRFSC');
    expect(selection.ligandChainId).toBe('B');
  });

  it('renders nothing for a single-component task without any binder candidate', () => {
    const task = taskWith([protein('c1', 'FLYKVNEYV')], {});
    const selection = resolveTaskSelectionContext(task, undefined, 'prediction');
    expect(selection.ligandSequence).toBe('');
    expect(selection.ligandSmiles).toBe('');
    expect(selection.ligandChainId).toBeNull();
  });
});
