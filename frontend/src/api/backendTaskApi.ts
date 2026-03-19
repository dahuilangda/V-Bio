import type { TaskStatusResponse } from '../types/models';
import { apiUrl } from '../utils/env';
import { API_HEADERS, fetchWithTimeout, requestBackend } from './backendClient';

export interface TaskRuntimeIndexResponse {
  active_task_ids?: string[];
  reserved_task_ids?: string[];
  scheduled_task_ids?: string[];
}

const ACTIVE_TASK_STATUS_CACHE_MS = 1200;
const TERMINAL_TASK_STATUS_CACHE_MS = 15000;
const TASK_STATUS_BATCH_FLUSH_DELAY_MS = 12;
const TASK_STATUS_BATCH_CHUNK_SIZE = 128;
const RUNTIME_INDEX_CACHE_MS = 1000;

type CachedTaskStatusEntry = {
  status: TaskStatusResponse;
  expiresAt: number;
};

type PendingTaskStatusWaiter = {
  resolve: (status: TaskStatusResponse) => void;
  reject: (error: unknown) => void;
};

const taskStatusCache = new Map<string, CachedTaskStatusEntry>();
const pendingTaskStatusWaiters = new Map<string, PendingTaskStatusWaiter[]>();
const pendingTaskStatusIds = new Set<string>();
let taskStatusBatchTimer: number | null = null;
let runtimeIndexCache: { value: TaskRuntimeIndexResponse; expiresAt: number } | null = null;
let runtimeIndexInFlight: Promise<TaskRuntimeIndexResponse> | null = null;

function taskStatusCacheMsForState(stateInput: unknown): number {
  const state = String(stateInput || '').trim().toUpperCase();
  return state === 'SUCCESS' || state === 'FAILURE' || state === 'REVOKED'
    ? TERMINAL_TASK_STATUS_CACHE_MS
    : ACTIVE_TASK_STATUS_CACHE_MS;
}

function readCachedTaskStatus(taskId: string): TaskStatusResponse | null {
  const cached = taskStatusCache.get(taskId);
  if (!cached) return null;
  if (cached.expiresAt <= Date.now()) {
    taskStatusCache.delete(taskId);
    return null;
  }
  return cached.status;
}

function writeCachedTaskStatus(status: TaskStatusResponse): void {
  const taskId = String(status?.task_id || '').trim();
  if (!taskId) return;
  taskStatusCache.set(taskId, {
    status,
    expiresAt: Date.now() + taskStatusCacheMsForState(status.state),
  });
}

async function flushPendingTaskStatusBatch(): Promise<void> {
  taskStatusBatchTimer = null;
  const taskIds = Array.from(pendingTaskStatusIds);
  pendingTaskStatusIds.clear();
  if (taskIds.length === 0) return;

  const resolveTask = (taskId: string, status: TaskStatusResponse) => {
    writeCachedTaskStatus(status);
    const waiters = pendingTaskStatusWaiters.get(taskId) || [];
    pendingTaskStatusWaiters.delete(taskId);
    for (const waiter of waiters) waiter.resolve(status);
  };
  const rejectTask = (taskId: string, error: unknown) => {
    const waiters = pendingTaskStatusWaiters.get(taskId) || [];
    pendingTaskStatusWaiters.delete(taskId);
    for (const waiter of waiters) waiter.reject(error);
  };

  try {
    const byTaskId: Record<string, TaskStatusResponse> = {};
    for (let i = 0; i < taskIds.length; i += TASK_STATUS_BATCH_CHUNK_SIZE) {
      const chunk = taskIds.slice(i, i + TASK_STATUS_BATCH_CHUNK_SIZE);
      const res = await requestBackend('/status/batch', {
        method: 'POST',
        headers: {
          ...API_HEADERS,
          Accept: 'application/json',
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          task_ids: chunk
        })
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(`Failed to fetch batch task status (${res.status}): ${text}`);
      }
      const payload = (await res.json()) as {
        statuses?: TaskStatusResponse[];
      };
      for (const item of Array.isArray(payload.statuses) ? payload.statuses : []) {
        const taskId = String(item?.task_id || '').trim();
        if (!taskId) continue;
        byTaskId[taskId] = item;
      }
      for (const taskId of chunk) {
        const status = byTaskId[taskId];
        if (status) {
          resolveTask(taskId, status);
          continue;
        }
        rejectTask(taskId, new Error(`Missing task status for ${taskId}.`));
      }
    }
  } catch (error) {
    for (const taskId of taskIds) {
      rejectTask(taskId, error);
    }
  }
}

