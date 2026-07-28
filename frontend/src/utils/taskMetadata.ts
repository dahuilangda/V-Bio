export const TASK_SUMMARY_MAX_LENGTH = 300;

export function limitTaskSummary(value: string): string {
  return value.slice(0, TASK_SUMMARY_MAX_LENGTH);
}

export function normalizeTaskSummary(value: unknown): string {
  if (typeof value !== 'string') return '';
  return value.trim().slice(0, TASK_SUMMARY_MAX_LENGTH);
}
