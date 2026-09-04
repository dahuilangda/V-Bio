import { describe, it, expect, vi } from 'vitest';
import { saveProjectDraftFromWorkspace, type SaveDraftDeps } from './projectDraftSave';
import type { CustomCcdMoleculeInput, Project, ProjectInputConfig, ProjectTask } from '../../types/models';
import type { AffinityPersistedUploads } from '../../hooks/useAffinityWorkflow';

/**
 * Regression: a metadata-only save (Basics name/summary edit) must rename
 * exactly one existing row and never insert another. While VIEWING a
 * completed or errored task (terminal tasks are renamed in place) the rename
 * patches that task in place — the old behavior fell through to the full-save
 * INSERT and silently created a NEW draft row.
 */

function makeDeps(overrides?: Partial<SaveDraftDeps>): SaveDraftDeps {
  const project = { id: 'p1', task_type: 'prediction', task_id: '', use_msa: false } as unknown as Project;
  const inputConfig: ProjectInputConfig = {
    version: 1,
    components: [],
    constraints: [],
    properties: { affinity: false, target: null, ligand: null, binder: null },
    options: { seed: null }
  };
  const deps: SaveDraftDeps = {
    project,
    draft: {
      taskName: 'Renamed task',
      taskSummary: '',
      backend: 'boltz',
      use_msa: false,
      color_mode: 'default',
      inputConfig
    },
    workspaceTab: 'components',
    metadataOnlyDraftDirty: true,
    affinityLigandSmiles: '',
    affinityPreviewLigandSmiles: '',
    affinityTargetFile: null,
    affinityLigandFile: null,
    affinityCurrentUploads: { target: null, ligand: null } as AffinityPersistedUploads,
    proteinTemplates: {},
    customResidueLibrary: [] as CustomCcdMoleculeInput[],
    normalizeConfigForBackend: (config) => config,
    nonEmptyComponents: (components) => components,
    computeUseMsaFlag: () => false,
    createDraftFingerprint: () => 'fp',
    createComputationFingerprint: () => 'fp',
    createProteinTemplatesFingerprint: () => 'fp',
    createAffinityUploadsFingerprint: () => 'fp',
    buildAffinityUploadSnapshotComponents: async (components) => components,
    addTemplatesToTaskSnapshotComponents: (components) => components,
    persistDraftTaskSnapshot: vi.fn(async () => ({ id: 'new-draft-row' }) as ProjectTask),
    resolveEditableDraftTaskRowId: () => null, // viewing a finished task: no editable draft row
    resolveTerminalTaskRowId: () => null,
    patch: vi.fn(async (payload: Partial<Project>) => ({ ...project, ...payload }) as Project),
    patchTask: vi.fn(async (_taskRowId: string, payload: Partial<ProjectTask>) => ({ id: 'x', ...payload }) as ProjectTask),
    rememberTemplatesForTaskRow: vi.fn(),
    rememberAffinityUploadsForTaskRow: vi.fn(),
    setDraft: vi.fn(),
    setSavedDraftFingerprint: vi.fn(),
    setSavedComputationFingerprint: vi.fn(),
    setSavedTemplateFingerprint: vi.fn(),
    setSavedAffinityUploadsFingerprint: vi.fn(),
    setRunMenuOpen: vi.fn(),
    navigate: vi.fn(),
    ...overrides
  };
  return deps;
}

describe('saveProjectDraftFromWorkspace task isolation', () => {
  it('metadata-only save while viewing a COMPLETED task renames that task in place without creating a new one', async () => {
    const deps = makeDeps({
      resolveTerminalTaskRowId: () => 'finished-row'
    });
    await saveProjectDraftFromWorkspace(deps);

    expect(deps.patchTask).toHaveBeenCalledWith('finished-row', {
      name: 'Renamed task',
      summary: '',
    });
    expect(deps.persistDraftTaskSnapshot).not.toHaveBeenCalled();
    expect(deps.navigate).not.toHaveBeenCalled();
  });

  it('metadata-only save while viewing an ERRORED task renames that task in place without creating a new one', async () => {
    const deps = makeDeps({
      resolveTerminalTaskRowId: () => 'errored-row'
    });
    await saveProjectDraftFromWorkspace(deps);

    expect(deps.patchTask).toHaveBeenCalledWith('errored-row', {
      name: 'Renamed task',
      summary: '',
    });
    expect(deps.persistDraftTaskSnapshot).not.toHaveBeenCalled();
  });

  it('metadata-only save still renames the user’s own editable DRAFT row', async () => {
    const deps = makeDeps({
      resolveEditableDraftTaskRowId: () => 'my-draft-row'
    });
    await saveProjectDraftFromWorkspace(deps);

    expect(deps.patchTask).toHaveBeenCalledWith('my-draft-row', {
      name: 'Renamed task',
      summary: '',
    });
    expect(deps.persistDraftTaskSnapshot).not.toHaveBeenCalled();
  });

  it('metadata-only save with no draft row and no terminal task in view (e.g. RUNNING task) still falls through to the full save', async () => {
    const deps = makeDeps();
    await saveProjectDraftFromWorkspace(deps);

    expect(deps.patchTask).not.toHaveBeenCalled();
    expect(deps.persistDraftTaskSnapshot).toHaveBeenCalledTimes(1);
    const options = (deps.persistDraftTaskSnapshot as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect(options.reuseTaskRowId).toBeNull();
    expect(deps.draft.taskName).toBe('Renamed task');
  });
});
