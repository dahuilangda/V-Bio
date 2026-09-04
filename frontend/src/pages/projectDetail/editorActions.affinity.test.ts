import { describe, expect, it } from 'vitest';
import type { Dispatch, SetStateAction } from 'react';
import { setAffinityComponentFromWorkspaceAction } from './editorActions';

interface DraftLike {
  backend: string;
  inputConfig: {
    version: number;
    components: never[];
    constraints: never[];
    properties: {
      affinity: boolean;
      target?: string | null;
      ligand?: string | null;
      binder?: string | null;
    };
  };
}

function buildDraft(properties: DraftLike['inputConfig']['properties']): DraftLike {
  return {
    backend: 'boltz',
    inputConfig: {
      version: 1,
      components: [],
      constraints: [],
      properties
    }
  };
}

function applyDraftAction(draft: DraftLike, invoke: (setDraft: Dispatch<SetStateAction<DraftLike | null>>) => void): DraftLike | null {
  let current: DraftLike | null = draft;
  const setDraft: Dispatch<SetStateAction<DraftLike | null>> = (updater) => {
    current = typeof updater === 'function' ? (updater as (prev: DraftLike | null) => DraftLike | null)(current) : updater;
  };
  invoke(setDraft);
  return current;
}

const workspaceTargetOptions = [
  { componentId: 'comp-1', chainId: 'A' },
  { componentId: 'comp-4', chainId: 'D' }
];
const workspaceLigandSelectableOptions = [
  { componentId: 'comp-2', chainId: 'B', isSmallMolecule: false },
  { componentId: 'comp-3', chainId: 'C', isSmallMolecule: true }
];

describe('setAffinityComponentFromWorkspaceAction', () => {
  it('drops a checked affinity head when the ligand switches to a polymer component', () => {
    const draft = buildDraft({ affinity: true, target: 'A', ligand: 'C', binder: 'C' });
    const next = applyDraftAction(draft, (setDraft) =>
      setAffinityComponentFromWorkspaceAction({
        field: 'ligand',
        componentId: 'comp-2',
        workspaceTargetOptions,
        workspaceLigandSelectableOptions,
        setDraft
      })
    );
    expect(next?.inputConfig.properties).toEqual({ affinity: false, target: 'A', ligand: 'B', binder: 'B' });
  });

  it('keeps affinity untouched when the ligand switches to a small-molecule component', () => {
    const draft = buildDraft({ affinity: false, target: 'A', ligand: 'B', binder: 'B' });
    const next = applyDraftAction(draft, (setDraft) =>
      setAffinityComponentFromWorkspaceAction({
        field: 'ligand',
        componentId: 'comp-3',
        workspaceTargetOptions,
        workspaceLigandSelectableOptions,
        setDraft
      })
    );
    expect(next?.inputConfig.properties).toEqual({ affinity: false, target: 'A', ligand: 'C', binder: 'C' });
  });

  it('leaves affinity and binder alone when only the target changes', () => {
    const draft = buildDraft({ affinity: true, target: 'A', ligand: 'C', binder: 'C' });
    const next = applyDraftAction(draft, (setDraft) =>
      setAffinityComponentFromWorkspaceAction({
        field: 'target',
        componentId: 'comp-4',
        workspaceTargetOptions,
        workspaceLigandSelectableOptions,
        setDraft
      })
    );
    expect(next?.inputConfig.properties).toEqual({ affinity: true, target: 'D', ligand: 'C', binder: 'C' });
  });
});
