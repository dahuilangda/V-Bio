import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import type { MutableRefObject } from 'react';
import { submitAffinityTaskFromDraft, type AffinitySubmitDeps, type AffinityDraftFields } from './affinitySubmission';
import type { AffinityPreviewPayload, Project, ProjectTask } from '../../types/models';

interface FetchCall {
  url: string;
  body: FormData | null;
}

let fetchCalls: FetchCall[] = [];
const fetchStub = vi.fn(async (url: string, init: RequestInit = {}) => {
  fetchCalls.push({ url: String(url), body: (init.body as FormData) || null });
  return { ok: true, json: async () => ({ task_id: 'task-123' }) } as unknown as Response;
});

function buildDraft(affinityMode: string, dockPocket?: Record<string, unknown> | null): AffinityDraftFields {
  return {
    taskName: 'T',
    taskSummary: 'S',
    backend: 'boltz',
    use_msa: true,
    color_mode: 'default',
    inputConfig: {
      components: [],
      constraints: null,
      properties: {},
      options: {
        affinityMode,
        ...(dockPocket ? { affinityDockPocket: dockPocket } : {}),
        seed: 42
      }
    }
  } as unknown as AffinityDraftFields;
}

function buildPreview(): AffinityPreviewPayload {
  return {
    structureText: 'data_test\n#',
    structureFormat: 'cif',
    structureName: 'input.cif',
    targetStructureText: 'data_test',
    targetStructureFormat: 'cif',
    ligandStructureText: '',
    ligandStructureFormat: 'cif',
    ligandSmiles: '',
    targetChainIds: ['A'],
    hasLigand: false,
    ligandIsSmallMolecule: false,
    supportsActivity: false,
    ligandChainId: ''
  } as unknown as AffinityPreviewPayload;
}

function buildDeps(overrides: Partial<AffinitySubmitDeps> = {}): AffinitySubmitDeps {
  const noop = () => {};
  const errors: string[] = [];
  const deps: AffinitySubmitDeps = {
    project: { id: 'p1' } as unknown as Project,
    draft: buildDraft('dock'),
    affinityTargetFile: new File(['pdb text'], 'target.pdb', { type: 'text/plain' }),
    affinityLigandFile: null,
    affinityPreviewLoading: false,
    affinityPreviewCurrent: true,
    affinityPreview: buildPreview(),
    affinityPreviewError: null,
    affinityTargetChainIds: ['A'],
    affinityLigandChainId: '',
    affinityLigandSmiles: 'CCO',
    affinityHasLigand: false,
    affinitySupportsActivity: false,
    affinityConfidenceOnly: false,
    affinityCurrentUploads: { target: null, ligand: null },
    proteinTemplates: {},
    submitInFlightRef: { current: false } as MutableRefObject<boolean>,
    runRedirectTimerRef: { current: null } as MutableRefObject<number | null>,
    runSuccessNoticeTimerRef: { current: null } as MutableRefObject<number | null>,
    setSubmitting: noop,
    setError: (v) => { if (v !== null) errors.push(String(v)); },
    setRunRedirectTaskId: noop,
    setRunSuccessNotice: noop,
    setDraft: noop,
    setSavedDraftFingerprint: noop,
    setSavedComputationFingerprint: noop,
    setSavedTemplateFingerprint: noop,
    setSavedAffinityUploadsFingerprint: noop,
    setRunMenuOpen: noop,
    syncWorkspaceTaskRow: noop,
    setProjectTasks: noop,
    setProject: noop,
    setStatusInfo: noop,
    showRunQueuedNotice: noop,
    normalizeConfigForBackend: (c) => c,
    computeUseMsaFlag: () => true,
    createDraftFingerprint: () => 'fp',
    createComputationFingerprint: () => 'fp',
    createProteinTemplatesFingerprint: () => 'fp',
    createAffinityUploadsFingerprint: () => 'fp',
    buildAffinityUploadSnapshotComponents: async (c) => c,
    persistDraftTaskSnapshot: async () => ({ id: 'local-1' }) as unknown as ProjectTask,
    resolveEditableDraftTaskRowId: () => null,
    rememberAffinityUploadsForTaskRow: noop,
    patch: async () => null,
    patchTask: async () => ({ id: 'local-1' }) as unknown as ProjectTask,
    updateProjectTask: async (_id, payload) => payload as ProjectTask,
    findProjectTaskByTaskId: async () => null,
    deleteProjectTask: async () => {},
    sortProjectTasks: (rows) => rows,
    saveProjectInputConfig: noop,
    ...overrides
  };
  return Object.assign(deps, { __errors: errors } as object) as AffinitySubmitDeps & { __errors: string[] };
}

