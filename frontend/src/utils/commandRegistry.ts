// Pure command-palette logic: command registry shape, grouping, filtering. Kept free of
// React/DOM so the filtering and ordering contracts are unit-testable (the palette
// component is a thin keyboard/ARIA shell over these).

import { fuzzyScore } from './fuzzyScore';

export interface PaletteCommand {
  /** Stable identity (cmdk lesson: selection tracks VALUE, never index — safe across
   * filtering re-renders and Strict Mode double-mounts). */
  id: string;
  label: string;
  /** Secondary match text (subtitle, keywords) folded into the fuzzy score. */
  hint?: string;
  group: string;
  /** Fuzzy score tie-break: lower runs first (cmdk keeps consumer render order). */
  order: number;
  run: () => void;
}

/** Filter + rank a command list for a query: non-matches drop, matches sort by fuzzy score
 * (label; hint rides as alias text) descending, then by declared order. Empty query keeps
 * the full list in declared order — the palette's default surface. */
export function filterCommands(commands: PaletteCommand[], query: string): PaletteCommand[] {
  const trimmed = query.trim();
  if (!trimmed) {
    return [...commands].sort((a, b) => a.order - b.order);
  }
  return commands
    .map((command, index) => ({
      command,
      index,
      score: fuzzyScore(command.label, trimmed, command.hint ? [command.hint] : []),
    }))
    .filter((entry) => entry.score > 0)
    .sort((a, b) => b.score - a.score || a.command.order - b.command.order || a.index - b.index)
    .map((entry) => entry.command);
}

/** Group a filtered list preserving relative order (fuzzy rank within the group wins —
 * cmdk keeps DOM order; we keep score order, which is the palette's purpose). */
export function groupCommands(commands: PaletteCommand[]): Array<{ group: string; commands: PaletteCommand[] }> {
  const groups: Array<{ group: string; commands: PaletteCommand[] }> = [];
  const byGroup = new Map<string, { group: string; commands: PaletteCommand[] }>();
  for (const command of commands) {
    let bucket = byGroup.get(command.group);
    if (!bucket) {
      bucket = { group: command.group, commands: [] };
      byGroup.set(command.group, bucket);
      groups.push(bucket);
    }
    bucket.commands.push(command);
  }
  return groups;
}

/** Next index under the palette's keyboard model: arrows loop (cmdk `loop` default true),
 * Home/End jump to the extremes, PageUp/Down move in pages. Pure so it is testable. */
export function nextPaletteIndex(
  count: number,
  current: number,
  key: 'ArrowDown' | 'ArrowUp' | 'Home' | 'End' | 'PageDown' | 'PageUp'
): number {
  if (count <= 0) return 0;
  const clamped = Math.min(Math.max(current, 0), count - 1);
  switch (key) {
    case 'ArrowDown':
      return (clamped + 1) % count;
    case 'ArrowUp':
      return (clamped - 1 + count) % count;
    case 'Home':
      return 0;
    case 'End':
      return count - 1;
    case 'PageDown':
      return Math.min(count - 1, clamped + 6);
    case 'PageUp':
      return Math.max(0, clamped - 6);
  }
}

/** The ⌘K/K keyboard chord, checked at the window level. Works while focused in inputs
 * (palette convention); ignored while an IME composition is in progress. */
export function isPaletteToggleEvent(event: KeyboardEvent): boolean {
  if (event.key !== 'k' && event.key !== 'K') return false;
  return event.metaKey || event.ctrlKey;
}
