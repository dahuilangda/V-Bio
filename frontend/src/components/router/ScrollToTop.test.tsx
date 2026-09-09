import { describe, expect, it } from 'vitest';
import { shouldResetScroll } from './ScrollToTop';

describe('shouldResetScroll', () => {
  it('resets on push navigations to a new path', () => {
    expect(shouldResetScroll('/projects', '/projects/abc', 'PUSH')).toBe(true);
  });

  it('resets on replace navigations to a new path', () => {
    expect(shouldResetScroll('/login', '/projects', 'REPLACE')).toBe(true);
  });

  it('keeps the browser scroll position on back/forward (POP)', () => {
    expect(shouldResetScroll('/projects/abc', '/projects', 'POP')).toBe(false);
  });

  it('keeps position when only the search params change (same path)', () => {
    expect(shouldResetScroll('/projects/1/tasks', '/projects/1/tasks', 'PUSH')).toBe(false);
  });

  it('does not scroll on first mount (no previous path)', () => {
    expect(shouldResetScroll(null, '/projects', 'POP')).toBe(false);
  });
});
