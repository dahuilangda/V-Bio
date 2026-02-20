import type { AffinityPersistedUploads } from '../../hooks/useAffinityWorkflow';
import type { InputComponent, ProjectTask, ProteinTemplateUpload } from '../../types/models';

interface SavedUiStateLike {
  proteinTemplates?: Record<string, ProteinTemplateUpload>;
  taskProteinTemplates?: Record<string, Record<string, ProteinTemplateUpload>>;
  taskAffinityUploads?: Record<string, unknown>;
}

export async function resolveTaskSnapshotContext(params: {
  taskRowsBase: ProjectTask[];
  activeTaskId: string;
  locationSearch: string;
  requestNewTask: boolean;
  workflowKey: string;
  getProjectTaskById: (taskRowId: string) => Promise<ProjectTask | null>;
  sortProjectTasks: (rows: ProjectTask[]) => ProjectTask[];
}): Promise<{
  taskRows: ProjectTask[];
  activeTaskRow: ProjectTask | null;
  requestedTaskRow: ProjectTask | null;
  latestDraftTask: ProjectTask | null;
  snapshotSourceTaskRow: ProjectTask | null;
}> {
  const { taskRowsBase, activeTaskId, locationSearch, requestNewTask, workflowKey, getProjectTaskById, sortProjectTasks } = params;
  const activeTaskRow =
    activeTaskId.length > 0
      ? taskRowsBase.find((item) => String(item.task_id || '').trim() === activeTaskId) || null
      : null;

  const query = new URLSearchParams(locationSearch);
  const requestedTaskRowId = query.get('task_row_id');
  const requestedTaskRow =
    requestedTaskRowId && requestedTaskRowId.trim()
      ? taskRowsBase.find((item) => String(item.id || '').trim() === requestedTaskRowId.trim()) || null
      : null;

  const snapshotSourceTaskRowBase = requestNewTask ? null : requestedTaskRow || activeTaskRow;
  const latestDraftTask = requestNewTask
    ? null
    : (() => {
        if (requestedTaskRow && requestedTaskRow.task_state === 'DRAFT' && !String(requestedTaskRow.task_id || '').trim()) {
          return requestedTaskRow;
        }
        return taskRowsBase.find((item) => item.task_state === 'DRAFT' && !String(item.task_id || '').trim()) || null;
      })();

  const snapshotTaskRowId = snapshotSourceTaskRowBase?.id || latestDraftTask?.id || null;
  const shouldLoadSnapshotDetail = Boolean(
    snapshotTaskRowId &&
      (workflowKey === 'prediction' || workflowKey === 'affinity' || workflowKey === 'lead_optimization')
  );
  const snapshotSourceTaskRowDetail = shouldLoadSnapshotDetail && snapshotTaskRowId
    ? await getProjectTaskById(snapshotTaskRowId)
    : null;
  const snapshotSourceTaskRow = snapshotSourceTaskRowDetail || snapshotSourceTaskRowBase;

  const taskRows = sortProjectTasks(
    taskRowsBase.map((item) =>
      snapshotSourceTaskRowDetail && item.id === snapshotSourceTaskRowDetail.id ? snapshotSourceTaskRowDetail : item
    )
  );

  return {
    taskRows,
    activeTaskRow,
    requestedTaskRow,
    latestDraftTask,
    snapshotSourceTaskRow,
  };
}

