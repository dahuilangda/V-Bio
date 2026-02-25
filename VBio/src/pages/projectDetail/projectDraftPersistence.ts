import type { Dispatch, SetStateAction } from 'react';
import type { InputComponent, Project, ProjectInputConfig, ProjectTask } from '../../types/models';
import { extractPrimaryProteinAndLigand } from '../../utils/projectInputs';
import { getWorkflowDefinition } from '../../utils/workflows';

export interface DraftSnapshotSource {
  taskName: string;
  taskSummary: string;
  backend: string;
}

export async function patchProjectRecord(params: {
  project: Project | null;
  payload: Partial<Project>;
  updateProject: (projectId: string, payload: Partial<Project>) => Promise<Project>;
  setProject: Dispatch<SetStateAction<Project | null>>;
}): Promise<Project | null> {
  const { project, payload, updateProject, setProject } = params;
  if (!project) return null;
  const next = await updateProject(project.id, payload);
  setProject(next);
  return next;
}

export async function patchTaskRecord(params: {
  taskRowId: string;
  payload: Partial<ProjectTask>;
  updateProjectTask: (taskRowId: string, payload: Partial<ProjectTask>) => Promise<ProjectTask>;
  setProjectTasks: Dispatch<SetStateAction<ProjectTask[]>>;
  sortProjectTasks: (rows: ProjectTask[]) => ProjectTask[];
}): Promise<ProjectTask | null> {
  const { taskRowId, payload, updateProjectTask, setProjectTasks, sortProjectTasks } = params;
  if (taskRowId.startsWith('local-')) {
    setProjectTasks((prev) =>
      sortProjectTasks(
        prev.map((row) =>
          row.id === taskRowId
            ? {
                ...row,
                ...payload,
                updated_at: new Date().toISOString(),
              }
            : row
        )
      )
    );
    return null;
  }
  const next = await updateProjectTask(taskRowId, payload);
  setProjectTasks((prev) => sortProjectTasks(prev.map((row) => (row.id === taskRowId ? next : row))));
  return next;
}

export function resolveEditableDraftTaskRowIdFromContext(params: {
  requestNewTask: boolean;
  locationSearch: string;
  project: Project | null;
  projectTasks: ProjectTask[];
  isDraftTaskSnapshot: (task: ProjectTask | null) => boolean;
  allowLatestDraftFallback?: boolean;
}): string | null {
  const { requestNewTask, locationSearch, project, projectTasks, isDraftTaskSnapshot } = params;
  const allowLatestDraftFallback = params.allowLatestDraftFallback !== false;
  if (requestNewTask) return null;

  const requestedTaskRowId = new URLSearchParams(locationSearch).get('task_row_id');
  if (requestedTaskRowId && requestedTaskRowId.trim()) {
    const requested = projectTasks.find((item) => String(item.id || '').trim() === requestedTaskRowId.trim()) || null;
    if (requested) {
      return isDraftTaskSnapshot(requested) ? requested.id : null;
    }
    return null;
  }

  const activeTaskId = String(project?.task_id || '').trim();
  if (activeTaskId) {
    const activeRow = projectTasks.find((item) => String(item.task_id || '').trim() === activeTaskId) || null;
    if (activeRow) {
      return isDraftTaskSnapshot(activeRow) ? activeRow.id : null;
    }
    return null;
  }

  if (!allowLatestDraftFallback) return null;
  const latestDraft = projectTasks.find((item) => isDraftTaskSnapshot(item)) || null;
  return latestDraft ? latestDraft.id : null;
}

export function resolveRuntimeTaskRowIdFromContext(params: {
  project: Project | null;
  projectTasks: ProjectTask[];
}): string | null {
  const { project, projectTasks } = params;
  const activeTaskId = String(project?.task_id || '').trim();
  if (!activeTaskId) return null;
  const runtimeRow = projectTasks.find((item) => String(item.task_id || '').trim() === activeTaskId) || null;
  return runtimeRow?.id || null;
}

export async function persistDraftTaskSnapshotRecord(params: {
  project: Project | null;
  draft: DraftSnapshotSource | null;
  normalizedConfig: ProjectInputConfig;
  options?: {
    statusText?: string;
    reuseTaskRowId?: string | null;
    snapshotComponents?: InputComponent[];
    proteinSequenceOverride?: string;
    ligandSmilesOverride?: string;
  };
  insertProjectTask: (payload: Partial<ProjectTask>) => Promise<ProjectTask>;
  updateProjectTask: (taskRowId: string, payload: Partial<ProjectTask>) => Promise<ProjectTask>;
  setProjectTasks: Dispatch<SetStateAction<ProjectTask[]>>;
  sortProjectTasks: (rows: ProjectTask[]) => ProjectTask[];
}): Promise<ProjectTask> {
  const {
    project,
    draft,
    normalizedConfig,
    options,
    insertProjectTask,
    updateProjectTask,
    setProjectTasks,
    sortProjectTasks,
  } = params;

  if (!project || !draft) {
    throw new Error('Project context is not ready.');
  }

  const { proteinSequence, ligandSmiles } = extractPrimaryProteinAndLigand(normalizedConfig);
  const statusText = options?.statusText || 'Draft saved (not submitted)';
  const snapshotComponents =
    Array.isArray(options?.snapshotComponents) && options.snapshotComponents.length > 0
      ? options.snapshotComponents
      : normalizedConfig.components;
  const storedProteinSequence =
    typeof options?.proteinSequenceOverride === 'string' ? options.proteinSequenceOverride : proteinSequence;
  const storedLigandSmiles = typeof options?.ligandSmilesOverride === 'string' ? options.ligandSmilesOverride : ligandSmiles;
  const effectiveBackend = getWorkflowDefinition(project.task_type).key === 'affinity' ? 'boltz' : draft.backend;

  const basePayload: Partial<ProjectTask> = {
    project_id: project.id,
    name: draft.taskName.trim(),
    summary: draft.taskSummary.trim(),
    task_id: '',
    task_state: 'DRAFT',
    status_text: statusText,
    error_text: '',
    backend: effectiveBackend,
    seed: normalizedConfig.options.seed ?? null,
    protein_sequence: storedProteinSequence,
    ligand_smiles: storedLigandSmiles,
    components: snapshotComponents,
    constraints: normalizedConfig.constraints,
    properties: normalizedConfig.properties,
    confidence: {},
    affinity: {},
    structure_name: '',
    submitted_at: null,
    completed_at: null,
    duration_seconds: null,
  };

  const reuseTaskRowId = options?.reuseTaskRowId || null;
  if (reuseTaskRowId) {
    if (!reuseTaskRowId.startsWith('local-')) {
      try {
        const updated = await updateProjectTask(reuseTaskRowId, basePayload);
        setProjectTasks((prev) => {
          const exists = prev.some((item) => item.id === reuseTaskRowId);
          const next = exists ? prev.map((item) => (item.id === reuseTaskRowId ? updated : item)) : [updated, ...prev];
          return sortProjectTasks(next);
        });
        return updated;
      } catch {
        // Fall through to insert path.
      }
    }
  }

  const inserted = await insertProjectTask(basePayload);
  setProjectTasks((prev) => sortProjectTasks([inserted, ...prev.filter((row) => row.id !== inserted.id)]));
  return inserted;
}
