import { describe, it, expect } from 'vitest';
import {
  appendInputHistory,
  nextInputHistoryNav,
  shouldNavigateHistory,
  INPUT_HISTORY_LIMIT
} from './copilotInputHistory';

describe('appendInputHistory', () => {
  it('appends a trimmed entry', () => {
    expect(appendInputHistory(['a'], '  b  ')).toEqual(['a', 'b']);
  });

  it('ignores empty/whitespace input', () => {
    expect(appendInputHistory(['a'], '   ')).toEqual(['a']);
    expect(appendInputHistory([], '')).toEqual([]);
  });

  it('dedups a consecutive duplicate but keeps an earlier repeat', () => {
    expect(appendInputHistory(['a', 'b'], 'b')).toEqual(['a', 'b']);
    expect(appendInputHistory(['a', 'b'], 'a')).toEqual(['a', 'b', 'a']);
  });

  it('caps to the limit, dropping the oldest', () => {
    const start = ['x', 'y'];
    const result = appendInputHistory(start, 'z', 2);
    expect(result).toEqual(['y', 'z']);
  });

  it('respects the default limit constant', () => {
    const many = Array.from({ length: INPUT_HISTORY_LIMIT }, (_, i) => `m${i}`);
    const result = appendInputHistory(many, 'new');
    expect(result).toHaveLength(INPUT_HISTORY_LIMIT);
    expect(result[result.length - 1]).toBe('new');
    expect(result[0]).toBe('m1'); // m0 dropped
  });
});

describe('nextInputHistoryNav', () => {
  const history = ['old', 'mid', 'new'];

  it('↑ from idle jumps to the newest, snapshotting the in-progress draft', () => {
    const res = nextInputHistoryNav(history, null, 'typing', 'up');
    expect(res).toEqual({ nav: { index: 2, draft: 'typing' }, value: 'new' });
  });

  it('↑ walks older, preserving the original snapshot', () => {
    const res = nextInputHistoryNav(history, { index: 2, draft: 'typing' }, 'ignored', 'up');
    expect(res).toEqual({ nav: { index: 1, draft: 'typing' }, value: 'mid' });
  });

  it('↑ at the oldest returns null (no change)', () => {
    expect(nextInputHistoryNav(history, { index: 0, draft: 'typing' }, 'mid', 'up')).toBeNull();
  });

  it('↓ moves forward', () => {
    const res = nextInputHistoryNav(history, { index: 1, draft: 'typing' }, 'ignored', 'down');
    expect(res).toEqual({ nav: { index: 2, draft: 'typing' }, value: 'new' });
  });

  it('↓ past the newest restores the in-progress draft and ends navigation', () => {
    const res = nextInputHistoryNav(history, { index: 2, draft: 'typing' }, 'ignored', 'down');
    expect(res).toEqual({ nav: null, value: 'typing' });
  });

  it('↓ while idle returns null (let the caret move normally)', () => {
    expect(nextInputHistoryNav(history, null, 'typing', 'down')).toBeNull();
  });

  it('returns null on empty history', () => {
    expect(nextInputHistoryNav([], null, 'typing', 'up')).toBeNull();
  });
});

describe('shouldNavigateHistory', () => {
  it('↑ navigates only on the first line', () => {
    expect(shouldNavigateHistory('up', 'one line', 8)).toBe(true);
    // caret on second line (newline before caret)
    expect(shouldNavigateHistory('up', 'one\ntwo', 7)).toBe(false);
    // caret still on first line of a multi-line value
    expect(shouldNavigateHistory('up', 'one\ntwo', 2)).toBe(true);
  });

  it('↓ navigates only on the last line', () => {
    expect(shouldNavigateHistory('down', 'one line', 0)).toBe(true);
    // caret on first line of a multi-line value (newline after caret)
    expect(shouldNavigateHistory('down', 'one\ntwo', 2)).toBe(false);
    // caret on the last line
    expect(shouldNavigateHistory('down', 'one\ntwo', 7)).toBe(true);
  });

  it('clamps an out-of-range caret', () => {
    expect(shouldNavigateHistory('up', 'abc', 99)).toBe(true);
    expect(shouldNavigateHistory('down', 'abc', -5)).toBe(true);
  });
});
