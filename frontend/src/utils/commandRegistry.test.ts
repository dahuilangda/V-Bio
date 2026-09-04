import { describe, expect, it } from 'vitest';
import {
  filterCommands,
  groupCommands,
  isPaletteToggleEvent,
  nextPaletteIndex,
  type PaletteCommand,
} from './commandRegistry';

function cmd(id: string, label: string, order: number, group = 'g', hint?: string): PaletteCommand {
  return { id, label, hint, group, order, run: () => {} };
}

describe('filterCommands', () => {
  const commands = [
    cmd('nav:projects', 'Projects', 10, 'Navigation', 'projects home'),
    cmd('nav:shares', 'Sharing & tokens', 20, 'Navigation', 'shares'),
    cmd('action:new', 'New project', 30, 'Actions', 'create prediction docking'),
    cmd('recent:1', 'Docking 2026-08-17', 100, 'Recent projects', 'Recent project affinity'),
  ];

  it('empty query keeps the full list in declared order', () => {
    expect(filterCommands(commands, '   ').map((c) => c.id)).toEqual(
      ['nav:projects', 'nav:shares', 'action:new', 'recent:1']
    );
  });

  it('fuzzy-matches label subsequences and drops non-matches', () => {
    expect(filterCommands(commands, 'wproj').map((c) => c.id)).toEqual(['action:new']);
    expect(filterCommands(commands, 'dock').map((c) => c.id)).toContain('recent:1');
    expect(filterCommands(commands, 'zzz')).toEqual([]);
  });

  it('matches hint aliases (cmdk keywords semantics)', () => {
    expect(filterCommands(commands, 'dock').map((c) => c.id)).toContain('action:new');
  });

  it('keeps declared order on equal scores', () => {
    const twin = [cmd('a', 'alpha', 2), cmd('b', 'alpha', 1)];
    expect(filterCommands(twin, 'alpha').map((c) => c.id)).toEqual(['b', 'a']);
  });
});

describe('groupCommands', () => {
  it('merges same-name groups into their first bucket, preserving per-command order', () => {
    const grouped = groupCommands([
      cmd('a', 'x', 1, 'Navigation'),
      cmd('b', 'x', 2, 'Actions'),
      cmd('c', 'x', 3, 'Navigation'),
    ]);
    expect(grouped.map((bucket) => bucket.group)).toEqual(['Navigation', 'Actions']);
    expect(grouped[0].commands.map((c) => c.id)).toEqual(['a', 'c']);
  });
});

describe('nextPaletteIndex (cmdk keyboard model)', () => {
  it('loops arrows at the edges', () => {
    expect(nextPaletteIndex(4, 3, 'ArrowDown')).toBe(0);
    expect(nextPaletteIndex(4, 0, 'ArrowUp')).toBe(3);
  });
  it('Home/End jump to the extremes; clamps out-of-range current', () => {
    expect(nextPaletteIndex(5, 3, 'Home')).toBe(0);
    expect(nextPaletteIndex(5, 99, 'End')).toBe(4);
  });
  it('pages move in blocks of six and clamp', () => {
    expect(nextPaletteIndex(20, 0, 'PageDown')).toBe(6);
    expect(nextPaletteIndex(20, 18, 'PageDown')).toBe(19);
    expect(nextPaletteIndex(20, 19, 'PageUp')).toBe(13);
    expect(nextPaletteIndex(3, 1, 'PageUp')).toBe(0);
  });
  it('empty list stays at zero', () => {
    expect(nextPaletteIndex(0, 0, 'ArrowDown')).toBe(0);
  });
});

describe('isPaletteToggleEvent', () => {
  const key = (k: string, mods: { meta?: boolean; ctrl?: boolean }) =>
    ({ key: k, metaKey: Boolean(mods.meta), ctrlKey: Boolean(mods.ctrl) }) as KeyboardEvent;

  it('matches cmd+k and ctrl+k in either case, nothing else', () => {
    expect(isPaletteToggleEvent(key('k', { meta: true }))).toBe(true);
    expect(isPaletteToggleEvent(key('K', { ctrl: true }))).toBe(true);
    expect(isPaletteToggleEvent(key('k', {}))).toBe(false);
    expect(isPaletteToggleEvent(key('j', { meta: true }))).toBe(false);
  });
});
