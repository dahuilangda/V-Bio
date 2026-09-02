import type { Project, ProjectTask } from '../../types/models';
import { formatDateTime, formatDuration } from '../../utils/date';
import {
  createTasksExcelExport,
  downloadTasksExcelExport,
  getTasksExcelExportStatus,
  triggerBrowserDownload,
  type TasksExcelExportRowPayload
} from '../../api/backendExportApi';
import type { TaskListRow, ExportProgressPhase } from './taskListTypes';
import { backendLabel } from './taskPresentation';

interface ExportTaskRowsToExcelInput {
  project: Project;
  filteredRows: TaskListRow[];
  onProgress?: (info: { phase: Exclude<ExportProgressPhase, 'collecting'>; done: number; total: number }) => void;
  /** Receives the server-side degradation notice (e.g. ligand images skipped). */
  onWarning?: (warning: string) => void;
  /** Receives the export id once the server job exists (enables cancellation). */
  onSubmitted?: (exportId: string) => void;
  /** Return true to abandon the export (component unmounted / project switched). */
  isCancelled?: () => boolean;
}

// Base 30 min, plus a per-row allowance — a 13k-row export with ligand images
// legitimately takes tens of minutes on the worker.
const EXPORT_POLL_BASE_MAX_MS = 30 * 60 * 1000;
const EXPORT_POLL_MS_PER_ROW = 400;
const EXPORT_POLL_MIN_INTERVAL_MS = 1500;
const EXPORT_POLL_MAX_INTERVAL_MS = 10000;
// success reported but the file is not readable: give the server this many
// polls to catch up (volume lag), then fail loudly instead of spinning.
const EXPORT_POLL_FILE_GRACE_POLLS = 10;
const EXPORT_POLL_MAX_CONSECUTIVE_ERRORS = 5;
const AFFINITY_EXPORT_FIELDS = [
  'affinity_pred_value',
  'affinity_pic50',
  'affinity_pred_value_mw',
  'affinity_pic50_mw',
  'affinity_pic501',
  'affinity_pic502',
  'affinity_probability_binary',
  'ligand_mw'
] as const;

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

function readAffinityExportPayload(task: ProjectTask): Record<string, number | null> {
  const affinity =
    task.affinity && typeof task.affinity === 'object' && !Array.isArray(task.affinity)
      ? (task.affinity as Record<string, unknown>)
      : {};
  const result: Record<string, number | null> = {};
  for (const field of AFFINITY_EXPORT_FIELDS) {
    const value = affinity[field];
    result[field] = typeof value === 'number' && Number.isFinite(value) ? value : null;
  }
  return result;
}

function normalizeAtomPlddts(values: number[] | null): number[] {
  if (!values || values.length === 0) return [];
  // Filtering element-by-element would shift indices and misalign per-atom
  // colors; if any entry is unusable, drop the whole series instead.
  if (values.some((value) => !Number.isFinite(value))) return [];
  return values.slice(0, 500).map((value) => Math.round(value * 10) / 10);
}

/**
 * Build the server-export payload purely from the (fully loaded) workspace
 * rows. ligandRenderSmiles / ligandRenderAtomPlddts / metrics are the exact
 * values the task table displays — no second data path, no re-fetch. The
 * server re-verifies runtime state and re-reads affinity from the result
 * archives, so the client never needs "fresher" copies of those.
 */
function buildExportRowPayload(row: TaskListRow): TasksExcelExportRowPayload {
  const task = row.task;
  return {
    row_id: String(task.id || '').trim(),
    task_id: String(task.task_id || '').trim(),
    name: String(task.name || '').trim(),
    summary: String(task.summary || '').trim(),
    backend_label: backendLabel(String(task.backend || '')),
    submitted_text: formatDateTime(task.submitted_at || task.created_at),
    duration_text: formatDuration(task.duration_seconds),
    // Parity with the old client-side export: the SMILES/identifier column was
    // always filled; only image rendering is gated on ligandIsSmiles.
    smiles: row.ligandRenderSmiles || row.ligandSmiles || '',
    atom_plddts: normalizeAtomPlddts(row.ligandRenderAtomPlddts),
    interface_label: row.metrics.interfaceMetricLabel || '',
    metrics: {
      plddt: row.metrics.plddt,
      interface_value: row.metrics.interfaceMetricValue,
      pae: row.metrics.pae
    },
    affinity: readAffinityExportPayload(task)
  };
}

