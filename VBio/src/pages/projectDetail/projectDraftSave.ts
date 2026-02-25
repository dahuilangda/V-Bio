import type { InputComponent, Project, ProjectInputConfig, ProjectTask, ProteinTemplateUpload } from '../../types/models';
import type { AffinityPersistedUploads } from '../../hooks/useAffinityWorkflow';
import { extractPrimaryProteinAndLigand, saveProjectInputConfig } from '../../utils/projectInputs';
import { getWorkflowDefinition } from '../../utils/workflows';

export interface SaveDraftFields {
  taskName: string;
  taskSummary: string;
  backend: string;
  use_msa: boolean;
  color_mode: string;
  inputConfig: ProjectInputConfig;
}

export interface SaveDraftDeps {
  project: Project;
  draft: SaveDraftFields;
  workspaceTab: 'results' | 'basics' | 'components' | 'constraints';
  metadataOnlyDraftDirty: boolean;
  affinityLigandSmiles: string;
  affinityPreviewLigandSmiles: string;
  affinityTargetFile: File | null;
  affinityLigandFile: File | null;
  affinityCurrentUploads: AffinityPersistedUploads;
  proteinTemplates: Record<string, ProteinTemplateUpload>;
  requestedStatusTaskRowId: string | null;
  activeStatusTaskRowId: string | null;
  normalizeConfigForBackend: (inputConfig: ProjectInputConfig, backend: string) => ProjectInputConfig;
  nonEmptyComponents: (components: InputComponent[]) => InputComponent[];
  computeUseMsaFlag: (components: InputComponent[], fallback?: boolean) => boolean;
  createDraftFingerprint: (draft: SaveDraftFields) => string;
  createComputationFingerprint: (draft: SaveDraftFields) => string;
  createProteinTemplatesFingerprint: (templates: Record<string, ProteinTemplateUpload>) => string;
  buildAffinityUploadSnapshotComponents: (
    baseComponents: InputComponent[],
    targetFile: File | null,
    ligandFile: File | null,
    ligandSmiles?: string
  ) => Promise<InputComponent[]>;
  addTemplatesToTaskSnapshotComponents: (
    components: InputComponent[],
    templates: Record<string, ProteinTemplateUpload>
  ) => InputComponent[];
  persistDraftTaskSnapshot: (
    normalizedConfig: ProjectInputConfig,
    options?: {
      statusText?: string;
      reuseTaskRowId?: string | null;
      snapshotComponents?: InputComponent[];
      proteinSequenceOverride?: string;
      ligandSmilesOverride?: string;
    }
  ) => Promise<ProjectTask>;
  resolveEditableDraftTaskRowId: (options?: { allowLatestDraftFallback?: boolean }) => string | null;
  resolveRuntimeTaskRowId: () => string | null;
  patch: (payload: Partial<Project>) => Promise<Project | null>;
  patchTask: (taskRowId: string, payload: Partial<ProjectTask>) => Promise<ProjectTask | null>;
  rememberTemplatesForTaskRow: (taskRowId: string | null, templates: Record<string, ProteinTemplateUpload>) => void;
  rememberAffinityUploadsForTaskRow: (taskRowId: string | null, uploads: AffinityPersistedUploads) => void;
  setDraft: (value: SaveDraftFields) => void;
  setSavedDraftFingerprint: (value: string) => void;
  setSavedComputationFingerprint: (value: string) => void;
  setSavedTemplateFingerprint: (value: string) => void;
  setRunMenuOpen: (value: boolean) => void;
  navigate: (path: string, options?: { replace?: boolean }) => void;
}

