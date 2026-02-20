import { getTaskStatus } from '../../api/backendApi';
import type { ProjectTask } from '../../types/models';
import type { SortDirection, SortKey } from './taskListTypes';

function compareNullableNumber(a: number | null, b: number | null, ascending: boolean): number {
  if (a === null && b === null) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  return ascending ? a - b : b - a;
}

function defaultSortDirection(key: SortKey): SortDirection {
  if (key === 'pae' || key === 'backend' || key === 'seed') return 'asc';
  return 'desc';
}

function nextSortDirection(current: SortDirection): SortDirection {
  return current === 'asc' ? 'desc' : 'asc';
}

function mapTaskState(raw: string): ProjectTask['task_state'] {
  const normalized = raw.toUpperCase();
  if (normalized === 'SUCCESS') return 'SUCCESS';
  if (normalized === 'FAILURE') return 'FAILURE';
  if (normalized === 'REVOKED') return 'REVOKED';
  if (normalized === 'PENDING' || normalized === 'RECEIVED' || normalized === 'RETRY') return 'QUEUED';
  if (normalized === 'STARTED' || normalized === 'RUNNING' || normalized === 'PROGRESS') return 'RUNNING';
  return 'QUEUED';
}

async function waitForRuntimeTaskToStop(taskId: string, timeoutMs = 12000, intervalMs = 900): Promise<ProjectTask['task_state'] | null> {
  const normalizedTaskId = String(taskId || '').trim();
  if (!normalizedTaskId) return null;
  const deadline = Date.now() + Math.max(1000, timeoutMs);
  let lastState: ProjectTask['task_state'] | null = null;

  while (Date.now() < deadline) {
    try {
      const status = await getTaskStatus(normalizedTaskId);
      const mapped = mapTaskState(String(status.state || ''));
      lastState = mapped;
      if (mapped !== 'QUEUED' && mapped !== 'RUNNING') {
        return mapped;
      }
    } catch {
      // Status endpoint can be briefly unavailable while workers update state.
    }
    await new Promise<void>((resolve) => {
      window.setTimeout(() => resolve(), intervalMs);
    });
  }

  return lastState;
}

function readStatusText(status: { info?: Record<string, unknown>; state: string }): string {
  if (!status.info) return status.state;
  const s1 = status.info.status;
  const s2 = status.info.message;
  if (typeof s1 === 'string' && s1.trim()) return s1;
  if (typeof s2 === 'string' && s2.trim()) return s2;
  return status.state;
}

function resolveTaskBackendValue(task: ProjectTask, fallbackBackend = ''): string {
  const direct = String(task.backend || '').trim().toLowerCase();
  if (direct) return direct;
  const confidence =
    task.confidence && typeof task.confidence === 'object' && !Array.isArray(task.confidence)
      ? (task.confidence as Record<string, unknown>)
      : null;
  const fromConfidence = confidence && typeof confidence.backend === 'string' ? confidence.backend.trim().toLowerCase() : '';
  if (fromConfidence) return fromConfidence;
  return String(fallbackBackend || '').trim().toLowerCase();
}

function parseNumberOrNull(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

function normalizePlddtThreshold(value: number | null): number | null {
  if (value === null) return null;
  if (value >= 0 && value <= 1) return value * 100;
  return value;
}

function normalizeIptmThreshold(value: number | null): number | null {
  if (value === null) return null;
  if (value > 1 && value <= 100) return value / 100;
  return value;
}

function normalizeSmilesForSearch(value: string): string {
  return value.trim().replace(/\s+/g, '');
}

function hasSubstructureMatchPayload(value: unknown): boolean {
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (!trimmed || trimmed === '[]' || trimmed === '{}' || trimmed === 'null') return false;
    try {
      const parsed = JSON.parse(trimmed) as unknown;
      if (Array.isArray(parsed)) return parsed.length > 0;
      if (parsed && typeof parsed === 'object') return Object.keys(parsed as Record<string, unknown>).length > 0;
      return Boolean(parsed);
    } catch {
      return trimmed !== '-1';
    }
  }
  if (Array.isArray(value)) return value.length > 0;
  if (value && typeof value === 'object') return Object.keys(value as Record<string, unknown>).length > 0;
  return Boolean(value);
}

function sanitizeFileName(value: string): string {
  const trimmed = value.trim();
  const safe = trimmed.replace(/[\\/:*?"<>|]/g, '_');
  return safe || 'project';
}

function toBase64FromBytes(bytes: Uint8Array): string {
  let binary = '';
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    const chunk = bytes.subarray(i, i + chunkSize);
    binary += String.fromCharCode(...chunk);
  }
  return btoa(binary);
}

interface LoadTaskDataOptions {
  silent?: boolean;
  showRefreshing?: boolean;
  preferBackendStatus?: boolean;
  forceRefetch?: boolean;
}

const SILENT_CACHE_SYNC_WINDOW_MS = 30000;

export {
  SILENT_CACHE_SYNC_WINDOW_MS,
  compareNullableNumber,
  defaultSortDirection,
  nextSortDirection,
  mapTaskState,
  waitForRuntimeTaskToStop,
  readStatusText,
  resolveTaskBackendValue,
  parseNumberOrNull,
  normalizePlddtThreshold,
  normalizeIptmThreshold,
  normalizeSmilesForSearch,
  hasSubstructureMatchPayload,
  sanitizeFileName,
  toBase64FromBytes
};

export type { LoadTaskDataOptions };