export function resolveRestoredEditorState(params: {
  requestNewTask: boolean;
  loadedComponents: InputComponent[];
  savedUiState: SavedUiStateLike | null | undefined;
  requestedTaskRow: ProjectTask | null;
  activeTaskRow: ProjectTask | null;
  latestDraftTask: ProjectTask | null;
  snapshotSourceTaskRow: ProjectTask | null;
  resolveAffinityUploadStorageTaskRowId: (taskRowId: string | null | undefined) => string | null;
  readTaskProteinTemplates: (task: ProjectTask | null) => Record<string, ProteinTemplateUpload>;
  hasProteinTemplates: (templates: Record<string, ProteinTemplateUpload> | null | undefined) => boolean;
  readTaskAffinityUploads: (task: ProjectTask | null) => AffinityPersistedUploads;
}): {
  restoredTemplates: Record<string, ProteinTemplateUpload>;
  savedTaskTemplates: Record<string, Record<string, ProteinTemplateUpload>>;
  hydratedTaskAffinityUploads: Record<string, AffinityPersistedUploads>;
} {
  const {
    requestNewTask,
    loadedComponents,
    savedUiState,
    requestedTaskRow,
    activeTaskRow,
    latestDraftTask,
    snapshotSourceTaskRow,
    resolveAffinityUploadStorageTaskRowId,
    readTaskProteinTemplates,
    hasProteinTemplates,
    readTaskAffinityUploads,
  } = params;

  const validProteinIds = new Set(
    loadedComponents.filter((component) => component.type === 'protein').map((component) => component.id)
  );

  const savedTaskTemplates = savedUiState?.taskProteinTemplates || {};
  const readSavedTaskTemplates = (task: ProjectTask | null) => {
    if (!task) return {};
    return savedTaskTemplates[task.id] || {};
  };

  const templateSource = (() => {
    if (requestNewTask) {
      return {};
    }
    if (requestedTaskRow) {
      const requestedEmbedded = readTaskProteinTemplates(requestedTaskRow);
      if (hasProteinTemplates(requestedEmbedded)) return requestedEmbedded;
      const requestedCached = readSavedTaskTemplates(requestedTaskRow);
      if (hasProteinTemplates(requestedCached)) return requestedCached;
      return {};
    }

    if (activeTaskRow) {
      const activeEmbedded = readTaskProteinTemplates(activeTaskRow);
      if (hasProteinTemplates(activeEmbedded)) return activeEmbedded;
      const activeCached = readSavedTaskTemplates(activeTaskRow);
      if (hasProteinTemplates(activeCached)) return activeCached;
      return {};
    }

    if (latestDraftTask) {
      const latestDraftEmbedded = readTaskProteinTemplates(latestDraftTask);
      if (hasProteinTemplates(latestDraftEmbedded)) return latestDraftEmbedded;
      const latestDraftCached = readSavedTaskTemplates(latestDraftTask);
      if (hasProteinTemplates(latestDraftCached)) return latestDraftCached;
      return {};
    }

    return savedUiState?.proteinTemplates || {};
  })();

  const restoredTemplates = Object.fromEntries(
    Object.entries(templateSource).filter(([componentId]) => validProteinIds.has(componentId))
  ) as Record<string, ProteinTemplateUpload>;

  const restoredTaskAffinityUploadsRaw = savedUiState?.taskAffinityUploads || {};
  const restoredTaskAffinityUploads: Record<string, AffinityPersistedUploads> = {};
  for (const [scopeKey, uploads] of Object.entries(restoredTaskAffinityUploadsRaw as Record<string, unknown>)) {
    if (scopeKey === '__legacy__') {
      restoredTaskAffinityUploads.__legacy__ = uploads as AffinityPersistedUploads;
      continue;
    }
    const storageTaskRowId = resolveAffinityUploadStorageTaskRowId(scopeKey);
    if (!storageTaskRowId) continue;
    restoredTaskAffinityUploads[storageTaskRowId] = uploads as AffinityPersistedUploads;
  }

  const affinityContextTaskRowId =
    snapshotSourceTaskRow?.id || requestedTaskRow?.id || activeTaskRow?.id || latestDraftTask?.id || null;
  const restoredAffinityUploadsFromLegacyScope = restoredTaskAffinityUploads.__legacy__ || null;
  const restoredAffinityUploadsFromTask = readTaskAffinityUploads(snapshotSourceTaskRow);
  const restoredAffinityUploadsFromTaskScope = affinityContextTaskRowId
    ? restoredTaskAffinityUploads[affinityContextTaskRowId] || null
    : null;

  const restoredAffinityUploads: AffinityPersistedUploads = {
    target:
      restoredAffinityUploadsFromTaskScope?.target ||
      restoredAffinityUploadsFromTask.target ||
      (!affinityContextTaskRowId ? restoredAffinityUploadsFromLegacyScope?.target : null),
    ligand:
      restoredAffinityUploadsFromTaskScope?.ligand ||
      restoredAffinityUploadsFromTask.ligand ||
      (!affinityContextTaskRowId ? restoredAffinityUploadsFromLegacyScope?.ligand : null),
  };

  const hydratedTaskAffinityUploads = { ...restoredTaskAffinityUploads };
  if (affinityContextTaskRowId && (restoredAffinityUploads.target || restoredAffinityUploads.ligand)) {
    hydratedTaskAffinityUploads[affinityContextTaskRowId] = restoredAffinityUploads;
  }

  return {
    restoredTemplates,
    savedTaskTemplates,
    hydratedTaskAffinityUploads,
  };
}
