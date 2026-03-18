import type { TaskState, TaskStatusResponse } from '../types/models';

export function mapBackendTaskState(raw: string): TaskState {
  const normalized = String(raw || '').trim().toUpperCase();
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
  if (!status.info) return status.state;
  const directStatus = status.info.status;
  const directMessage = status.info.message;
  if (typeof directStatus === 'string' && directStatus.trim()) return directStatus;
  if (typeof directMessage === 'string' && directMessage.trim()) return directMessage;
  return status.state;
}

export function inferTaskStateFromStatusPayload(
  status: Pick<TaskStatusResponse, 'state' | 'info'>,
  currentStateInput?: unknown
): TaskState {
  const mapped = mapBackendTaskState(status.state);
  const statusText = readTaskRuntimeStatusText(status).trim().toLowerCase();
  const pendingLike = mapped === 'QUEUED' || mapped === 'RUNNING';
  if (
    statusText.includes('non-existent') ||
    statusText.includes('does not exist') ||
    statusText.includes('not found') ||
    statusText.includes('unknown task') ||
    statusText.includes('expired')
  ) {
    if (pendingLike) {
      return resolveNonRegressiveTaskState(currentStateInput, mapped);
    }
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
