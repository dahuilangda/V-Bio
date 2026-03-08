import type { EffectiveAccessLevel, Project, ProjectTask } from '../types/models';

function normalizeEffectiveAccessLevel(value: unknown): EffectiveAccessLevel {
  const token = String(value || '').trim().toLowerCase();
  if (token === 'owner') return 'owner';
  if (token === 'editor') return 'editor';
  return 'viewer';
}

export function canEditProject(project: Project | null | undefined): boolean {
  if (!project) return false;
  if (String(project.access_scope || 'owner').trim() === 'task_share') return false;
  const accessLevel = normalizeEffectiveAccessLevel(project.access_level || project.access_scope);
  return accessLevel === 'owner' || accessLevel === 'editor';
}

export function canManageProjectShares(
  project: Project | null | undefined,
  sessionUserId?: string | null
): boolean {
  if (!project) return false;
  if (String(project.access_scope || 'owner').trim() !== 'owner') return false;
  if (!sessionUserId) return true;
  return String(project.user_id || '').trim() === String(sessionUserId || '').trim();
}

export function canDeleteProject(
  project: Project | null | undefined,
  sessionUserId?: string | null
): boolean {
  return canManageProjectShares(project, sessionUserId);
}

export function canEditTask(task: ProjectTask | null | undefined): boolean {
  if (!task) return false;
  const accessLevel = normalizeEffectiveAccessLevel(task.access_level || task.access_scope);
  return accessLevel === 'owner' || accessLevel === 'editor';
}

export function isTaskEditableForProject(
  project: Project | null | undefined,
  taskId: string | null | undefined
): boolean {
  if (!project) return false;
  if (canEditProject(project)) return true;
  if (String(project.access_scope || 'owner').trim() !== 'task_share') return false;
  const normalizedTaskId = String(taskId || '').trim();
  if (!normalizedTaskId) return false;
  return (project.editable_task_ids || []).some((item) => String(item || '').trim() === normalizedTaskId);
}
