import type { CustomCcdMoleculeInput, InputComponent, Project, ProjectInputConfig, ProjectTask, ProteinTemplateUpload } from '../../types/models';
import type { AffinityPersistedUploads } from '../../hooks/useAffinityWorkflow';
import { extractPrimaryProteinAndLigand, saveProjectInputConfig } from '../../utils/projectInputs';
import { normalizeAffinityBackend } from '../apiAccessHelpers';
import { normalizeTaskSummary } from '../../utils/taskMetadata';
import { getWorkflowDefinition, isPredictionLikeWorkflowKey } from '../../utils/workflows';
import { mergeTaskInputOptionsIntoProperties } from './projectTaskSnapshot';

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
  customResidueLibrary: CustomCcdMoleculeInput[];
  normalizeConfigForBackend: (inputConfig: ProjectInputConfig, backend: string) => ProjectInputConfig;
  nonEmptyComponents: (components: InputComponent[]) => InputComponent[];
  computeUseMsaFlag: (components: InputComponent[], fallback?: boolean) => boolean;
  createDraftFingerprint: (draft: SaveDraftFields) => string;
  createComputationFingerprint: (draft: SaveDraftFields) => string;
  createProteinTemplatesFingerprint: (templates: Record<string, ProteinTemplateUpload>) => string;
  createAffinityUploadsFingerprint: (uploads: AffinityPersistedUploads) => string;
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
  resolveEditableDraftTaskRowId: () => string | null;
  resolveTerminalTaskRowId: () => string | null;
  patch: (payload: Partial<Project>) => Promise<Project | null>;
  patchTask: (taskRowId: string, payload: Partial<ProjectTask>) => Promise<ProjectTask | null>;
  rememberTemplatesForTaskRow: (taskRowId: string | null, templates: Record<string, ProteinTemplateUpload>) => void;
  rememberAffinityUploadsForTaskRow: (taskRowId: string | null, uploads: AffinityPersistedUploads) => void;
  setDraft: (value: SaveDraftFields) => void;
  setSavedDraftFingerprint: (value: string) => void;
  setSavedComputationFingerprint: (value: string) => void;
  setSavedTemplateFingerprint: (value: string) => void;
  setSavedAffinityUploadsFingerprint: (value: string) => void;
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
    normalizeConfigForBackend,
    nonEmptyComponents,
    computeUseMsaFlag,
    createDraftFingerprint,
    createComputationFingerprint,
    createProteinTemplatesFingerprint,
    createAffinityUploadsFingerprint,
    buildAffinityUploadSnapshotComponents,
    addTemplatesToTaskSnapshotComponents,
    persistDraftTaskSnapshot,
    resolveEditableDraftTaskRowId,
    resolveTerminalTaskRowId,
    patch,
    patchTask,
    rememberTemplatesForTaskRow,
    rememberAffinityUploadsForTaskRow,
    setDraft,
    setSavedDraftFingerprint,
    setSavedComputationFingerprint,
    setSavedTemplateFingerprint,
    setSavedAffinityUploadsFingerprint,
    setRunMenuOpen,
    navigate,
  } = deps;

  const workflowDef = getWorkflowDefinition(project.task_type);
  const persistedBackend = workflowDef.key === 'affinity' ? normalizeAffinityBackend(draft.backend) : draft.backend;
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
    taskSummary: normalizeTaskSummary(draft.taskSummary),
    backend: next.backend,
    use_msa: next.use_msa,
    color_mode: next.color_mode === 'alphafold' ? 'alphafold' : 'default',
    inputConfig: normalizedConfig,
  };

  if (metadataOnlyDraftDirty) {
    // Metadata-only saves rename exactly one existing row and never insert another:
    //  - the user's own editable DRAFT row, when one exists;
    //  - the COMPLETED or ERRORED task being viewed, otherwise (terminal tasks
    //    are renamed in place, never duplicated) — the rename patches that task
    //    in place, the
    //    same way the task list's inline rename does.
    const metadataTaskRowId = resolveEditableDraftTaskRowId() || resolveTerminalTaskRowId();
    if (metadataTaskRowId) {
      await patchTask(metadataTaskRowId, {
        name: nextDraft.taskName,
        summary: nextDraft.taskSummary,
      });
      setDraft(nextDraft);
      setSavedDraftFingerprint(createDraftFingerprint(nextDraft));
      setSavedComputationFingerprint(createComputationFingerprint(nextDraft));
      setSavedTemplateFingerprint(createProteinTemplatesFingerprint(proteinTemplates));
      setSavedAffinityUploadsFingerprint(createAffinityUploadsFingerprint(affinityCurrentUploads));
      setRunMenuOpen(false);
      return;
    }
    // No editable draft row and no terminal task in view (e.g. a RUNNING task):
    // fall through to the full-save INSERT, which creates a NEW draft carrying
    // the rename.
  }

  const reusableDraftTaskRowId = resolveEditableDraftTaskRowId();
  const normalizedConfigWithTaskOptions: ProjectInputConfig = {
    ...normalizedConfig,
    properties: mergeTaskInputOptionsIntoProperties(normalizedConfig.properties, normalizedConfig.options)
  };
  const snapshotComponents =
    workflowDef.key === 'affinity'
      ? await buildAffinityUploadSnapshotComponents(
          normalizedConfigWithTaskOptions.components,
          affinityTargetFile,
          affinityLigandFile,
          storedLigandSmiles
        )
      : addTemplatesToTaskSnapshotComponents(normalizedConfigWithTaskOptions.components, proteinTemplates);
  const draftTaskRow = await persistDraftTaskSnapshot(normalizedConfigWithTaskOptions, {
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
  setSavedAffinityUploadsFingerprint(createAffinityUploadsFingerprint(affinityCurrentUploads));
  setRunMenuOpen(false);
  // Docking (affinity) keeps its whole editor (target upload, ligand, preview,
  // run controls) on the Components tab; navigating to Basics after each save
  // hid the workspace the user was working in.
  const nextTab =
    workspaceTab === 'basics'
      ? 'basics'
      : isPredictionLikeWorkflowKey(workflowDef.key) || workflowDef.key === 'affinity'
        ? 'components'
        : 'basics';
  const query = new URLSearchParams({
    tab: nextTab,
    task_row_id: draftTaskRow.id,
  }).toString();
  navigate(`/projects/${next.id}?${query}`, { replace: true });
}