const POCKET = {
  centerX: 1,
  centerY: 2,
  centerZ: 3,
  sizeX: 22,
  sizeY: 22,
  sizeZ: 22,
  method: 'manual'
};

describe('submitAffinityTaskFromDraft — dock mode validation', () => {
  beforeEach(() => {
    fetchCalls = [];
    vi.stubGlobal('fetch', fetchStub);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('rejects dock without a ligand SMILES', async () => {
    const deps = buildDeps({ affinityLigandSmiles: '  ' });
    // Validation gates now THROW (not silent-return): programmatic callers (Copilot submit)
    // get an honest rejection instead of a false "queued" resolution.
    await expect(submitAffinityTaskFromDraft(deps)).rejects.toThrow('Dock mode requires a ligand SMILES');
    expect((deps as any).__errors[0]).toContain('Dock mode requires a ligand SMILES');
    expect(fetchCalls).toHaveLength(0);
  });

  it('rejects dock without a pocket box', async () => {
    const deps = buildDeps({ draft: buildDraft('dock', null) });
    await submitAffinityTaskFromDraft(deps);
    expect((deps as any).__errors[0]).toContain('pocket box');
    expect(fetchCalls).toHaveLength(0);
  });

  it('submits dock with smiles + pocket and posts dock fields to the API', async () => {
    const deps = buildDeps({ draft: buildDraft('dock', POCKET) });
    await submitAffinityTaskFromDraft(deps);
    expect((deps as any).__errors).toHaveLength(0);
    const submitCall = fetchCalls.find((c) => c.url.includes('/api/boltz2score'));
    expect(submitCall).toBeDefined();
    const form = submitCall!.body!;
    // The management gateway rejects submits without a form project_id (403) — pin it.
    expect(form.get('project_id')).toBe('p1');
    expect(form.get('mode')).toBe('dock');
    expect(form.get('ligand_smiles')).toBe('CCO');
    expect(form.get('center_x')).toBe('1');
    expect(form.get('center_y')).toBe('2');
    expect(form.get('center_z')).toBe('3');
    expect(form.get('size_x')).toBe('22');
    expect(form.get('enable_affinity')).toBe('true');
    expect(form.get('ligand_chain')).toBe('L');
    expect(form.get('target_chain')).toBe('A');
  });

  it('keeps the legacy pose-mode requirement on uploaded ligand files', async () => {
    const deps = buildDeps({ draft: buildDraft('pose', null) });
    await expect(submitAffinityTaskFromDraft(deps)).rejects.toThrow('Pose/refine/interface modes require uploaded target and ligand files');
    expect((deps as any).__errors[0]).toContain('Pose/refine/interface modes require uploaded target and ligand files');
    expect(fetchCalls).toHaveLength(0);
  });

  it('falls back to dock mode for unknown persisted modes', async () => {
    const deps = buildDeps({ draft: buildDraft('nonsense', null) });
    await submitAffinityTaskFromDraft(deps);
    // Falls back to dock (the new default); dock requires SMILES + pocket,
    // so the submission is rejected with a clear error.
    const submitCall = fetchCalls.find((c) => c.url.includes('/api/boltz2score'));
    expect(submitCall).toBeUndefined();  // blocked by validation
    expect((deps as any).__errors[0]).toContain('Dock mode requires');
  });
});
