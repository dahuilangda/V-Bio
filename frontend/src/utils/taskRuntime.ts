import type { TaskState, TaskStatusResponse } from '../types/models';

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

export function mapBackendTaskState(raw: string): TaskState {
  const normalized = String(raw || '').trim().toUpperCase();
  if (normalized === 'DRAFT') return 'DRAFT';
  if (normalized === 'SUCCESS') return 'SUCCESS';
  if (normalized === 'FAILURE') return 'FAILURE';
  if (normalized === 'REVOKED') return 'REVOKED';
  if (normalized === 'PENDING' || normalized === 'RECEIVED' || normalized === 'RETRY') return 'QUEUED';
  if (normalized === 'STARTED' || normalized === 'RUNNING' || normalized === 'PROGRESS') return 'RUNNING';
  return 'QUEUED';
}

function resolveNonRegressiveTaskState(currentStateInput: unknown, incomingState: TaskState): TaskState {
  const current = String(currentStateInput || '').trim().toUpperCase();
  if (!current) return incomingState;
  if (current === 'RUNNING' && incomingState === 'QUEUED') return 'RUNNING';
  if (
    (current === 'SUCCESS' || current === 'FAILURE' || current === 'REVOKED') &&
    (incomingState === 'QUEUED' || incomingState === 'RUNNING')
  ) {
    return current as TaskState;
  }
  return incomingState;
}

export function readTaskRuntimeStatusText(status: Pick<TaskStatusResponse, 'state' | 'info'>): string {
  const info = asRecord(status.info);
  if (Object.keys(info).length === 0) return status.state;
  const directStatus = info.status;
  const directMessage = info.message;
  if (typeof directStatus === 'string' && directStatus.trim()) return directStatus;
  if (typeof directMessage === 'string' && directMessage.trim()) return directMessage;
  const tracker = asRecord(info.tracker);
  const trackerDetails = tracker.details;
  const trackerStatus = tracker.status;
  if (typeof trackerDetails === 'string' && trackerDetails.trim()) return trackerDetails;
  if (typeof trackerStatus === 'string' && trackerStatus.trim()) return trackerStatus;
  return status.state;
}

function isMissingTaskRuntimeStatusText(statusText: string): boolean {
  return (
    statusText.includes('non-existent') ||
    statusText.includes('does not exist') ||
    statusText.includes('not found') ||
    statusText.includes('unknown task') ||
    statusText.includes('expired')
  );
}

export function buildTaskRuntimeFailureMessage(
  status: Pick<TaskStatusResponse, 'state' | 'info'>,
  fallback = 'Task failed.'
): string {
  const info = asRecord(status.info);
  const explicitError = String(info.error || info.exc_message || info.exc_type || '').trim();
  if (explicitError) return explicitError;
  const statusText = readTaskRuntimeStatusText(status).trim();
  const normalizedStatusText = statusText.toLowerCase();
  if (isMissingTaskRuntimeStatusText(normalizedStatusText)) {
    return statusText || fallback;
  }
  return statusText || String(status.state || '').trim() || fallback;
}

export function inferTaskStateFromStatusPayload(
  status: Pick<TaskStatusResponse, 'state' | 'info'>,
  currentStateInput?: unknown
): TaskState {
  const mapped = mapBackendTaskState(status.state);
  const statusText = readTaskRuntimeStatusText(status).trim().toLowerCase();
  if (isMissingTaskRuntimeStatusText(statusText)) {
    return resolveNonRegressiveTaskState(currentStateInput, 'FAILURE');
  }

  const hinted =
    mapped === 'QUEUED' &&
    (statusText.includes('running') ||
      statusText.includes('started') ||
      statusText.includes('starting') ||
      statusText.includes('acquiring') ||
      statusText.includes('preparing') ||
      statusText.includes('uploading') ||
      statusText.includes('processing') ||
      statusText.includes('termination in progress'))
      ? 'RUNNING'
      : mapped;

  return resolveNonRegressiveTaskState(currentStateInput, hinted);
}