export async function saveProjectDraftFromWorkspace(deps: SaveDraftDeps): Promise<void> {
  const {
    project,
    draft,
    workspaceTab,
    metadataOnlyDraftDirty,
    affinityLigandSmiles,
    affinityPreviewLigandSmiles,
    affinityTargetFile,
    affinityLigandFile,
    affinityCurrentUploads,
    proteinTemplates,
    requestedStatusTaskRowId,
    activeStatusTaskRowId,
    normalizeConfigForBackend,
    nonEmptyComponents,
    computeUseMsaFlag,
    createDraftFingerprint,
    createComputationFingerprint,
    createProteinTemplatesFingerprint,
    buildAffinityUploadSnapshotComponents,
    addTemplatesToTaskSnapshotComponents,
    persistDraftTaskSnapshot,
    resolveEditableDraftTaskRowId,
    resolveRuntimeTaskRowId,
    patch,
    patchTask,
    rememberTemplatesForTaskRow,
    rememberAffinityUploadsForTaskRow,
    setDraft,
    setSavedDraftFingerprint,
    setSavedComputationFingerprint,
    setSavedTemplateFingerprint,
    setRunMenuOpen,
    navigate,
  } = deps;

  const workflowDef = getWorkflowDefinition(project.task_type);
  const persistedBackend = workflowDef.key === 'affinity' ? 'boltz' : draft.backend;
  const normalizedConfig = normalizeConfigForBackend(draft.inputConfig, persistedBackend);
  const activeComponents = nonEmptyComponents(normalizedConfig.components);
  const { proteinSequence, ligandSmiles } = extractPrimaryProteinAndLigand(normalizedConfig);
  const msaComponents = workflowDef.key === 'affinity' ? normalizedConfig.components : activeComponents;
  const hasMsa = computeUseMsaFlag(msaComponents, draft.use_msa);
  const storedProteinSequence = workflowDef.key === 'affinity' ? '' : proteinSequence;
  const storedLigandSmiles =
    workflowDef.key === 'affinity'
      ? affinityLigandSmiles.trim() || affinityPreviewLigandSmiles.trim() || ligandSmiles
      : ligandSmiles;

  const projectPatch: Partial<Project> = {
    backend: persistedBackend,
    use_msa: hasMsa,
    color_mode: draft.color_mode,
    status_text: 'Draft saved',
    protein_sequence: storedProteinSequence,
    ligand_smiles: storedLigandSmiles,
  };
  const next = await patch(projectPatch);

  if (!next) return;

  saveProjectInputConfig(next.id, normalizedConfig);
  const nextDraft: SaveDraftFields = {
    taskName: draft.taskName.trim(),
    taskSummary: draft.taskSummary.trim(),
    backend: next.backend,
    use_msa: next.use_msa,
    color_mode: next.color_mode === 'alphafold' ? 'alphafold' : 'default',
    inputConfig: normalizedConfig,
  };

  if (workspaceTab === 'basics' || metadataOnlyDraftDirty) {
    const metadataTaskRowId =
      requestedStatusTaskRowId ||
      activeStatusTaskRowId ||
      resolveRuntimeTaskRowId() ||
      resolveEditableDraftTaskRowId({ allowLatestDraftFallback: false });
    if (metadataTaskRowId) {
      await patchTask(metadataTaskRowId, {
        name: nextDraft.taskName,
        summary: nextDraft.taskSummary,
      });
    }
    setDraft(nextDraft);
    setSavedDraftFingerprint(createDraftFingerprint(nextDraft));
    setSavedComputationFingerprint(createComputationFingerprint(nextDraft));
    setSavedTemplateFingerprint(createProteinTemplatesFingerprint(proteinTemplates));
    setRunMenuOpen(false);
    return;
  }

  const reusableDraftTaskRowId = resolveEditableDraftTaskRowId({
    allowLatestDraftFallback: workflowDef.key !== 'affinity',
  });
  const snapshotComponents =
    workflowDef.key === 'affinity'
      ? await buildAffinityUploadSnapshotComponents(
          normalizedConfig.components,
          affinityTargetFile,
          affinityLigandFile,
          storedLigandSmiles
        )
      : addTemplatesToTaskSnapshotComponents(normalizedConfig.components, proteinTemplates);
  const draftTaskRow = await persistDraftTaskSnapshot(normalizedConfig, {
    statusText: 'Draft saved (not submitted)',
    reuseTaskRowId: reusableDraftTaskRowId,
    snapshotComponents,
    proteinSequenceOverride: storedProteinSequence,
    ligandSmilesOverride: storedLigandSmiles,
  });
  rememberTemplatesForTaskRow(draftTaskRow.id, proteinTemplates);
  if (workflowDef.key === 'affinity') {
    rememberAffinityUploadsForTaskRow(draftTaskRow.id, affinityCurrentUploads);
  }
  setDraft(nextDraft);
  setSavedDraftFingerprint(createDraftFingerprint(nextDraft));
  setSavedComputationFingerprint(createComputationFingerprint(nextDraft));
  setSavedTemplateFingerprint(createProteinTemplatesFingerprint(proteinTemplates));
  setRunMenuOpen(false);
  const nextTab = workflowDef.key === 'prediction' ? 'components' : 'basics';
  const query = new URLSearchParams({
    tab: nextTab,
    task_row_id: draftTaskRow.id,
  }).toString();
  navigate(`/projects/${next.id}?${query}`, { replace: true });
}
