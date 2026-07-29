import { managementApiUrl, requestManagement } from './backendClient';

export type AdminTaskBucket = 'queued' | 'running' | 'success' | 'failure' | 'cancelled' | 'other';

export interface AdminClusterTask {
  id: string;
  name: string;
  capability: string | null;
  state: string;
  queue: string;
  runtime_seconds: number | null;
  time_start: string | null;
  eta: string | null;
}

export interface AdminWorkerStatus {
  server: string;
  host: string;
  worker_type: 'gpu' | 'cpu' | 'mixed' | string;
  queues: string[];
  capabilities: string[];
  resources: {
    slots_total: number;
    slots_busy: number;
    slots_idle: number;
    gpu_slots_total: number;
    cpu_slots_total: number;
  };
  utilization: {
    slot_utilization: number;
  };
  tasks: {
    active: AdminClusterTask[];
    reserved: AdminClusterTask[];
    scheduled: AdminClusterTask[];
  };
  tasks_truncated: {
    active: boolean;
    reserved: boolean;
    scheduled: boolean;
  };
  task_counts: {
    active: number;
    reserved: number;
    scheduled: number;
  };
  task_counters: {
    executed_total_since_start: number;
    executed_by_task_name: Record<string, number>;
  };
  worker_stats: {
    uptime_seconds: number;
    pid: number;
    clock: number;
  };
}

export interface AdminCapabilityStatus {
  online: boolean;
  workers: string[];
  worker_count: number;
  max_running_tasks_upper_bound: number;
  gpu_slots_total: number;
  cpu_slots_total: number;
  active_tasks_count: number;
  reserved_tasks_count: number;
  scheduled_tasks_count: number;
  active_tasks: AdminClusterTask[];
  reserved_tasks: AdminClusterTask[];
  scheduled_tasks: AdminClusterTask[];
}

export interface AdminClusterSnapshot {
  generated_at: string;
  worker_count: number;
  summary: {
    workers_total: number;
    capabilities_total: number;
    capabilities_online: number;
    slots_total: number;
    slots_busy: number;
    slots_idle: number;
    gpu_slots_total: number;
    cpu_slots_total: number;
  };
  workers: Record<string, AdminWorkerStatus>;
  capabilities: Record<string, AdminCapabilityStatus>;
}

export interface AdminTaskStateCounts {
  queued: number;
  running: number;
  success: number;
  failure: number;
  cancelled: number;
  other: number;
}

export interface AdminBackendStatistics extends AdminTaskStateCounts {
  backend: string;
  total: number;
}

export interface AdminTaskTimelinePoint {
  start: string;
  total: number;
  success: number;
  failure: number;
}

export interface AdminRecentTask {
  id: string;
  project_id: string;
  task_id: string;
  name: string;
  backend: string;
  state: string;
  bucket: AdminTaskBucket;
  submitted_at: string;
  completed_at: string | null;
  duration_seconds: number | null;
  status_text: string;
  error_text: string;
}

export interface AdminTaskStatistics {
  generated_at: string;
  window_start: string;
  window_hours: number;
  total: number;
  states: AdminTaskStateCounts;
  terminal_total: number;
  success_rate: number | null;
  average_duration_seconds: number | null;
  by_backend: AdminBackendStatistics[];
  timeline: AdminTaskTimelinePoint[];
  recent_tasks: AdminRecentTask[];
  truncated: boolean;
}

export interface AdminClusterOverview {
  sequence: number;
  generated_at: string;
  cluster: AdminClusterSnapshot | null;
  cluster_error: string;
  tasks: AdminTaskStatistics | null;
  tasks_error: string;
}

function isNonEmptyRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length > 0);
}

