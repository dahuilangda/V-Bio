import type { TaskStatusResponse } from '../types/models';
import { apiUrl } from '../utils/env';
import { API_HEADERS, fetchWithTimeout, requestBackend } from './backendClient';

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

export async function getTaskStatusBatch(taskIds: string[]): Promise<Record<string, TaskStatusResponse>> {
  const normalizedTaskIds = Array.from(
    new Set(
      (Array.isArray(taskIds) ? taskIds : [])
        .map((item) => String(item || '').trim())
        .filter(Boolean)
    )
  ).slice(0, 64);
  if (normalizedTaskIds.length === 0) return {};
  const res = await requestBackend('/status_batch', {
    method: 'POST',
    headers: {
      ...API_HEADERS,
      Accept: 'application/json'
    },
    body: JSON.stringify({
      task_ids: normalizedTaskIds
    })
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to fetch task status batch (${res.status}): ${text}`);
  }
  const payload = (await res.json().catch(() => ({}))) as { tasks?: TaskStatusResponse[] };
  const tasks = Array.isArray(payload?.tasks) ? payload.tasks : [];
  const byTaskId: Record<string, TaskStatusResponse> = {};
  for (const task of tasks) {
    const taskId = String(task?.task_id || '').trim();
    if (!taskId) continue;
    byTaskId[taskId] = task;
  }
  return byTaskId;
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
