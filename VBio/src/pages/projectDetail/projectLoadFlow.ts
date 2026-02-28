import { getProjectById, getProjectTaskById, listProjectTasksCompact, listProjectTasksForList } from '../../api/supabaseLite';
import type { AffinityPersistedUploads } from '../../hooks/useAffinityWorkflow';
import type { Project, ProjectInputConfig, ProjectTask, ProteinTemplateUpload } from '../../types/models';
import { loadProjectInputConfig, loadProjectUiState } from '../../utils/projectInputs';
import { getWorkflowDefinition, isPredictionLikeWorkflowKey } from '../../utils/workflows';
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

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function readText(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value);
}

function hasLeadOptResultPayload(value: unknown): boolean {
  const row = asRecord(value);
  const confidence = asRecord(row.confidence);
  const leadOptMmp = asRecord(confidence.lead_opt_mmp);
  const queryResult = asRecord(leadOptMmp.query_result);
  const leadOptMeta = asRecord(asRecord(row.properties).lead_opt_list);
  const metaPredictionSummary = asRecord(leadOptMeta.prediction_summary);

  const queryId =
    readText(leadOptMmp.query_id || queryResult.query_id).trim() ||
    readText(leadOptMeta.query_id).trim();
  if (queryId) return true;

  const enumeratedCandidates = Array.isArray(leadOptMmp.enumerated_candidates)
    ? leadOptMmp.enumerated_candidates
    : [];
  if (enumeratedCandidates.length > 0) return true;

  const predictionBySmiles = asRecord(leadOptMmp.prediction_by_smiles);
  if (Object.keys(predictionBySmiles).length > 0) return true;

  const candidateCount = Number(leadOptMeta.candidate_count);
  if (Number.isFinite(candidateCount) && candidateCount > 0) return true;

  const bucketCount = Number(leadOptMeta.bucket_count);
  if (Number.isFinite(bucketCount) && bucketCount > 0) return true;

  const metaCandidates = Array.isArray(leadOptMeta.enumerated_candidates)
    ? leadOptMeta.enumerated_candidates
    : [];
  if (metaCandidates.length > 0) return true;

  const metaPredictionBySmiles = asRecord(leadOptMeta.prediction_by_smiles);
  if (Object.keys(metaPredictionBySmiles).length > 0) return true;

  const predictionTotal = Number(metaPredictionSummary.total);
  if (Number.isFinite(predictionTotal) && predictionTotal > 0) return true;

  return false;
}

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
  const isPredictionLikeWorkflow = isPredictionLikeWorkflowKey(workflowDef.key);
  const normalizedBackend = workflowDef.key === 'affinity' ? 'boltz' : next.backend;
  const query = new URLSearchParams(locationSearch);
  const requestedTab = String(query.get('tab') || '').trim().toLowerCase();
  const requestedTaskRowId = String(query.get('task_row_id') || '').trim();
  const shouldIncludeTaskComponents =
    workflowDef.key === 'lead_optimization'
      ? requestNewTask || requestedTab === 'components' || requestedTab === 'constraints'
      : requestNewTask || requestedTab === 'components' || requestedTab === 'constraints' || !requestedTab;
  const shouldIncludeTaskConfidence =
    workflowDef.key === 'lead_optimization'
      ? false
      : workflowDef.key === 'peptide_design'
      ? requestedTab === 'results' || !requestedTab || Boolean(requestedTaskRowId)
      : true;
  const shouldIncludeTaskProperties = workflowDef.key === 'lead_optimization' ? false : true;
  const shouldUseTaskListView = workflowDef.key === 'lead_optimization' || workflowDef.key === 'peptide_design';
  const taskRowsBase = sortProjectTasks(
    await (shouldUseTaskListView
      ? listProjectTasksForList(next.id, {
          includeComponents: shouldIncludeTaskComponents,
          includeConfidence: shouldIncludeTaskConfidence,
          includeProperties: shouldIncludeTaskProperties,
          includeLeadOptSummary: workflowDef.key === 'lead_optimization'
        })
      : listProjectTasksCompact(next.id))
  );

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
  const backendConstraints = filterConstraintsByBackend(taskAlignedConfig.constraints, normalizedBackend);

  const savedUiState = loadProjectUiState(next.id);
  const loadedDraft: LoadedDraftFields = {
    taskName: String(snapshotSourceTaskRow?.name || '').trim(),
    taskSummary: String(snapshotSourceTaskRow?.summary || '').trim(),
    backend: normalizedBackend,
    use_msa: next.use_msa,
    color_mode: next.color_mode === 'alphafold' ? 'alphafold' : 'default',
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

  const contextHasLeadOptResult = hasLeadOptResultPayload(defaultContextTask);
  const projectHasLeadOptResult = hasLeadOptResultPayload(next);

  let suggestedWorkspaceTab: 'results' | 'components' | 'basics' | null = null;
  if (!query.get('tab')) {
    if (workflowDef.key === 'lead_optimization') {
      if (requestedTaskRowId) {
        suggestedWorkspaceTab = 'results';
      } else {
        suggestedWorkspaceTab =
          requestNewTask || (!contextHasLeadOptResult && !projectHasLeadOptResult)
            ? 'components'
            : 'results';
      }
    } else if (requestNewTask && (isPredictionLikeWorkflow || workflowDef.key === 'affinity')) {
      suggestedWorkspaceTab = 'components';
    } else if (isPredictionLikeWorkflow || workflowDef.key === 'affinity') {
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