export async function fetchAdminClusterOverview(
  managementToken: string,
  windowHours: number
): Promise<AdminClusterOverview> {
  const safeHours = Math.max(1, Math.min(24 * 31, Math.round(Number(windowHours) || 24)));
  const res = await requestManagement(
    `/vbio-api/admin/cluster-overview?window_hours=${safeHours}`,
    {
      method: 'GET',
      headers: {
        Accept: 'application/json',
        'X-VBio-Session': managementToken
      }
    },
    30000
  );
  const payload = (await res.json().catch(() => ({}))) as {
    generated_at?: string;
    cluster?: unknown;
    cluster_error?: string;
    tasks?: unknown;
    tasks_error?: string;
    error?: string;
  };
  if (!res.ok) {
    throw new Error(payload.error || `Cluster overview request failed with HTTP ${res.status}.`);
  }
  return {
    sequence: Math.max(0, Number((payload as { sequence?: number }).sequence) || 0),
    generated_at: String(payload.generated_at || ''),
    cluster: isNonEmptyRecord(payload.cluster) ? payload.cluster as unknown as AdminClusterSnapshot : null,
    cluster_error: String(payload.cluster_error || ''),
    tasks: isNonEmptyRecord(payload.tasks) ? payload.tasks as unknown as AdminTaskStatistics : null,
    tasks_error: String(payload.tasks_error || '')
  };
}

export interface AdminMonitorStreamOptions {
  managementToken: string;
  windowHours: number;
  cursor: number;
  signal: AbortSignal;
  onOpen?: () => void;
  onOverview: (overview: AdminClusterOverview) => void;
}

function parseOverviewPayload(value: string): AdminClusterOverview {
  const payload = JSON.parse(value) as AdminClusterOverview;
  if (!payload || typeof payload !== 'object') {
    throw new Error('Monitor stream returned an invalid overview payload.');
  }
  return payload;
}

export async function streamAdminClusterOverview(options: AdminMonitorStreamOptions): Promise<number> {
  const safeHours = Math.max(1, Math.min(24 * 31, Math.round(Number(options.windowHours) || 24)));
  let latestCursor = Math.max(0, Math.round(Number(options.cursor) || 0));
  const url = managementApiUrl(
    `/vbio-api/admin/monitor-stream?window_hours=${safeHours}&cursor=${latestCursor}`
  );
  const response = await fetch(url, {
    method: 'GET',
    headers: {
      Accept: 'text/event-stream',
      'Cache-Control': 'no-cache',
      'Last-Event-ID': String(latestCursor),
      'X-VBio-Session': options.managementToken
    },
    cache: 'no-store',
    signal: options.signal
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as { error?: string };
    throw new Error(payload.error || `Monitor stream failed with HTTP ${response.status}.`);
  }
  if (!response.body) {
    throw new Error('Monitor stream response has no readable body.');
  }
  options.onOpen?.();

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let reconnectRequested = false;
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() || '';
    for (const frame of frames) {
      let eventName = 'message';
      let eventId = '';
      const dataLines: string[] = [];
      for (const line of frame.split(/\r?\n/)) {
        if (!line || line.startsWith(':')) continue;
        const separator = line.indexOf(':');
        const field = separator >= 0 ? line.slice(0, separator) : line;
        const rawValue = separator >= 0 ? line.slice(separator + 1) : '';
        const fieldValue = rawValue.startsWith(' ') ? rawValue.slice(1) : rawValue;
        if (field === 'event') eventName = fieldValue;
        else if (field === 'id') eventId = fieldValue;
        else if (field === 'data') dataLines.push(fieldValue);
      }
      if (eventId) latestCursor = Math.max(latestCursor, Number(eventId) || 0);
      if (eventName === 'reconnect') reconnectRequested = true;
      if (eventName === 'overview' && dataLines.length) {
        const overview = parseOverviewPayload(dataLines.join('\n'));
        latestCursor = Math.max(latestCursor, Number(overview.sequence) || 0);
        options.onOverview({ ...overview, sequence: latestCursor });
      }
    }
    if (done) {
      if (reconnectRequested) return latestCursor;
      break;
    }
  }
  throw new Error(`Monitor stream closed after event ${latestCursor}.`);
}