function queueTaskStatus(taskId: string): Promise<TaskStatusResponse> {
  return new Promise<TaskStatusResponse>((resolve, reject) => {
    const waiters = pendingTaskStatusWaiters.get(taskId) || [];
    waiters.push({ resolve, reject });
    pendingTaskStatusWaiters.set(taskId, waiters);
    pendingTaskStatusIds.add(taskId);
    if (taskStatusBatchTimer !== null) return;
    taskStatusBatchTimer = window.setTimeout(() => {
      void flushPendingTaskStatusBatch();
    }, TASK_STATUS_BATCH_FLUSH_DELAY_MS);
  });
}

export async function getTaskStatus(taskId: string): Promise<TaskStatusResponse> {
  const normalizedTaskId = String(taskId || '').trim();
  if (!normalizedTaskId) {
    throw new Error('Missing task ID for status request.');
  }
  const statuses = await getTaskStatuses([normalizedTaskId]);
  const status = statuses[normalizedTaskId];
  if (!status) {
    throw new Error(`Missing task status for ${normalizedTaskId}.`);
  }
  return status;
}

export async function getTaskStatuses(taskIds: string[]): Promise<Record<string, TaskStatusResponse>> {
  const normalizedTaskIds = Array.from(
    new Set(
      (Array.isArray(taskIds) ? taskIds : [])
        .map((item) => String(item || '').trim())
        .filter(Boolean)
    )
  );
  if (normalizedTaskIds.length === 0) return {};
  const byTaskId: Record<string, TaskStatusResponse> = {};
  const pendingLookups: Array<Promise<void>> = [];

  for (const taskId of normalizedTaskIds) {
    const cached = readCachedTaskStatus(taskId);
    if (cached) {
      byTaskId[taskId] = cached;
      continue;
    }
    pendingLookups.push(
      queueTaskStatus(taskId)
        .then((status) => {
          byTaskId[taskId] = status;
        })
        .catch(() => undefined)
    );
  }

  if (pendingLookups.length > 0) {
    await Promise.all(pendingLookups);
  }

  return byTaskId;
}

export async function getTaskRuntimeIndex(): Promise<TaskRuntimeIndexResponse> {
  if (runtimeIndexCache && runtimeIndexCache.expiresAt > Date.now()) {
    return runtimeIndexCache.value;
  }
  if (runtimeIndexInFlight) {
    return await runtimeIndexInFlight;
  }
  runtimeIndexInFlight = (async () => {
    const res = await requestBackend('/tasks/runtime_index', {
      headers: {
        ...API_HEADERS,
        Accept: 'application/json'
      }
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`Failed to fetch task runtime index (${res.status}): ${text}`);
    }
    const payload = (await res.json()) as TaskRuntimeIndexResponse;
    runtimeIndexCache = {
      value: payload,
      expiresAt: Date.now() + RUNTIME_INDEX_CACHE_MS
    };
    return payload;
  })();
  try {
    return await runtimeIndexInFlight;
  } finally {
    runtimeIndexInFlight = null;
  }
}

export async function terminateTask(taskId: string): Promise<{
  status?: string;
  task_id?: string;
  terminated?: boolean;
  details?: Record<string, unknown>;
}> {
  const normalizedTaskId = String(taskId || '').trim();
  if (!normalizedTaskId) {
    throw new Error('Missing task_id for termination.');
  }
  const res = await requestBackend(`/tasks/${encodeURIComponent(normalizedTaskId)}`, {
    method: 'DELETE',
    headers: {
      ...API_HEADERS,
      Accept: 'application/json'
    }
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to terminate task (${res.status}): ${text}`);
  }
  const payload = (await res.json().catch(() => ({}))) as {
    status?: string;
    task_id?: string;
    terminated?: boolean;
    details?: Record<string, unknown>;
  };
  return payload;
}

export type DownloadResultMode = 'view' | 'full';

export async function downloadResultBlob(taskId: string, options?: { mode?: DownloadResultMode }): Promise<Blob> {
  const mode = options?.mode || 'full';
  const path = mode === 'view' ? `/results/${taskId}/view` : `/results/${taskId}`;
  const url = apiUrl(path);
  const res = await fetchWithTimeout(url, {
    cache: 'no-store'
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to download result (${res.status}) from ${url}: ${text}`);
  }
  return await res.blob();
}
