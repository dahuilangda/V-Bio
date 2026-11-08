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
let taskStatusBatchTimer: ReturnType<typeof setTimeout> | null = null;
// 增量轮询第一阶段：每任务的轻量状态指纹（服务端 /status/stubs）。
// ver 未变化 → 直接复用上一份全量状态，不进批量拉取；map 有界防泄漏。
const statusVerById = new Map<string, string>();
const lastStatusById = new Map<string, TaskStatusResponse>();
const STATUS_VER_MAP_MAX_ENTRIES = 20000;

function trimStatusVerMap(): void {
  const excess = statusVerById.size - STATUS_VER_MAP_MAX_ENTRIES;
  if (excess <= 0) return;
  let dropped = 0;
  for (const key of statusVerById.keys()) {
    statusVerById.delete(key);
    dropped += 1;
    if (dropped >= excess) break;
  }
}

async function fetchStatusStubs(taskIds: string[]): Promise<Map<string, string>> {
  const res = await requestBackend('/status/stubs', {
    method: 'POST',
    headers: {
      ...API_HEADERS,
      Accept: 'application/json',
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({ task_ids: taskIds })
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to fetch status stubs (${res.status}): ${text}`);
  }
  const payload = (await res.json()) as { stubs?: { task_id?: string; ver?: string }[] };
  const stubs = new Map<string, string>();
  for (const item of Array.isArray(payload.stubs) ? payload.stubs : []) {
    const taskId = String(item?.task_id || '').trim();
    const ver = String(item?.ver || '').trim();
    if (taskId && ver) stubs.set(taskId, ver);
  }
  return stubs;
}
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
    lastStatusById.set(taskId, status);
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
    taskStatusBatchTimer = setTimeout(() => {
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
  const failedTaskIds: string[] = [];

  const uncachedIds: string[] = [];
  for (const taskId of normalizedTaskIds) {
    const cached = readCachedTaskStatus(taskId);
    if (cached) {
      byTaskId[taskId] = cached;
      continue;
    }
    uncachedIds.push(taskId);
  }

  // 增量两阶段：先取轻量指纹，仅对 ver 变化（或本地无全量）的 id 发批量请求；
  // 未变化 id 直接复用上一份全量。stub 不可用（旧后端/瞬时故障）则退回全量路径。
  let fullFetchIds = uncachedIds;
  // 待拉取 id 在 stub 时刻的指纹：批量返回可能比 stub 更新，但契约以
  // “ver 相同即视为状态未变”为准，存 stub 时刻的 ver 即可。
  const stubVerById = new Map<string, string>();
  if (fullFetchIds.length > 0) {
    try {
      const stubs = await fetchStatusStubs(fullFetchIds);
      fullFetchIds = fullFetchIds.filter((taskId) => {
        const ver = stubs.get(taskId);
        const known = statusVerById.get(taskId);
        const last = lastStatusById.get(taskId);
        if (ver !== undefined && known === ver && last) {
          byTaskId[taskId] = last;
          return false;
        }
        if (ver !== undefined) stubVerById.set(taskId, ver);
        return true;
      });
      trimStatusVerMap();
    } catch {
      // 退回全量
    }
  }

  for (const taskId of fullFetchIds) {
    const stubVer = stubVerById.get(taskId);
    pendingLookups.push(
      queueTaskStatus(taskId)
        .then((status) => {
          byTaskId[taskId] = status;
          if (stubVer) {
            statusVerById.set(taskId, stubVer);
            trimStatusVerMap();
          }
        })
        .catch(() => {
          // 批量失败不静默：记入 failedTaskIds 供日志说明列表为何陈旧。
          failedTaskIds.push(taskId);
        })
    );
  }

  if (pendingLookups.length > 0) {
    await Promise.all(pendingLookups);
  }
  if (failedTaskIds.length > 0) {
    console.warn(
      `[taskStatus] ${failedTaskIds.length}/${normalizedTaskIds.length} status lookup(s) failed; ` +
        'active tasks keep their last known state: ' +
        failedTaskIds.slice(0, 8).join(', ') +
        (failedTaskIds.length > 8 ? ` …(+${failedTaskIds.length - 8})` : '')
    );
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

export async function downloadResultBlob(
  taskId: string,
  options?: { mode?: DownloadResultMode; preferredStructureName?: string }
): Promise<Blob> {
  const mode = options?.mode || 'full';
  const path = mode === 'view' ? `/results/${taskId}/view` : `/results/${taskId}`;
  const params = new URLSearchParams();
  const preferredStructureName = String(options?.preferredStructureName || '').trim();
  if (mode === 'view' && preferredStructureName) {
    params.set('structure_name', preferredStructureName);
  }
  const url = apiUrl(params.size > 0 ? `${path}?${params.toString()}` : path);
  const res = await fetchWithTimeout(url, {
    // Result archives are content-addressed by the server's ETag: repeat
    // downloads of an unchanged archive revalidate (304, no transfer). A
    // changed archive gets a new stat and therefore a new ETag — no stale
    // window beyond the server's 60s max_age.
    cache: 'default',
    // The runtime gateway authorizes task reads by X-API-Token (platform token) or an
    // explicit project_id query — every other gateway call spreads API_HEADERS; a bare
    // fetch here 403'd result downloads with "project_id query is required".
    headers: { ...API_HEADERS }
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to download result (${res.status}) from ${url}: ${text}`);
  }
  return await res.blob();
}
