/**
 * Client for the asynchronous server-side task-list Excel export.
 *
 * The workbook is built by a Celery job on the CPU worker (Redis-backed queue)
 * so large task lists never freeze the browser tab or hammer PostgREST.
 * Flow: create job -> poll status (progress counters) -> download the finished
 * xlsx as a blob, which only resolves after the full body has transferred.
 */
import { API_HEADERS } from './backendClient';
import { apiUrl } from '../utils/env';

export interface TasksExcelExportRowPayload {
  row_id: string;
  task_id: string;
  name: string;
  summary: string;
  backend_label: string;
  submitted_text: string;
  duration_text: string;
  smiles: string;
  atom_plddts: number[];
  interface_label: string;
  metrics: {
    plddt: number | null;
    interface_value: number | null;
    pae: number | null;
  };
  affinity: Record<string, number | null>;
}

export type TasksExcelExportStatus = 'queued' | 'running' | 'success' | 'failure';

export interface TasksExcelExportStatusPayload {
  export_id: string;
  status: TasksExcelExportStatus;
  total: number;
  done: number;
  file_name: string;
  file_bytes: number;
  file_ready: boolean;
  /** Server-side degradation notice, e.g. "ligand images skipped". */
  warning: string;
  error: string;
}

/** Large bodies legitimately take longer than the default backend timeout. */
const CREATE_TIMEOUT_MS = 10 * 60 * 1000;
const STATUS_TIMEOUT_MS = 20000;
const DOWNLOAD_TIMEOUT_MS = 10 * 60 * 1000;

function apiErrorMessage(prefix: string, status: number, body: string): string {
  try {
    const parsed = JSON.parse(body) as { error?: unknown };
    const detail = typeof parsed.error === 'string' && parsed.error.trim() ? parsed.error.trim() : body;
    return `${prefix} (${status}): ${detail}`;
  } catch {
    return `${prefix} (${status}): ${body}`;
  }
}

export async function createTasksExcelExport(input: {
  projectName: string;
  tasks: TasksExcelExportRowPayload[];
}): Promise<{ exportId: string; total: number }> {
  const res = await fetchWithTimeout(
    '/api/export/tasks_excel',
    {
      method: 'POST',
      headers: { ...API_HEADERS, 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ project_name: input.projectName, tasks: input.tasks })
    },
    CREATE_TIMEOUT_MS
  );
  if (!res.ok) {
    throw new Error(apiErrorMessage('Failed to create Excel export', res.status, await res.text()));
  }
  const data = (await res.json()) as { export_id?: string; total?: number };
  if (!data.export_id) {
    throw new Error('Excel export response did not include an export id.');
  }
  return {
    exportId: data.export_id,
    total: data.total || input.tasks.length
  };
}

/**
 * Revoke a running export server-side (terminates the Celery task so the
 * worker slot is freed immediately). Best-effort by design: local UI state is
 * already cancelled by the caller; a failed revoke must not resurface as an
 * error after the user asked to stop.
 */
export async function cancelTasksExcelExport(exportId: string): Promise<void> {
  try {
    await fetchWithTimeout(
      `/api/export/tasks_excel/${encodeURIComponent(exportId)}/cancel`,
      { method: 'POST', headers: { ...API_HEADERS, Accept: 'application/json' } },
      STATUS_TIMEOUT_MS
    );
  } catch {
    // Server-side state converges anyway: a revoked/finished job reports
    // failure via the status route's Celery reconciliation.
  }
}

export async function getTasksExcelExportStatus(exportId: string): Promise<TasksExcelExportStatusPayload> {
  const res = await fetchWithTimeout(
    `/api/export/tasks_excel/${encodeURIComponent(exportId)}/status`,
    { method: 'GET', headers: { ...API_HEADERS, Accept: 'application/json' } },
    STATUS_TIMEOUT_MS
  );
  if (!res.ok) {
    throw new Error(apiErrorMessage('Failed to fetch Excel export status', res.status, await res.text()));
  }
  const data = (await res.json()) as Partial<TasksExcelExportStatusPayload>;
  if (
    data.status !== 'queued' &&
    data.status !== 'running' &&
    data.status !== 'success' &&
    data.status !== 'failure'
  ) {
    throw new Error(`Unknown Excel export status: ${String(data.status)}`);
  }
  return {
    export_id: data.export_id || exportId,
    status: data.status,
    total: Number(data.total || 0),
    done: Number(data.done || 0),
    file_name: data.file_name || '',
    file_bytes: Number(data.file_bytes || 0),
    file_ready: Boolean(data.file_ready),
    warning: data.warning || '',
    error: data.error || ''
  };
}

export async function downloadTasksExcelExport(
  exportId: string
): Promise<{ blob: Blob; fileName: string }> {
  const url = apiUrl(`/api/export/tasks_excel/${encodeURIComponent(exportId)}/download`);
  // Own fetch (not requestBackend): the abort timer must stay armed through
  // res.blob() — headers arriving is not the end of a large body transfer.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), DOWNLOAD_TIMEOUT_MS);
  let res: Response;
  let blob: Blob;
  try {
    res = await fetch(url, {
      method: 'GET',
      cache: 'no-store',
      headers: { ...API_HEADERS, Accept: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' },
      signal: controller.signal
    });
    if (!res.ok) {
      throw new Error(apiErrorMessage('Failed to download Excel export', res.status, await res.text()));
    }
    blob = await res.blob();
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new Error(`Excel export download timed out after ${DOWNLOAD_TIMEOUT_MS}ms for ${url}`);
    }
    throw new Error(`Backend request failed for the Excel export download (${url}): ${error instanceof Error ? error.message : String(error)}`);
  } finally {
    clearTimeout(timer);
  }
  if (blob.size === 0) {
    throw new Error('Excel export download returned an empty file.');
  }
  return { blob, fileName: parseContentDispositionFilename(res.headers.get('Content-Disposition'), exportId) };
}

/** Prefer the RFC 5987 filename* (keeps the full unicode project name). */
function parseContentDispositionFilename(disposition: string | null, exportId: string): string {
  const starMatch = /filename\*=(?:utf-8'')?([^;]+)/i.exec(disposition || '');
  const plainMatch = /filename="([^";]+)"|filename=([^;"]+)/i.exec(disposition || '');
  let fileName = starMatch?.[1] || plainMatch?.[1] || plainMatch?.[2] || `tasks_${exportId}.xlsx`;
  fileName = fileName.trim().replace(/^"|"$/g, '');
  try {
    fileName = decodeURIComponent(fileName);
  } catch {
    // A raw '%' in the header (proxy rewrite) must not kill an
    // already-downloaded file — keep the undecoded capture instead.
  }
  return fileName;
}

async function fetchWithTimeout(path: string, init: RequestInit, timeoutMs: number): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const url = apiUrl(path);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new Error(`Backend request timeout after ${timeoutMs}ms for ${path}`);
    }
    throw new Error(`Backend request failed for ${path} (${url}): ${error instanceof Error ? error.message : String(error)}`);
  } finally {
    clearTimeout(timer);
  }
}

export function triggerBrowserDownload(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = fileName;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
