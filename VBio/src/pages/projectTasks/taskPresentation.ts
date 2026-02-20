import type { ProjectTask } from '../../types/models';
import { getWorkflowDefinition, normalizeWorkflowKey, type WorkflowKey } from '../../utils/workflows';
import type { MetricTone } from './taskListTypes';

function hasObjectFields(value: unknown): boolean {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value as Record<string, unknown>).length > 0);
}

function toSearchText(task: ProjectTask): string {
  return [task.name, task.summary, task.status_text, task.error_text].join(' ').toLowerCase();
}

function includesAny(text: string, tokens: string[]): boolean {
  return tokens.some((token) => text.includes(token));
}

export function toneForPlddt(value: number | null): MetricTone {
  if (value === null) return 'neutral';
  if (value >= 90) return 'excellent';
  if (value >= 70) return 'good';
  if (value >= 50) return 'medium';
  return 'low';
}

export function toneForIptm(value: number | null): MetricTone {
  if (value === null) return 'neutral';
  const normalized = value <= 1 ? value : value / 100;
  if (normalized >= 0.8) return 'excellent';
  if (normalized >= 0.6) return 'good';
  if (normalized >= 0.4) return 'medium';
  return 'low';
}

export function toneForPae(value: number | null): MetricTone {
  if (value === null) return 'neutral';
  if (value <= 5) return 'excellent';
  if (value <= 10) return 'good';
  if (value <= 20) return 'medium';
  return 'low';
}

export function formatMetric(value: number | null, fractionDigits: number): string {
  if (value === null) return '-';
  return value.toFixed(fractionDigits);
}

export function taskStateLabel(state: ProjectTask['task_state']): string {
  if (state === 'QUEUED') return 'Queued';
  if (state === 'RUNNING') return 'Running';
  if (state === 'SUCCESS') return 'Success';
  if (state === 'FAILURE') return 'Failed';
  if (state === 'REVOKED') return 'Revoked';
  return 'Draft';
}

export function taskStateTone(state: ProjectTask['task_state']): 'draft' | 'queued' | 'running' | 'success' | 'failure' | 'revoked' {
  if (state === 'QUEUED') return 'queued';
  if (state === 'RUNNING') return 'running';
  if (state === 'SUCCESS') return 'success';
  if (state === 'FAILURE') return 'failure';
  if (state === 'REVOKED') return 'revoked';
  return 'draft';
}

function normalizeStatusToken(value: string): string {
  return value.trim().toLowerCase().replace(/[_\s-]+/g, '');
}

export function shouldShowRunNote(state: ProjectTask['task_state'], note: string): boolean {
  const trimmed = note.trim();
  if (!trimmed) return false;
  const normalizedNote = normalizeStatusToken(trimmed);
  const normalizedState = normalizeStatusToken(state);
  if (normalizedNote === normalizedState) return false;
  const label = taskStateLabel(state);
  if (normalizedNote === normalizeStatusToken(label)) return false;
  return true;
}

export function backendLabel(value: string): string {
  if (value === 'alphafold3') return 'AlphaFold3';
  if (value === 'protenix') return 'Protenix';
  if (value === 'boltz') return 'Boltz-2';
  return value ? value.toUpperCase() : 'Unknown';
}

export function resolveTaskWorkflowKey(task: ProjectTask, fallbackTaskType: string): WorkflowKey {
  const normalizedFallback = normalizeWorkflowKey(fallbackTaskType);
  if (normalizedFallback === 'prediction' || normalizedFallback === 'affinity' || normalizedFallback === 'lead_optimization') {
    return normalizedFallback;
  }

  const searchText = toSearchText(task);
  if (includesAny(searchText, ['lead optimization', 'lead-opt', 'lead_opt', 'mmp', 'fragment'])) {
    return 'lead_optimization';
  }
  if (includesAny(searchText, ['affinity', 'binding', 'pocket score', 'delta g', 'ic50'])) {
    return 'affinity';
  }

  if (hasObjectFields(task.affinity)) {
    const affinityPayload = task.affinity as Record<string, unknown>;
    if (
      typeof affinityPayload.binding_probability === 'number' ||
      typeof affinityPayload.predicted_affinity === 'number' ||
      typeof affinityPayload.affinity_score === 'number'
    ) {
      return 'affinity';
    }
  }

  return 'prediction';
}

export function workflowLabelForTask(task: ProjectTask, fallbackTaskType: string): string {
  const key = resolveTaskWorkflowKey(task, fallbackTaskType);
  return getWorkflowDefinition(key).shortTitle;
}

export function workflowClassNameForTask(task: ProjectTask, fallbackTaskType: string): string {
  return resolveTaskWorkflowKey(task, fallbackTaskType).replace(/_/g, '-');
}
