/** Shared payload readers for Copilot-filter parsing (used by multiple pages). */

export function readCopilotText(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

export function readCopilotNumber(value: unknown): number | null {
  const n = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

export function isOneOf<T extends readonly string[]>(value: string, options: T): value is T[number] {
  return (options as readonly string[]).includes(value);
}
