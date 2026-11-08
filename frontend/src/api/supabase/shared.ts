import { EffectiveAccessLevel, Project, ProjectAccessScope, ProjectTask, ShareAccessLevel } from '../../types/models';



// 项目/任务访问级别与行归一化辅助。


function normalizeProjectAccessScope(value: unknown): ProjectAccessScope {
  const token = String(value || '').trim().toLowerCase();
  if (token === 'project_share') return 'project_share';
  if (token === 'task_share') return 'task_share';
  return 'owner';
}

function normalizeShareAccessLevel(value: unknown): ShareAccessLevel {
  return String(value || '').trim().toLowerCase() === 'editor' ? 'editor' : 'viewer';
}

function normalizeEffectiveAccessLevel(value: unknown): EffectiveAccessLevel {
  const token = String(value || '').trim().toLowerCase();
  if (token === 'owner') return 'owner';
  if (token === 'editor') return 'editor';
  return 'viewer';
}

function toEffectiveAccessLevel(value: unknown): EffectiveAccessLevel {
  return normalizeShareAccessLevel(value) === 'editor' ? 'editor' : 'viewer';
}

function buildEditableTaskIdSet(taskIds: string[]): Set<string> {
  return new Set(taskIds.map((item) => String(item || '').trim()).filter(Boolean));
}

function resolveTaskAccessLevel(
  taskId: string,
  scope: ProjectAccessScope,
  accessLevel: EffectiveAccessLevel,
  editableTaskIdSet?: Set<string>
): EffectiveAccessLevel {
  if (scope === 'owner') return 'owner';
  if (editableTaskIdSet?.has(String(taskId || '').trim())) return 'editor';
  if (scope === 'project_share' && accessLevel === 'editor') return 'editor';
  return 'viewer';
}

function normalizeProjectRow(row: Partial<Project>): Project {
  const normalizedAccessibleTaskIds = Array.isArray(row.accessible_task_ids)
    ? Array.from(new Set(row.accessible_task_ids.map((item) => String(item || '').trim()).filter(Boolean)))
    : [];
  const normalizedEditableTaskIds = Array.isArray(row.editable_task_ids)
    ? Array.from(new Set(row.editable_task_ids.map((item) => String(item || '').trim()).filter(Boolean)))
    : [];
  return {
    ...row,
    protein_sequence: row.protein_sequence || '',
    ligand_smiles: row.ligand_smiles || '',
    confidence:
      row.confidence && typeof row.confidence === 'object' && !Array.isArray(row.confidence)
        ? row.confidence
        : {},
    affinity:
      row.affinity && typeof row.affinity === 'object' && !Array.isArray(row.affinity)
        ? row.affinity
        : {},
    access_scope: normalizeProjectAccessScope(row.access_scope),
    access_level: normalizeEffectiveAccessLevel(row.access_level || (String(row.access_scope || '').trim() === 'owner' ? 'owner' : 'viewer')),
    accessible_task_ids: normalizedAccessibleTaskIds,
    editable_task_ids: normalizedEditableTaskIds
  } as Project;
}

function applyTaskAccess(
  task: ProjectTask,
  scope: ProjectAccessScope,
  accessLevel: EffectiveAccessLevel = scope === 'owner' ? 'owner' : 'viewer',
  editableTaskIdSet?: Set<string>
): ProjectTask {
  return {
    ...task,
    access_scope: scope,
    access_level: resolveTaskAccessLevel(task.id, scope, accessLevel, editableTaskIdSet)
  };
}

export function sanitizeProjectForTaskShare(project: Project, tasks: ProjectTask[]): Project {
  const sanitizedTasks = tasks.filter((task) => String(task.project_id || '').trim() === String(project.id || '').trim());
  const activeTaskId = String(project.task_id || '').trim();
  const preferredTask =
    (activeTaskId
      ? sanitizedTasks.find((task) => String(task.task_id || '').trim() === activeTaskId) || null
      : null) ||
    sanitizedTasks[0] ||
    null;
  if (!preferredTask) {
    return {
      ...project,
      task_id: '',
      task_state: 'DRAFT',
      status_text: 'Shared task unavailable.',
      error_text: '',
      confidence: {},
      affinity: {},
      structure_name: ''
    };
  }
  return {
    ...project,
    task_id: preferredTask.task_id,
    task_state: preferredTask.task_state,
    status_text: preferredTask.status_text,
    error_text: preferredTask.error_text,
    submitted_at: preferredTask.submitted_at,
    completed_at: preferredTask.completed_at,
    duration_seconds: preferredTask.duration_seconds,
    confidence:
      preferredTask.confidence && typeof preferredTask.confidence === 'object' && !Array.isArray(preferredTask.confidence)
        ? preferredTask.confidence
        : {},
    affinity:
      preferredTask.affinity && typeof preferredTask.affinity === 'object' && !Array.isArray(preferredTask.affinity)
        ? preferredTask.affinity
        : {},
    structure_name: preferredTask.structure_name || ''
  };
}

// app_users: anon SELECT covers display columns only; user management beyond
// listUsersByIds lives in api/authServerApi.ts.

export { normalizeProjectAccessScope, normalizeShareAccessLevel, normalizeEffectiveAccessLevel, toEffectiveAccessLevel, buildEditableTaskIdSet, resolveTaskAccessLevel, normalizeProjectRow, applyTaskAccess };
