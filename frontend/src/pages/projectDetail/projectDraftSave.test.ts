import { describe, it, expect, vi } from 'vitest';
import { saveProjectDraftFromWorkspace, type SaveDraftDeps } from './projectDraftSave';
import type { CustomCcdMoleculeInput, Project, ProjectInputConfig, ProjectTask } from '../../types/models';
import type { AffinityPersistedUploads } from '../../hooks/useAffinityWorkflow';

/**
 * Regression: saving while VIEWING a finished task used to patch that task's
 * row (metadata-only saves renamed the finished task via the URL task_row_id).
 * A finished task is immutable — every save must land on the user's own
 * editable DRAFT row or create a new one.
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
  it('metadata-only save while viewing a finished task creates a NEW draft instead of patching it', async () => {
    const deps = makeDeps();
    await saveProjectDraftFromWorkspace(deps);

    expect(deps.patchTask).not.toHaveBeenCalled();
    expect(deps.persistDraftTaskSnapshot).toHaveBeenCalledTimes(1);
    const options = (deps.persistDraftTaskSnapshot as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect(options.reuseTaskRowId).toBeNull();
    expect(deps.draft.taskName).toBe('Renamed task');
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
});