async function pollUntilExportComplete(
  exportId: string,
  total: number,
  onProgress?: ExportTaskRowsToExcelInput['onProgress'],
  onWarning?: (warning: string) => void,
  isCancelled?: () => boolean
): Promise<void> {
  const startedAt = Date.now();
  const maxWaitMs = Math.max(EXPORT_POLL_BASE_MAX_MS, total * EXPORT_POLL_MS_PER_ROW);
  let consecutiveErrors = 0;
  // Adaptive cadence: while `done` advances keep the UI snappy; when the job
  // is quietly grinding, back off to keep request volume low on long exports.
  let intervalMs = EXPORT_POLL_MIN_INTERVAL_MS;
  let lastDone = -1;
  let fileGraceLeft = EXPORT_POLL_FILE_GRACE_POLLS;
  let pendingWarning = '';
  for (;;) {
    if (isCancelled?.()) return;
    if (Date.now() - startedAt > maxWaitMs) {
      throw new Error('Excel export timed out waiting for the server job to finish.');
    }
    let status;
    try {
      status = await getTasksExcelExportStatus(exportId);
      consecutiveErrors = 0;
    } catch (err) {
      consecutiveErrors += 1;
      if (consecutiveErrors >= EXPORT_POLL_MAX_CONSECUTIVE_ERRORS) {
        throw err;
      }
      await sleep(intervalMs);
      continue;
    }
    if (status.status === 'failure') {
      throw new Error(
        status.error
          ? `Excel export failed on the server: ${status.error}`
          : 'Excel export failed on the server.'
      );
    }
    pendingWarning = status.warning || pendingWarning;
    if (status.status === 'success' && status.file_ready) {
      onWarning?.(pendingWarning);
      onProgress?.({ phase: 'downloading', done: status.total || total, total: status.total || total });
      return;
    }
    if (status.status === 'success' && !status.file_ready) {
      fileGraceLeft -= 1;
      if (fileGraceLeft <= 0) {
        throw new Error('The export finished on the server but its file is not readable; please retry.');
      }
    }
    onProgress?.({ phase: 'exporting', done: status.done, total: status.total || total });
    intervalMs = status.done !== lastDone
      ? EXPORT_POLL_MIN_INTERVAL_MS
      : Math.min(intervalMs * 2, EXPORT_POLL_MAX_INTERVAL_MS);
    lastDone = status.done;
    await sleep(intervalMs);
  }
}

export async function exportTaskRowsToExcel({
  project,
  filteredRows,
  onProgress,
  onWarning,
  onSubmitted,
  isCancelled
}: ExportTaskRowsToExcelInput): Promise<void> {
  if (filteredRows.length === 0) return;
  onProgress?.({ phase: 'submitting', done: 0, total: filteredRows.length });

  // Zero re-fetching: the caller passes the COMPLETE filtered set (the page
  // awaits the full list load first); submitting is the only network traffic.
  const payloadTasks = filteredRows.map((row) => buildExportRowPayload(row));

  const created = await createTasksExcelExport({
    projectName: project.name || 'Tasks',
    tasks: payloadTasks
  });
  onSubmitted?.(created.exportId);
  onProgress?.({ phase: 'exporting', done: 0, total: created.total });

  await pollUntilExportComplete(created.exportId, created.total, onProgress, onWarning, isCancelled);
  if (isCancelled?.()) return;

  const { blob, fileName } = await downloadTasksExcelExport(created.exportId);
  if (isCancelled?.()) return;
  triggerBrowserDownload(blob, fileName);
}
