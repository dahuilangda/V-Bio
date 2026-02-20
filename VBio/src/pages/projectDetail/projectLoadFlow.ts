import { getProjectById, getProjectTaskById, listProjectTasksCompact, listProjectTasksForList } from '../../api/supabaseLite';
import type { AffinityPersistedUploads } from '../../hooks/useAffinityWorkflow';
import type { Project, ProjectInputConfig, ProjectTask, ProteinTemplateUpload } from '../../types/models';
import { loadProjectInputConfig, loadProjectUiState } from '../../utils/projectInputs';
import { getWorkflowDefinition } from '../../utils/workflows';
import { resolveRestoredEditorState, resolveTaskSnapshotContext } from './projectLoadHelpers';
import {
  defaultConfigFromProject,
  mergeTaskSnapshotIntoConfig,
  readTaskAffinityUploads,
  readTaskProteinTemplates,
  resolveAffinityUploadStorageTaskRowId,
} from './projectTaskSnapshot';
import {
  createComputationFingerprint,
  createDraftFingerprint,
  createProteinTemplatesFingerprint,
  filterConstraintsByBackend,
  hasProteinTemplates,
  hasRecordData,
  sortProjectTasks,
} from './projectDraftUtils';

export interface LoadedDraftFields {
  taskName: string;
  taskSummary: string;
  backend: string;
  use_msa: boolean;
  color_mode: string;
  inputConfig: ProjectInputConfig;
}

export interface ProjectLoadFlowResult {
  project: Project;
  projectTasks: ProjectTask[];
  draft: LoadedDraftFields;
  savedDraftFingerprint: string;
  savedComputationFingerprint: string;
  savedTemplateFingerprint: string;
  proteinTemplates: Record<string, ProteinTemplateUpload>;
  taskProteinTemplates: Record<string, Record<string, ProteinTemplateUpload>>;
  taskAffinityUploads: Record<string, AffinityPersistedUploads>;
  activeConstraintId: string | null;
  selectedConstraintTemplateComponentId: string | null;
  suggestedWorkspaceTab: 'results' | 'components' | 'basics' | null;
}

export async function loadProjectFlow(params: {
  projectId: string;
  locationSearch: string;
  requestNewTask: boolean;
  sessionUserId?: string;
}): Promise<ProjectLoadFlowResult> {
  const { projectId, locationSearch, requestNewTask, sessionUserId } = params;

  const next = await getProjectById(projectId);
  if (!next || next.deleted_at) {
    throw new Error('Project not found or already deleted.');
  }
  if (sessionUserId && next.user_id !== sessionUserId) {
    throw new Error('You do not have permission to access this project.');
  }

  const activeTaskId = (next.task_id || '').trim();
  const workflowDef = getWorkflowDefinition(next.task_type);
  const taskRowsBase = sortProjectTasks(
    await (workflowDef.key === 'lead_optimization' ? listProjectTasksForList(next.id) : listProjectTasksCompact(next.id))
  );
  const query = new URLSearchParams(locationSearch);

  const {
    taskRows,
    activeTaskRow,
    requestedTaskRow,
    latestDraftTask,
    snapshotSourceTaskRow,
  } = await resolveTaskSnapshotContext({
    taskRowsBase,
    activeTaskId,
    locationSearch,
    requestNewTask,
    workflowKey: workflowDef.key,
    getProjectTaskById,
    sortProjectTasks,
  });

  const savedConfig = loadProjectInputConfig(next.id);
  const baseConfig = requestNewTask ? defaultConfigFromProject(next) : savedConfig || defaultConfigFromProject(next);
  const taskAlignedConfig = mergeTaskSnapshotIntoConfig(baseConfig, snapshotSourceTaskRow);
  const backendConstraints = filterConstraintsByBackend(taskAlignedConfig.constraints, next.backend);

  const savedUiState = loadProjectUiState(next.id);
  const loadedDraft: LoadedDraftFields = {
    taskName: String(snapshotSourceTaskRow?.name || '').trim(),
    taskSummary: String(snapshotSourceTaskRow?.summary || '').trim(),
    backend: next.backend,
    use_msa: next.use_msa,
    color_mode: next.color_mode || 'white',
    inputConfig: {
      ...taskAlignedConfig,
      constraints: backendConstraints,
    },
  };

  const {
    restoredTemplates,
    savedTaskTemplates,
    hydratedTaskAffinityUploads,
  } = resolveRestoredEditorState({
    requestNewTask,
    loadedComponents: loadedDraft.inputConfig.components,
    savedUiState,
    requestedTaskRow,
    activeTaskRow,
    latestDraftTask,
    snapshotSourceTaskRow,
    resolveAffinityUploadStorageTaskRowId,
    readTaskProteinTemplates,
    hasProteinTemplates,
    readTaskAffinityUploads,
  });

  const defaultContextTask = snapshotSourceTaskRow || requestedTaskRow || activeTaskRow;
  const contextHasResult = Boolean(
    String(defaultContextTask?.structure_name || '').trim() ||
      hasRecordData(defaultContextTask?.confidence) ||
      hasRecordData(defaultContextTask?.affinity)
  );
  const projectHasResult = Boolean(
    String(next.structure_name || '').trim() || hasRecordData(next.confidence) || hasRecordData(next.affinity)
  );

  let suggestedWorkspaceTab: 'results' | 'components' | 'basics' | null = null;
  if (!query.get('tab')) {
    if (requestNewTask && (workflowDef.key === 'prediction' || workflowDef.key === 'affinity')) {
      suggestedWorkspaceTab = 'components';
    } else if (workflowDef.key === 'prediction' || workflowDef.key === 'affinity') {
      suggestedWorkspaceTab = contextHasResult || projectHasResult ? 'results' : 'components';
    } else {
      suggestedWorkspaceTab = 'basics';
    }
  }

  return {
    project: next,
    projectTasks: taskRows,
    draft: loadedDraft,
    savedDraftFingerprint: createDraftFingerprint(loadedDraft),
    savedComputationFingerprint: createComputationFingerprint(loadedDraft),
    savedTemplateFingerprint: createProteinTemplatesFingerprint(restoredTemplates),
    proteinTemplates: restoredTemplates,
    taskProteinTemplates: savedTaskTemplates,
    taskAffinityUploads: hydratedTaskAffinityUploads,
    activeConstraintId: savedUiState?.activeConstraintId || null,
    selectedConstraintTemplateComponentId: savedUiState?.selectedConstraintTemplateComponentId || null,
    suggestedWorkspaceTab,
  };
}
