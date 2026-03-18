import type { TaskStatusResponse } from '../types/models';
import { apiUrl } from '../utils/env';
import { API_HEADERS, fetchWithTimeout, requestBackend } from './backendClient';

export interface TaskRuntimeIndexResponse {
  active_task_ids?: string[];
  reserved_task_ids?: string[];
  scheduled_task_ids?: string[];
}

export async function getTaskStatus(taskId: string): Promise<TaskStatusResponse> {
  const res = await requestBackend(`/status/${taskId}`, {
    headers: {
      Accept: 'application/json'
    }
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to fetch task status (${res.status}): ${text}`);
  }
  return (await res.json()) as TaskStatusResponse;
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
  const res = await requestBackend('/status/batch', {
    method: 'POST',
    headers: {
      ...API_HEADERS,
      Accept: 'application/json',
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      task_ids: normalizedTaskIds
    })
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to fetch batch task status (${res.status}): ${text}`);
  }
  const payload = (await res.json()) as {
    statuses?: TaskStatusResponse[];
  };
  const byTaskId: Record<string, TaskStatusResponse> = {};
  for (const item of Array.isArray(payload.statuses) ? payload.statuses : []) {
    const taskId = String(item?.task_id || '').trim();
    if (!taskId) continue;
    byTaskId[taskId] = item;
  }
  return byTaskId;
}

export async function getTaskRuntimeIndex(): Promise<TaskRuntimeIndexResponse> {
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
  return (await res.json()) as TaskRuntimeIndexResponse;
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
